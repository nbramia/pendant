import Foundation

/// Read-only client for the server's transcript feed (pendant_direct.feed).
/// Responses are cached on disk so the app shows the last known data offline.
struct Conversation: Codable, Identifiable, Hashable {
    let time: String
    let title: String
    let device: String?
    let durationS: Int
    let text: String
    let words: Int
    let overview: String?
    let actionItems: [String]?
    let notable: Bool?
    let meeting: String?
    let meetingSource: String?
    /// "HH:MM" of each button-press star in this conversation.
    let stars: [String]?
    /// Which pendant and when the conversation started (ms): its key for "Add to tasks".
    let deviceId: String?
    let startMs: Int?
    /// Action items already sent to LifeOS tasks.
    let tasked: [String]?
    /// Speaker tag -> voice cluster id, for tags that name exactly one voice (absent otherwise:
    /// "Everywhere in this conversation" can't tell those apart).
    let speakerTags: [String: Int]?
    /// Paragraph indices the server shows as reassigned one by one.
    let lineOverrides: [Int]?
    /// Starred from the app (swipe right), as opposed to a button press.
    let appStar: Bool?
    /// "conversation", "media" or "fragment" (absent from older servers: a conversation), and 0-2.
    let kind: String?
    let importance: Int?
    /// [start_s, end_s] into the conversation's clip for each stamped paragraph (recordings from #26 on).
    let paragraphOffsets: [[Double]]?
    /// Every Granola/Wispr note linked to it (all parts of a split meeting share them).
    let meetings: [MeetingLink]?
    /// The calendar event it was matched to, with attendees resolved to LifeOS people.
    let calendar: CalendarMatch?
    var recordingKind: RecordingKind { RecordingKind(feed: kind) }
    var id: String { "\(time)|\(title)|\(device ?? "")" }

    enum CodingKeys: String, CodingKey {
        case time, title, device, text, words, overview, notable, stars
        case durationS = "duration_s"
        case actionItems = "action_items"
        case meeting
        case meetingSource = "meeting_source"
        case deviceId = "device_id"
        case startMs = "start_ms"
        case tasked
        case speakerTags = "speaker_tags"
        case lineOverrides = "line_overrides"
        case appStar = "app_star"
        case kind, importance
        case paragraphOffsets = "paragraph_offsets"
        case meetings, calendar
    }

    /// The linked meeting notes: the feed's list, else the older single link.
    var meetingLinks: [MeetingLink] {
        if let meetings, !meetings.isEmpty { return meetings }
        guard let meeting else { return [] }
        return [MeetingLink(note: meeting, source: meetingSource ?? "")]
    }

    /// "<device>-<start_ms>": the key swipes, Undo and the trash use; nil for sections without one.
    /// Device id of the screen tour's inert sample clips (debug builds with `-screenTourSamples`).
    static let tourSampleDevice = "TOURSAMPLE"

    var rowKey: String? {
        guard let deviceId, let startMs else { return nil }
        return "\(deviceId)-\(startMs)"
    }

    /// Older sections are titled with the clip name (DEVICE_YYYYMMDD_HHMMSS).
    var hasRealTitle: Bool { !(title.contains("_") && title.split(separator: "_").count == 3) }

    var durationLabel: String {
        durationS >= 60 ? "\(durationS / 60) min" : "\(durationS) s"
    }

    static let starMark = "⭐"

    typealias Paragraph = (stamp: String?, text: String, starred: Bool, index: Int?, speaker: String?, spoken: String)

    /// Paragraphs, with the "[HH:MM]" stamps the processor adds split out, and
    /// the "⭐ " it puts after the stamp of a starred paragraph turned into `starred`.
    /// `index` counts stamped paragraphs (the server's paragraph numbering); `speaker`
    /// and `spoken` split a leading `**Name:**` from the words (`text` keeps both).
    var paragraphs: [Paragraph] {
        let lines = text.split(separator: "\n").map(String.init)
        let indexes = Reassign.paragraphIndexes(lines: lines)
        return lines.enumerated().map { n, s -> Paragraph in
            var stamp: String?
            var body = s
            if s.hasPrefix("["), let close = s.firstIndex(of: "]") {
                stamp = String(s[s.index(after: s.startIndex)..<close])
                body = s[s.index(after: close)...].trimmingCharacters(in: .whitespaces)
            }
            var starred = false
            if body.hasPrefix(Conversation.starMark) {
                body = body.dropFirst(Conversation.starMark.count).trimmingCharacters(in: .whitespaces)
                starred = true
            }
            let label = Reassign.label(in: body)
            return (stamp, body, starred, indexes[n], label?.tag, label?.rest ?? body)
        }
    }

    var starCount: Int { stars?.count ?? 0 }

    var preview: String {
        if let o = overview, !o.isEmpty { return o }
        return paragraphs.map { $0.text.replacingOccurrences(of: "**", with: "") }.joined(separator: " ")
    }

