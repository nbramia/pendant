import Foundation

/// Maps transcript lines to positions in a conversation's audio, and back.
///
/// A line's position comes from `paragraph_offsets` (seconds into the clip, per stamped
/// paragraph, recordings from #26 on). Older recordings have only the "HH:MM" stamp, so a
/// line falls back to that minute minus the conversation's start (minute resolution).
public enum Playback {
    /// Seconds into the audio where a line starts, or nil if nothing places it.
    /// - Parameters:
    ///   - index: the line's stamped-paragraph index (`Conversation.Paragraph.index`).
    ///   - stamp: the line's "HH:MM" text.
    public static func seekTime(index: Int?, stamp: String?, offsets: [[Double]]?, startMs: Int?,
                                timeZone: TimeZone = .current) -> Double? {
        if let index, let offsets, offsets.indices.contains(index), let s = offsets[index].first, s >= 0 {
            return s
        }
        guard let stamp, let startMs, let minute = minuteOfDay(stamp) else { return nil }
        return max(0, minuteStart(minute, startMs: startMs, timeZone: timeZone))
    }

    /// Seconds from the conversation's start to the start of a stamped minute. Negative only by the
    /// part of that minute before the conversation began; a stamp earlier than that is the next day.
    static func minuteStart(_ minute: Int, startMs: Int, timeZone: TimeZone) -> Double {
        var d = Double(minute * 60) - secondOfDay(ms: startMs, timeZone: timeZone)
        if d < -60 { d += 86400 }
        return d
    }

    /// One transcript line as `lineStarts` needs it.
    public struct Line {
        public let index: Int?, stamp: String?, words: Int
        public init(index: Int?, stamp: String?, words: Int) { self.index = index; self.stamp = stamp; self.words = words }
    }

    /// Every line's start. Lines with offsets use them. Without offsets, consecutive lines sharing a
    /// minute stamp are spread through that minute in proportion to their words, so seeking and the
    /// highlight walk through them in order instead of all landing on the minute.
    public static func lineStarts(_ lines: [Line], offsets: [[Double]]?, startMs: Int?,
                                  timeZone: TimeZone = .current) -> [Double?] {
        var out = lines.map { seekTime(index: $0.index, stamp: $0.stamp, offsets: offsets, startMs: startMs, timeZone: timeZone) }
        func hasOffset(_ l: Line) -> Bool {
            guard let i = l.index, let offsets, offsets.indices.contains(i), let s = offsets[i].first else { return false }
            return s >= 0
        }
        guard let startMs else { return out }
        var i = 0
        while i < lines.count {
            guard !hasOffset(lines[i]), let stamp = lines[i].stamp, let minute = minuteOfDay(stamp) else { i += 1; continue }
            var j = i + 1
            while j < lines.count, !hasOffset(lines[j]), lines[j].stamp == stamp { j += 1 }
            if j - i > 1 {
                let lo = max(0, minuteStart(minute, startMs: startMs, timeZone: timeZone))
                let hi = max(minuteStart(minute, startMs: startMs, timeZone: timeZone) + 60, lo)
                let weights = lines[i..<j].map { Double(max($0.words, 1)) }
                let total = weights.reduce(0, +)
                var before = 0.0
                for (k, w) in weights.enumerated() {
                    out[i + k] = lo + (hi - lo) * before / total
                    before += w
                }
            }
            i = j
        }
        return out
    }

    /// The line playing at `time`: the last line starting at or before it.
    /// `starts` holds each line's start (nil for lines nothing places) in transcript order.
    public static func activeLine(starts: [Double?], at time: Double) -> Int? {
        var best: Int?
        var bestStart = -Double.infinity
        for (i, s) in starts.enumerated() {
            guard let s, s <= time + 0.05, s >= bestStart else { continue }
            best = i
            bestStart = s
        }
        return best
    }

    /// "1:05" or "1:02:03".
    public static func clock(_ seconds: Double) -> String {
        let t = max(0, Int(seconds.isFinite ? seconds : 0))
        let (h, m, s) = (t / 3600, t / 60 % 60, t % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    /// 1x -> 1.5x -> 2x -> 1x.
    public static let speeds: [Float] = [1, 1.5, 2]
    public static func nextSpeed(after speed: Float) -> Float {
        let i = speeds.firstIndex(of: speed) ?? 0
        return speeds[(i + 1) % speeds.count]
    }

    public static func speedLabel(_ speed: Float) -> String { speed == speed.rounded() ? "\(Int(speed))×" : "\(speed)×" }

    /// Feed path of a conversation's audio.
    public static func audioPath(device: String, startMs: Int) -> String {
        RowRequest.path(device: device, startMs: startMs, "audio")
    }

    static func minuteOfDay(_ stamp: String) -> Int? {
        let p = stamp.split(separator: ":")
        guard p.count == 2, let h = Int(p[0]), let m = Int(p[1]), (0..<24).contains(h), (0..<60).contains(m) else { return nil }
        return h * 60 + m
    }

    static func secondOfDay(ms: Int, timeZone: TimeZone) -> Double {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        let d = Date(timeIntervalSince1970: Double(ms) / 1000)
        let c = cal.dateComponents([.hour, .minute, .second], from: d)
        return Double((c.hour ?? 0) * 3600 + (c.minute ?? 0) * 60 + (c.second ?? 0))
    }
}

/// Tapping a line's time stamp to play from it. The tapped line shows `loading` until the
/// audio is ready and positioned at its start, then `playing`; a failure shows on that line
/// with a retry (tapping it again). Only one load runs at a time: a tap while loading doesn't
/// start another, it only moves the target to the newer line (the same line is a no-op).
public struct LineTap: Equatable {
    public enum State: Equatable {
        case idle
        case loading(Int)
        case playing(Int)
        case failed(Int, String)
    }

    /// How long a tap waits for the audio (the first tap may wait for the server's transcode).
    public static let timeout: Double = 20

    public private(set) var state: State = .idle
    public init() {}

    /// A tap on line `n`. True when the caller should start loading; false while a load is
    /// already running (the running load plays the newest target when it finishes).
    public mutating func tap(_ n: Int) -> Bool {
        if case .loading = state {
            state = .loading(n)
            return false
        }
        state = .loading(n)
        return true
    }

    /// The line a running load should play, or nil when nothing is loading.
    public var target: Int? {
        if case let .loading(n) = state { return n }
        return nil
    }

    /// The audio is ready at line `n`'s start. True (now playing) when `n` is still the
    /// target; false when another line was tapped since: position at `target` and call again.
    public mutating func ready(at n: Int) -> Bool {
        guard target == n else { return false }
        state = .playing(n)
        return true
    }

    /// The running load failed: the target line shows `message`.
    public mutating func fail(_ message: String) {
        guard let n = target else { return }
        state = .failed(n, message)
    }

    public func isLoading(_ n: Int) -> Bool { target == n }

    /// The failure shown on line `n`, if any.
    public func error(on n: Int) -> String? {
        if case let .failed(line, message) = state, line == n { return message }
        return nil
    }
}
