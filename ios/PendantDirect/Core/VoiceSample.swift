import Foundation

/// "Record a voice sample": a person reads the sentences below near the pendant while the
/// pendant and the phone's own microphone both record. The app sends the reading's window to
/// the server (`POST /pendant/feed/enroll`), which finds the pendant's audio and judges it
/// when it syncs, and uploads the phone's recording (`POST .../enroll/<id>/phone`), which is
/// judged at once. Each adds its own samples (tagged `pendant` and `phone`).
public enum VoiceSampleScript {
    /// Eight sentences, phonetically varied, about a minute at a normal pace.
    public static let sentences: [String] = [
        "Please call Stella and ask her to bring these things from the store: six spoons of fresh peas, five slabs of blue cheese, and a snack for her brother Bob.",
        "When the sunlight strikes raindrops in the air, they act as a prism and form a rainbow.",
        "My neighbor's wheelbarrow rattled down the gravel driveway just after midnight, and every dog on the street started barking.",
        "Could you tell me whether the thirty-third train to Chicago leaves from platform four or platform fourteen?",
        "Jack quickly picked twelve ripe oranges, but the zebra at the zoo vexed him by gulping them down.",
        "Yesterday I thought about buying a used guitar, then decided that a quiet weekend by the lake sounded better.",
        "Her voice rose and fell as she described the enormous yellow kite tangled in the old oak tree.",
        "Finally, this is how I sound when I talk normally, about an arm's length from the pendant.",
    ]

    /// Reading for less than this can't hold the 25 s of speech the server needs.
    public static let minimumSeconds: Double = 25

    /// The phone's recording stops here (the server takes at most 60 s of it).
    public static let phoneMaxSeconds: Double = 60

    public static func phonePath(id: Int) -> String { "enroll/\(id)/phone" }

    /// "Yes, this reading is them": the saved voiceprint gives way to it.
    public static func replacePath(id: Int) -> String { "enroll/\(id)/replace" }
}

/// `POST /pendant/feed/enroll`.
public struct EnrollRequest: Equatable {
    public let name: String
    public let device: String
    public let startMs: Int
    public let endMs: Int
    /// The LifeOS person picked for this name, if any (links the name; nothing else).
    public let entityId: String?

    public init(name: String, device: String, startMs: Int, endMs: Int, entityId: String? = nil) {
        self.name = name; self.device = device; self.startMs = startMs; self.endMs = endMs; self.entityId = entityId
    }

    public static let path = "enroll"

    public var body: [String: Any] {
        var b: [String: Any] = ["name": name, "device": device, "start_ms": startMs, "end_ms": endMs]
        if let entityId { b["entity_id"] = entityId; b["source"] = "user" }
        return b
    }
}

/// What the server says about a reading (`GET /pendant/feed/enroll/<id>`, also the POST's answer).
public enum EnrollOutcome: Equatable {
    case waiting
    case added(samples: Int)
    case rejected(reason: String)

    /// What went wrong, with something the person can do about it.
    public static func advice(for reason: String) -> String {
        switch reason {
        case "too little speech":
            return "There wasn't enough speech. Read all eight sentences, a little closer to the pendant."
        case "more than one voice":
            return "The recording held more than one voice. Read alone, somewhere quiet."
        case "no audio from that pendant":
            return "The pendant sent no audio for that time. Check that it is on, charged and in range, then try again."
        case "could not be checked":
            return "\(AppSettings.serverNameAtStart) couldn't check that recording, so it was treated as ordinary audio. Try again later."
        case "overlaps another voice sample":
            return "Another voice sample already covers that time. Wait for its result, then try again."
        case "reserved name":
            return "That name is reserved (like \"Me\" or \"Speaker A\"). Pick the person's own name."
        case "different voice":
            return "This voice doesn't sound like the one already saved under that name."
        case "bad request":
            return "That name or reading time couldn't be used. Check the name and try again."
        case "not allowed":     // an older server, before the two reasons were told apart
            return "That name is reserved, or this voice doesn't match the one already saved under it."
        default:
            return reason
        }
    }
}

/// How the phone-microphone copy of a reading stands.
public enum PhoneOutcome: Equatable {
    /// The phone didn't record (microphone permission denied): the pendant copy alone counts.
    case none
    /// Recording while the person reads.
    case recording
    /// Uploaded, or about to be; the server is checking it.
    case uploading
    case added(samples: Int)
    case rejected(reason: String)
    /// The upload didn't get through; it can be sent again.
    case failed
}