    /// The transcript's words with `**Name:**` labels kept, for previews that chip the names;
    /// nil when the preview is the overview (it has no names).
    var transcriptPreview: String? {
        if let o = overview, !o.isEmpty { return nil }
        return paragraphs.map(\.text).joined(separator: " ")
    }
}

struct DayFeed: Codable, Equatable {
    let date: String
    let conversations: [Conversation]
    let hourlyMinutes: [Double]
    let talkMinutes: Double
    let words: Int

    enum CodingKeys: String, CodingKey {
        case date, conversations, words
        case hourlyMinutes = "hourly_minutes"
        case talkMinutes = "talk_minutes"
    }
}

struct DaySummary: Codable, Identifiable, Hashable {
    let date: String
    let conversations: Int
    let talkMinutes: Double
    let words: Int
    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, conversations, words
        case talkMinutes = "talk_minutes"
    }
}

struct DaysFeed: Codable { let days: [DaySummary] }

struct ServerStatus: Codable {
    struct Pendant: Codable { let device: String; let lastAudio: Int?; let totalMinutes: Double
        enum CodingKeys: String, CodingKey { case device; case lastAudio = "last_audio"; case totalMinutes = "total_minutes" } }
    let pendants: [Pendant]
    let lastUpload: Int?
    let untranscribedMinutes: Double
    enum CodingKeys: String, CodingKey {
        case pendants
        case lastUpload = "last_upload"
        case untranscribedMinutes = "untranscribed_minutes"
    }
}

struct UnknownSpeaker: Codable, Identifiable, Hashable, HintedCard {
    let id: Int
    let when: Int?            // ms since epoch
    let durationS: Double?
    let excerpt: String?
    let suggested: String?
    let currentLabel: String?
    /// Who it probably is from the calendar, and the event's other remaining attendees.
    let likely: LikelyPerson?
    let candidates: [PersonSuggestion]?
    /// The same with LifeOS's `maybe` people after them (as the speaker sheet gets them).
    let hints: CalendarHint?
    /// The conversation the voice comes from.
    let conversation: CardConversation?
    /// The hints once another card from the same conversation was answered (`CardHints.named`).
    private var narrowed: CalendarHint? = nil
    enum CodingKeys: String, CodingKey {
        case id, when, excerpt, suggested, likely, candidates, hints, conversation
        case durationS = "duration_s"
        case currentLabel = "current_label"
    }

    var hint: CalendarHint {
        get { narrowed ?? hints ?? CalendarHint(likely: likely, candidates: candidates ?? []) }
        set { narrowed = newValue }
    }

    /// The card's one-tap guess (`CalendarHint.cardGuess`).
    var oneTap: CardGuess? { hint.cardGuess(asking: currentLabel ?? suggested) }

    /// The people heading the "Someone else" picker: the calendar's candidates, then the other maybes.
    var pickerLead: [PersonSuggestion] { hint.pickerLead(for: currentLabel ?? suggested ?? Reassign.someone) }
}

struct UnknownSpeakers: Codable { let speakers: [UnknownSpeaker] }

/// The same candidates as Voice Scoring cards (`VoiceCard`, Core).
struct VoiceCards: Codable { let speakers: [VoiceCard] }

struct SpeakerRoster: Codable {
    /// count = voiceprint samples; recognized = conversations' voices carrying the name.
    struct Enrolled: Codable, Hashable { let name: String; let count: Int; let recognized: Int? }
    let enrolled: [Enrolled]
    let allowed: [String]
}

enum Feed {
    /// Stars a conversation from the app, or takes that star off.
    static func star(deviceId: String, startMs: Int, on: Bool) async throws {
        guard deviceId != Conversation.tourSampleDevice else { return }   // screen-tour samples
        try await post(RowRequest.path(device: deviceId, startMs: startMs, "star"), json: ["on": on])
    }

    /// Moves a conversation to the 7-day trash on the server.
    static func delete(deviceId: String, startMs: Int) async throws {
        guard deviceId != Conversation.tourSampleDevice else { return }   // screen-tour samples
        try await post(RowRequest.path(device: deviceId, startMs: startMs, "delete"), json: [:])
    }

    /// Puts a deleted conversation back.
    static func restore(deviceId: String, startMs: Int) async throws {
        guard deviceId != Conversation.tourSampleDevice else { return }   // screen-tour samples
        try await post(RowRequest.path(device: deviceId, startMs: startMs, "restore"), json: [:])
    }

    /// Records a speaker reassignment (`Reassign.request`). The receipt carries the change's Undo
    /// id; nil when the server sends none (older servers) or its answer can't be read.
    static func reassign(deviceId: String, path: String, json: [String: Any]) async throws -> ReassignReceipt? {
        guard deviceId != Conversation.tourSampleDevice else { return .tourSample() }   // screen-tour samples
        let data = try await post(path, json: json)
        return try? JSONDecoder().decode(ReassignReceipt.self, from: data)
    }

    /// Undoes a saved reassignment (`POST reassign/<change_id>/undo`); an undo already done answers 200 too.
    static func undoReassign(changeId: String) async throws {
        guard changeId != ReassignReceipt.tourSampleId else { return }   // screen-tour samples
        try await post(Reassign.undoPath(changeId: changeId), json: [:])
    }