public struct EnrollStatus: Codable, Equatable {
    public struct Phone: Codable, Equatable {
        public let status: String
        public let samples: Int?
        public let reason: String?
        /// How close the reading came to the saved voiceprint (when refused as a different voice).
        public let match: Double?
        public init(status: String, samples: Int? = nil, reason: String? = nil, match: Double? = nil) {
            self.status = status; self.samples = samples; self.reason = reason; self.match = match
        }
        public var outcome: PhoneOutcome {
            status == "added" ? .added(samples: samples ?? 0) : .rejected(reason: reason ?? "rejected")
        }
    }

    public let id: Int
    public let name: String?
    public let status: String
    public let samples: Int?
    public let reason: String?
    /// The phone-mic copy's verdict, once uploaded.
    public let phone: Phone?
    /// How many samples were saved under the name when a copy was refused as a different voice.
    public let saved: Int?
    /// A copy was refused as a different voice: `replace` can use the reading instead.
    public let replaceable: Bool?
    /// The reading replaced what was saved under the name.
    public let replaced: Bool?

    public init(id: Int, name: String? = nil, status: String, samples: Int? = nil, reason: String? = nil,
                phone: Phone? = nil, saved: Int? = nil, replaceable: Bool? = nil, replaced: Bool? = nil) {
        self.id = id; self.name = name; self.status = status; self.samples = samples; self.reason = reason
        self.phone = phone; self.saved = saved; self.replaceable = replaceable; self.replaced = replaced
    }

    public var outcome: EnrollOutcome {
        switch status {
        case "added": return .added(samples: samples ?? 0)
        case "rejected": return .rejected(reason: reason ?? "rejected")
        default: return .waiting
        }
    }
}

/// The flow's state: pick a name, read, send the window, wait for the verdict.
/// Pure (no clock, no network) so the transitions can be tested.
public struct VoiceSampleFlow: Equatable {
    public enum Phase: Equatable {
        case name
        case ready
        case reading(start: Date)
        case tooShort(seconds: Int)
        case sending
        case sendFailed
        case waiting(id: Int)
        case added(samples: Int)
        case rejected(reason: String)
    }

    public private(set) var phase: Phase = .name
    /// The pendant the reading is attributed to: the one connected when Start was pressed.
    public private(set) var device: String?
    public private(set) var name: String = ""
    /// The LifeOS person picked with the name (nil when typed).
    public private(set) var entityId: String?
    public private(set) var request: EnrollRequest?
    /// The phone-mic copy of this reading.
    public private(set) var phone: PhoneOutcome = .none
    /// The server's id for this reading, once sent.
    public private(set) var readingId: Int?
    /// A copy was refused as a different voice and the reading can replace what's saved.
    public private(set) var replaceable = false
    /// Samples saved under the name when that refusal happened.
    public private(set) var savedSamples: Int?
    /// The reading replaced what was saved under the name.
    public private(set) var replaced = false
    /// The replace request is on its way.
    public private(set) var replacing = false

    public init() {}

    /// The screen tour's inert sample: a reading refused as a different voice, asking to replace.
    /// It has no reading id, so its Replace button sends nothing.
    public static func tourReplaceSample(name: String) -> VoiceSampleFlow {
        var f = VoiceSampleFlow()
        f.choose(name: name)
        f.phase = .rejected(reason: "different voice")
        f.phone = .rejected(reason: "different voice")
        f.replaceable = true
        f.savedSamples = 19
        return f
    }

    /// A name the server accepts: a letter first, then letters, spaces, ' or -, at most 40.
    public static func clean(_ raw: String) -> String? {
        let n = raw.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard let first = n.unicodeScalars.first, n.unicodeScalars.count <= 40,
              first.isASCII, CharacterSet.letters.contains(first),
              n.unicodeScalars.allSatisfy({ $0.isASCII && (CharacterSet.letters.contains($0) || " '-".unicodeScalars.contains($0)) })
        else { return nil }
        return n
    }

    public mutating func choose(name raw: String, entityId: String? = nil) {
        guard let n = Self.clean(raw) else { return }
        name = n
        self.entityId = entityId
        phase = .ready
    }

    public mutating func changeName() { phase = .name }

    public var canStart: Bool {
        switch phase {
        case .ready, .tooShort: return true
        default: return false
        }
    }

    /// Start reading. Needs a connected pendant: its id goes to the server with the times.
    @discardableResult
    public mutating func start(now: Date, device: String?, phoneRecording: Bool = false) -> Bool {
        guard canStart, let device, !device.isEmpty else { return false }
        self.device = device
        request = nil
        clearReplace()
        phone = phoneRecording ? .recording : .none
        phase = .reading(start: now)
        return true
    }

    public func elapsed(now: Date) -> Double {
        if case .reading(let s) = phase { return max(0, now.timeIntervalSince(s)) }
        return 0
    }

    /// Done reading. Returns the request to send, or nil when it was too short to hold enough speech.
    public mutating func done(now: Date) -> EnrollRequest? {
        guard case .reading(let start) = phase, let device else { return nil }
        let seconds = now.timeIntervalSince(start)
        guard seconds >= VoiceSampleScript.minimumSeconds else {
            phase = .tooShort(seconds: Int(seconds))
            phone = .none
            return nil
        }
        if phone == .recording { phone = .uploading }
        let req = EnrollRequest(name: name, device: device, startMs: Int((start.timeIntervalSince1970 * 1000).rounded()),
                                endMs: Int((now.timeIntervalSince1970 * 1000).rounded()),
                                entityId: entityId)
        request = req
        phase = .sending
        return req
    }

    /// The POST answered (the id, and possibly an immediate verdict).
    public mutating func sent(_ status: EnrollStatus) { apply(status) }

    /// The POST failed; the same window can be sent again.
    public mutating func sendFailed() {
        if phase == .sending { phase = .sendFailed }
    }

    public mutating func retrySend() -> EnrollRequest? {
        guard phase == .sendFailed, let request else { return nil }
        phase = .sending
        return request
    }

    /// The phone upload's answer (the status with its `phone` verdict).
    public mutating func phoneSent(_ status: EnrollStatus) {
        if let p = status.phone { phone = p.outcome } else { phone = .failed }
        note(status)
    }

    /// The replace question: "This voice doesn't sound like the N samples saved for Riley. If it
    /// really is Riley, replace them with this reading." nil when there's nothing to ask.
    public var replaceQuestion: String? {
        guard replaceable, !replaced, !anyAdded else { return nil }
        let saved = savedSamples.map { "the \($0) samples saved" } ?? "what's saved"
        return "This reading doesn't sound like \(saved) for \(name). If it really is \(name), "
            + "replace what's saved with this reading. The old samples are backed up."
    }

    /// Tapped "Replace": returns the reading's id to send it for, nil when there's nothing to replace.
    public mutating func startReplace() -> Int? {
        guard replaceQuestion != nil, !replacing, let id = readingId else { return nil }
        replacing = true
        return id
    }

    /// The replace answered (the status afterwards), or failed (nil).
    public mutating func replaceFinished(_ status: EnrollStatus?) {
        replacing = false
        if let status { apply(status) }
    }

    private mutating func note(_ status: EnrollStatus) {
        if status.id > 0 { readingId = status.id }
        if let s = status.saved { savedSamples = s }
        replaceable = status.replaceable ?? false
        if status.replaced == true { replaced = true }
    }

    private mutating func clearReplace() {
        readingId = nil; replaceable = false; savedSamples = nil; replaced = false; replacing = false
    }

    /// Sending the phone recording again.
    public mutating func retryPhone() { if phone == .failed { phone = .uploading } }

    /// The phone upload didn't get through (or was refused as unusable audio).
    public mutating func phoneFailed() { phone = .failed }

    /// Whether any copy of the reading has added samples.
    public var anyAdded: Bool {
        if case .added = phase { return true }
        if case .added = phone { return true }
        return false
    }

    /// "Phone recording: 12 samples added", or nil when the phone didn't record.
    public var phoneLine: String? {
        switch phone {
        case .none, .recording: return nil
        case .uploading: return "Phone recording: checking…"
        case .added(let n): return "Phone recording: \(n) samples added"
        case .rejected(let r): return "Phone recording not added. " + EnrollOutcome.advice(for: r)
        case .failed: return "Phone recording didn't upload."
        }
    }

    /// "Pendant copy: pending…", once the window is sent.
    public var pendantLine: String? {
        switch phase {
        case .waiting: return "Pendant copy: pending, added when the pendant syncs"
        case .added(let n): return "Pendant copy: \(n) samples added"
        case .rejected(let r): return "Pendant copy not added. " + EnrollOutcome.advice(for: r)
        default: return nil
        }
    }

    /// A poll's answer.
    public mutating func apply(_ status: EnrollStatus) {
        if let p = status.phone { phone = p.outcome }
        note(status)
        switch status.outcome {
        case .waiting: phase = .waiting(id: status.id)
        case .added(let n): phase = .added(samples: n)
        case .rejected(let r): phase = .rejected(reason: r)
        }
    }

    /// After a rejection (or a too-short attempt): back to the sentences with the same name.
    public mutating func tryAgain() {
        request = nil
        clearReplace()
        phone = .none
        phase = .ready
    }

    public var isFinished: Bool {
        switch phase {
        case .added, .rejected: return true
        default: return false
        }
    }
}