    /// Asks the server to create a LifeOS task for an action item (once per item).
    static func addToTasks(deviceId: String, startMs: Int, item: String) async throws {
        try await post("tasks", json: ["device_id": deviceId, "start_ms": startMs, "item": item])
    }

    static var base: URL? {
        #if DEBUG
        // UI tests point the feed somewhere that refuses connections to see the error state.
        if let o = UserDefaults.standard.string(forKey: "feedBaseOverride"), let u = URL(string: o) { return u }
        #endif
        // Ingest URL is .../pendant/ingest; the feed lives beside it.
        return Uploader.baseURL?.deletingLastPathComponent().appendingPathComponent("feed")
    }

    /// base + path, with any "?query" kept as the query (appendingPathComponent would escape the "?").
    static func url(_ path: String) -> URL? {
        guard let base else { return nil }
        return FeedURL.make(base: base, path: path)
    }

    /// A request with the bearer token set.
    private static func request(_ url: URL, method: String = "GET", body: Data? = nil, type: String? = nil,
                                timeout: TimeInterval? = nil) -> URLRequest {
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("Bearer \(Uploader.token)", forHTTPHeaderField: "Authorization")
        if let type { req.setValue(type, forHTTPHeaderField: "Content-Type") }
        req.httpBody = body
        if let timeout { req.timeoutInterval = timeout }
        return req
    }

    /// One request through the retry layer: transient failures (offline, timeouts, 502/503/504 and
    /// other 5xx) are retried after 0.5 s, 1.5 s and 4 s; 4xx never are. Decoding is part of the
    /// attempt's route logging, but a decode error is final.
    private static func perform(_ path: String, _ req: URLRequest) async throws -> Data {
        guard req.url != nil else { throw URLError(.badURL) }
        return try await FeedClient.run(route: FeedClient.route(path)) {
            let (data, resp) = try await URLSession.shared.data(for: req)
            return try FeedClient.body(data, resp)
        }
    }

    /// `cached: false` for answers that must be live (a preview of a change never falls back to an old copy).
    static func fetch<T: Decodable>(_ path: String, as: T.Type, cached: Bool = true) async throws -> T {
        guard let url = url(path) else { throw URLError(.badURL) }
        let req = request(url, timeout: 15)
        let cache = cacheURL(path)
        do {
            let data = try await perform(path, req)
            let value: T
            do { value = try JSONDecoder().decode(T.self, from: data) } catch {
                FeedLog.write(FeedProblem(error).logLine(route: FeedClient.route(path)))
                throw error
            }
            if cached { try? data.write(to: cache) }
            return value
        } catch {
            if cached, let data = try? Data(contentsOf: cache), let value = try? JSONDecoder().decode(T.self, from: data) {
                return value
            }
            throw error
        }
    }

    /// Downloads raw bytes (e.g. an audio snippet) with auth.
    static func data(_ path: String) async throws -> Data {
        guard let url = url(path) else { throw URLError(.badURL) }
        return try await perform(path, request(url))
    }

    /// The server's status when it answered with something other than 200.
    typealias HTTPStatus = FeedHTTPStatus

    /// POST of JSON; returns the answer's body for the callers that read it.
    @discardableResult
    static func post(_ path: String, json: [String: Any]) async throws -> Data {
        guard let url = url(path) else { throw URLError(.badURL) }
        let body = try JSONSerialization.data(withJSONObject: json)
        return try await perform(path, request(url, method: "POST", body: body, type: "application/json"))
    }

    /// POST that returns JSON (e.g. a new voice-sample reading's id and verdict).
    static func send<T: Decodable>(_ path: String, json: [String: Any], as: T.Type) async throws -> T {
        guard let url = url(path) else { throw URLError(.badURL) }
        let body = try JSONSerialization.data(withJSONObject: json)
        let data = try await perform(path, request(url, method: "POST", body: body, type: "application/json", timeout: 20))
        do { return try JSONDecoder().decode(T.self, from: data) } catch {
            FeedLog.write(FeedProblem(error).logLine(route: FeedClient.route(path)))
            throw error
        }
    }

    /// POST of raw audio (the phone-mic voice sample); answers JSON.
    static func upload<T: Decodable>(_ path: String, body: Data, type: String, as: T.Type) async throws -> T {
        guard let url = url(path) else { throw URLError(.badURL) }
        let data = try await perform(path, request(url, method: "POST", body: body, type: type, timeout: 60))
        do { return try JSONDecoder().decode(T.self, from: data) } catch {
            FeedLog.write(FeedProblem(error).logLine(route: FeedClient.route(path)))
            throw error
        }
    }

    private static func cacheURL(_ path: String) -> URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("feed")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let name = path.replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "?", with: "_")
        return dir.appendingPathComponent(String(name.prefix(120)) + ".json")
    }

    static func todayString(_ date: Date = Date()) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}
