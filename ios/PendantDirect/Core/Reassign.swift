import Foundation

/// How far a speaker reassignment reaches (`POST /pendant/feed/conversations/<device>/<start_ms>/speaker`).
public enum ReassignScope: String, CaseIterable, Identifiable {
    case line, conversation, all
    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .line: "This line"
        case .conversation: "This conversation"
        case .all: "All past conversations"
        }
    }
    /// What the sheet starts on when it is available.
    public static let `default`: ReassignScope = .conversation
    /// Whether the scope relabels a whole voice (so it needs a tag that names exactly one voice).
    public var needsVoice: Bool { self != .line }
    /// The confirm button's title: it always names the scope that will be applied.
    public var confirmTitle: String {
        switch self {
        case .line: "Change this line"
        case .conversation: "Change whole conversation"
        case .all: "Change all past conversations…"
        }
    }
    /// The confirm button's title for Remove name ("All past conversations" never removes a name).
    public var removeTitle: String? {
        switch self {
        case .line: "Remove name · this line"
        case .conversation: "Remove name · this conversation"
        case .all: nil
        }
    }
}

/// What "This conversation" covers: the sitting, the tapped piece plus the neighbouring pieces of
/// the same real conversation that hold the same voice (`GET .../speaker/<tag>/sitting`, and the
/// `sitting` of a saved answer, which adds the voices it named there).
public struct SittingScope: Codable, Equatable, Hashable {
    public let parts: Int
    /// From the first piece's start to the last one's end, in seconds (nil when unknown).
    public let spanS: Int?
    /// Voices named (or that would be) in the other pieces, and in how many of them, earlier and later.
    public let named: Int
    public let namedParts: Int
    public let before: Int
    public let after: Int
    /// The voices a saved answer named in the other pieces (Voice Scoring and Who's this drop their cards).
    public let ids: [Int]?

    enum CodingKeys: String, CodingKey {
        case parts, named, before, after, ids
        case spanS = "span_s"
        case namedParts = "named_parts"
    }

    public init(parts: Int, spanS: Int?, named: Int = 0, before: Int = 0, after: Int = 0, ids: [Int]? = nil) {
        self.parts = parts; self.spanS = spanS; self.named = named; self.namedParts = before + after
        self.before = before; self.after = after; self.ids = ids
    }

    /// The scope row's title: "This conversation (1 hr, 4 parts)"; plain for a single piece.
    public var scopeTitle: String {
        guard parts > 1 else { return ReassignScope.conversation.title }
        let span = spanS.map { "\(Self.span($0)), " } ?? ""
        return "\(ReassignScope.conversation.title) (\(span)\(parts) parts)"
    }

    /// "45 min", "1 hr", "1 hr 30 min": minutes under an hour, else hours to the nearest quarter.
    public static func span(_ seconds: Int) -> String {
        let minutes = max(1, Int((Double(seconds) / 60).rounded()))
        guard minutes >= 60 else { return "\(minutes) min" }
        let quarters = Int((Double(minutes) / 15).rounded())
        let h = quarters / 4, m = (quarters % 4) * 15
        return m == 0 ? "\(h) hr" : "\(h) hr \(m) min"
    }

    /// The saved answer's line about the other pieces: "Also named in 3 earlier parts",
    /// "Also named in 2 earlier parts and 1 later part"; nil when nothing else was named.
    public var alsoNamed: String? {
        func n(_ k: Int, _ word: String) -> String { "\(k) \(word) part\(k == 1 ? "" : "s")" }
        switch (before, after) {
        case (0, 0): return nil
        case (let b, 0): return "Also named in \(n(b, "earlier"))"
        case (0, let a): return "Also named in \(n(a, "later"))"
        case let (b, a): return "Also named in \(n(b, "earlier")) and \(n(a, "later"))"
        }
    }

    /// Path of the coverage for the voice behind `tag` in this conversation.
    public static func path(deviceId: String, startMs: Int, tag: String) -> String {
        let safe = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return "conversations/\(deviceId)/\(startMs)/speaker/\(tag.addingPercentEncoding(withAllowedCharacters: safe) ?? tag)/sitting"
    }

    /// The screen tour's inert sample: an hour in four parts, named in the three earlier ones.
    public static let tourSample = SittingScope(parts: 4, spanS: 3900, named: 3, before: 3, after: 0)
}

/// The reassign sheet's scope options. Each scope is available or carries a one-line reason
/// it isn't. The selection starts on the default when that is available, else the first one
/// that is, and after that changes only when the wearer picks an available option: picking an
/// unavailable one keeps the current selection, and nothing switches it by itself. While Remove
/// name is picked (`removing`), "All past conversations" is unavailable too; if it was selected,
/// nothing is until the wearer picks this line or this conversation, and picking a person instead
/// brings his "All past conversations" back unless he chose another scope meanwhile.
public struct ScopeChoice: Equatable {
    public struct Option: Equatable, Identifiable {
        public let scope: ReassignScope
        /// Why it can't be used here; nil when it can.
        public let reason: String?
        public var enabled: Bool { reason == nil }
        public var id: String { scope.id }
    }

    public static let lineReason = "This line can't be changed on its own."
    public static let sharedVoiceReason = "This voice shares its name with another voice here; change single lines instead."
    public static let removeAllReason = "Remove name applies to this line or this conversation only."

    /// Each scope's reason it can't apply here, Remove name aside.
    private let reasons: [ReassignScope: String]
    /// What "This conversation" covers, once the server said (nil: not known, the plain title).
    public var sitting: SittingScope?
    /// Remove name is picked: the confirm removes the name instead of naming someone.
    public private(set) var removing = false
    /// "All past conversations", set aside while Remove name is picked.
    private var setAside: ReassignScope?
    /// The scope the confirm button applies; nil when no scope is available.
    public private(set) var selected: ReassignScope?

    public var options: [Option] {
        ReassignScope.allCases.map { s in
            Option(scope: s, reason: reasons[s] ?? (removing && s == .all ? Self.removeAllReason : nil))
        }
    }

    /// - Parameters:
    ///   - lineOK: the tapped paragraph can be addressed on its own.
    ///   - voiceOK: the tag names exactly one voice in this conversation.
    public init(lineOK: Bool, voiceOK: Bool) {
        var r: [ReassignScope: String] = [:]
        for s in ReassignScope.allCases {
            if let why = s.needsVoice ? (voiceOK ? nil : Self.sharedVoiceReason) : (lineOK ? nil : Self.lineReason) {
                r[s] = why
            }
        }
        reasons = r
        let open = ReassignScope.allCases.filter { r[$0] == nil }
        selected = open.contains(ReassignScope.default) ? ReassignScope.default : open.first
    }

    /// Picking Remove name (true) or a person (false). Removing while "All past conversations" is
    /// selected leaves nothing selected; it is never swapped for another scope.
    public mutating func setRemoving(_ on: Bool) {
        removing = on
        if on, selected == .all {
            setAside = .all
            selected = nil
        } else if !on {
            if selected == nil, let s = setAside, option(s).enabled { selected = s }
            setAside = nil
        }
    }

    public func option(_ scope: ReassignScope) -> Option { options.first { $0.scope == scope }! }

    /// An option's title: "This conversation" says how much it covers ("This conversation (1 hr, 4 parts)").
    /// Remove name stays in the tapped piece, so it shows the plain title then.
    public func title(_ scope: ReassignScope) -> String {
        guard scope == .conversation, !removing, let sitting else { return scope.title }
        return sitting.scopeTitle
    }

    /// Picks `scope` if it is available. False (and the selection unchanged) when it isn't.
    @discardableResult
    public mutating func select(_ scope: ReassignScope) -> Bool {
        guard option(scope).enabled else { return false }
        selected = scope
        setAside = nil
        return true
    }

    /// A picked name waits for the confirm button: with "All past conversations" (its preview comes
    /// first), with Remove name, and when no scope can apply. Otherwise one tap on a name saves it.
    public var confirmsPick: Bool { removing || selected == nil || selected == .all }

    /// The confirm button's title for the selected scope.
    public var confirmTitle: String {
        if removing {
            if let t = selected?.removeTitle { return t }
            return options.contains { $0.enabled } ? "Choose this line or this conversation" : "Nothing here can be changed"
        }
        if let selected { return selected.confirmTitle }
        return options.contains { $0.enabled } ? "Choose where to apply" : "Nothing here can be changed"
    }

    /// The calendar card's one-tap confirm, which applies the selected scope at once:
    /// "Yes, Morgan Hale · this conversation". Nil when no scope is available.
    public func likelyTitle(_ name: String) -> String? {
        guard let selected else { return nil }
        let reach: String
        switch selected {
        case .line: reach = "this line"
        case .conversation: reach = "this conversation"
        case .all: reach = "all past conversations…"
        }
        return "Yes, \(name) · \(reach)"
    }
}

/// Tapping a `**Name:**` label in a transcript: finding the label and the
/// paragraph's index, and building the request that records the answer.
public enum Reassign {
    /// How older transcripts label a stranger; Remove name now gives an unnamed "Speaker X" instead.
    public static let someone = "Someone"

    /// What a Remove name row says under its title.
    public static let removeCaption = "Back to an unnamed speaker"

    /// An unnamed speaker tag ("Speaker A" ... "Speaker Z", then "Speaker AA") none of `used`
    /// is (any case); `own` when it already is one and isn't used. the server picks the real one
    /// the same way; this is for the screen tour's inert sample and an answer that doesn't say.
    public static func freshTag(avoiding used: Set<String>, keeping own: String? = nil) -> String {
        let taken = Set(used.map { $0.lowercased() })
        if let own, isUnnamed(own), !taken.contains(own.lowercased()) { return own }
        let letters = (65...90).map { String(UnicodeScalar($0)!) }
        let all = letters.map { "Speaker \($0)" } + letters.flatMap { a in letters.map { "Speaker \(a)\($0)" } }
        return all.first { !taken.contains($0.lowercased()) } ?? "Speaker ?"
    }

    /// Whether a tag is an unnamed speaker's ("Speaker A", "Speaker AB").
    public static func isUnnamed(_ tag: String) -> Bool {
        let parts = tag.split(separator: " ", omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0] == "Speaker", (1...2).contains(parts[1].count) else { return false }
        return parts[1].unicodeScalars.allSatisfy { (65...90).contains($0.value) }
    }

    /// The name as the transcript shows it after a change ("someone" in any case is "Someone").
    public static func shownName(_ name: String) -> String {
        name.lowercased() == someone.lowercased() ? someone : name.trimmingCharacters(in: .whitespaces)
    }

    /// Path of the undo for a saved change (`POST /pendant/feed/reassign/<change_id>/undo`).
    public static func undoPath(changeId: String) -> String {
        let safe = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return "reassign/\(changeId.addingPercentEncoding(withAllowedCharacters: safe) ?? changeId)/undo"
    }

    /// `**Alex:** hello` -> ("Alex", "hello"); nil for a paragraph without a label.
    public static func label(in body: String) -> (tag: String, rest: String)? {
        guard body.hasPrefix("**"), let close = body.range(of: ":**") else { return nil }
        let tag = String(body[body.index(body.startIndex, offsetBy: 2)..<close.lowerBound])
        guard !tag.isEmpty, tag.count <= 40, !tag.contains("*"), !tag.contains("\n") else { return nil }
        return (tag, String(body[close.upperBound...]).trimmingCharacters(in: .whitespaces))
    }

    /// A run of preview text: a speaker's name or the words around it.
    public enum PreviewSegment: Equatable {
        case name(String)
        case words(String)
    }

    /// Splits text holding `**Name:**` labels into names and words, in order, so previews can
    /// show the names as chips. Text without a complete label comes back as one `words` run.
    public static func previewSegments(_ text: String) -> [PreviewSegment] {
        var out: [PreviewSegment] = []
        var words = ""
        var rest = Substring(text)
        func flush() {
            let w = words.trimmingCharacters(in: .whitespacesAndNewlines)
            if !w.isEmpty { out.append(.words(w)) }
            words = ""
        }
        while let open = rest.range(of: "**") {
            let after = rest[open.upperBound...]
            if let close = after.range(of: ":**"),
               case let tag = String(after[..<close.lowerBound]),
               !tag.isEmpty, tag.count <= 40, !tag.contains("*"), !tag.contains("\n") {
                words += rest[..<open.lowerBound]
                flush()
                out.append(.name(tag))
                rest = after[close.upperBound...]
            } else {
                words += rest[..<open.upperBound]
                rest = after
            }
        }
        words += rest
        flush()
        return out
    }

    /// Where each search term (lowercased) occurs in `text`, ignoring case, for highlighting.
    public static func highlightRanges(in text: String, terms: [String]) -> [Range<String.Index>] {
        var out: [Range<String.Index>] = []
        for t in terms where !t.isEmpty {
            var from = text.startIndex
            while let r = text.range(of: t, options: .caseInsensitive, range: from..<text.endIndex) {
                out.append(r)
                from = r.upperBound
            }
        }
        return out
    }

    /// For each line of a conversation's text, its index among the stamped
    /// (`[HH:MM] ...`) paragraphs, or nil for other lines (notes). The server
    /// counts paragraphs the same way.
    public static func paragraphIndexes(lines: [String]) -> [Int?] {
        var next = 0
        return lines.map { line in
            guard isStamped(line) else { return nil }
            defer { next += 1 }
            return next
        }
    }

    static func isStamped(_ line: String) -> Bool {
        guard line.hasPrefix("["), let close = line.firstIndex(of: "]") else { return false }
        let parts = line[line.index(after: line.startIndex)..<close].split(separator: ":", omittingEmptySubsequences: false)
        return parts.count == 2 && (1...2).contains(parts[0].count) && parts[1].count == 2
            && parts.allSatisfy { $0.allSatisfy(\.isNumber) }
    }

    /// (path under /pendant/feed, JSON body). Names go with source "user": the wearer
    /// picked or typed them, which is consent for a voiceprint.
    public static func request(deviceId: String, startMs: Int, scope: ReassignScope, tag: String,
                               paragraph: Int?, name: String, entityId: String? = nil,
                               preview: ReassignPreview? = nil) -> (path: String, body: [String: Any]) {
        var body: [String: Any] = ["scope": scope.rawValue, "tag": tag, "source": "user",
                                   "name": name.trimmingCharacters(in: .whitespaces)]
        if let entityId { body["entity_id"] = entityId }   // the LifeOS person the wearer picked
        if scope == .line, let paragraph { body["paragraph"] = paragraph }
        if scope == .all, let preview { body["ids"] = preview.ids; body["digest"] = preview.digest }
        return ("conversations/\(deviceId)/\(startMs)/speaker", body)
    }

    /// Remove name (`remove: true`) for this line or this conversation; nil for "All past conversations",
    /// which never removes a name.
    public static func removeRequest(deviceId: String, startMs: Int, scope: ReassignScope, tag: String,
                                     paragraph: Int?) -> (path: String, body: [String: Any])? {
        guard scope != .all else { return nil }
        var body: [String: Any] = ["scope": scope.rawValue, "tag": tag, "remove": true]
        if scope == .line, let paragraph { body["paragraph"] = paragraph }
        return ("conversations/\(deviceId)/\(startMs)/speaker", body)
    }

    /// Path (with query) of the size preview for "All past conversations".
    public static func previewPath(deviceId: String, startMs: Int, tag: String, name: String) -> String {
        let safe = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        func esc(_ v: String) -> String { v.addingPercentEncoding(withAllowedCharacters: safe) ?? "" }
        let q = "tag=\(esc(tag))&name=\(esc(name.trimmingCharacters(in: .whitespaces)))&source=user"
        return "conversations/\(deviceId)/\(startMs)/speaker/preview?\(q)"
    }
}

/// The URL a feed request uses: the feed base, the path, and the query kept as its own
/// (already percent-encoded) query, never folded into the path.
public enum FeedURL {
    public static func make(base: URL, path: String) -> URL? {
        let parts = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        guard var c = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return nil }
        let trimmed = c.percentEncodedPath.hasSuffix("/") ? String(c.percentEncodedPath.dropLast()) : c.percentEncodedPath
        // segments may arrive already escaped (a full name: "people/Alex%20Rivera"); keep their
        // escapes instead of escaping the "%" again, which made the server look up "Alex%20Rivera"
        let pathChars = CharacterSet.urlPathAllowed.union(CharacterSet(charactersIn: "%"))
        let rel = parts[0].split(separator: "/").map { $0.addingPercentEncoding(withAllowedCharacters: pathChars) ?? String($0) }
        c.percentEncodedPath = trimmed + "/" + rel.joined(separator: "/")
        c.percentEncodedQuery = parts.count > 1 ? String(parts[1]) : nil
        return c.url
    }
}

/// What "All past conversations" would do (`GET .../speaker/preview`), shown before the wearer confirms.
public struct ReassignPreview: Codable, Equatable, Hashable {
    public struct Sample: Codable, Equatable, Hashable, Identifiable {
        public let date: String
        public let time: String
        public let title: String
        public let deviceId: String
        public let startMs: Int
        public let label: String
        public let similarity: Double
        public let excerpt: String
        public let words: Int
        /// Stamped-paragraph indexes the relabel touches (the lines to mark).
        public let paragraphs: [Int]
        public var id: String { "\(deviceId)|\(startMs)" }

        enum CodingKeys: String, CodingKey {
            case date, time, title, label, similarity, excerpt, words, paragraphs
            case deviceId = "device_id"
            case startMs = "start_ms"
        }
    }
    public let conversations: Int
    public let days: Int
    public let words: Int
    public let skipped: Int?
    /// The matched voice ids and their digest: confirming sends both back, and the server
    /// applies only if it still matches exactly these voices.
    public let ids: [Int]
    public let digest: String
    public let sample: [Sample]

    public init(conversations: Int, days: Int, words: Int, skipped: Int? = nil, ids: [Int] = [],
                digest: String = "", sample: [Sample]) {
        self.conversations = conversations; self.days = days; self.words = words
        self.skipped = skipped; self.ids = ids; self.digest = digest; self.sample = sample
    }

    public var isEmpty: Bool { conversations == 0 }

    /// "312 words in 14 conversations over 6 days".
    public var summary: String {
        "\(Self.count(words, "word")) in \(Self.count(conversations, "conversation")) over \(Self.count(days, "day"))"
    }
    /// The confirm button: it carries the word and day counts.
    public var confirmTitle: String { "Relabel \(Self.count(words, "word")) over \(Self.count(days, "day"))" }

    static func count(_ n: Int, _ noun: String) -> String { "\(n) \(noun)\(n == 1 ? "" : "s")" }

    /// The sample grouped by day, newest day first, best match first within a day.
    public var sampleByDay: [(date: String, items: [Sample])] {
        let days = Array(Set(sample.map(\.date))).sorted(by: >)
        return days.map { d in
            (d, sample.filter { $0.date == d }.sorted { $0.similarity > $1.similarity })
        }
    }
}

/// Labels the app shows before the server's copy of a transcript agrees. When
/// the processor is busy a reassignment is queued, and a reload still has the old
/// names; these keep the new ones on screen until a reload shows them.
public struct PendingLabels: Equatable {
    struct Rename: Equatable { let from: String; let to: String }
    public private(set) var lines: [Int: String] = [:]     // paragraph index -> name
    private var renames: [Rename] = []

    public init() {}
    public var isEmpty: Bool { lines.isEmpty && renames.isEmpty }

    public mutating func setLine(_ index: Int, to name: String) { lines[index] = name }
    public mutating func renameVoice(_ from: String, to name: String) { renames.append(Rename(from: from, to: name)) }

    /// One recorded answer, enough to take just that answer back (`revert`).
    public struct Change: Equatable {
        enum Kind: Equatable {
            case line(Int, previous: String?)
            case rename(Rename)
        }
        let kind: Kind
        let name: String
    }

    /// Records what a saved answer changes.
    @discardableResult
    public mutating func record(scope: ReassignScope, tag: String, paragraph: Int?, name: String) -> Change {
        let shown = Reassign.shownName(name)
        // "All past conversations" changes this conversation's voice here; the other conversations load fresh.
        if scope == .line, let paragraph {
            let previous = lines[paragraph]
            setLine(paragraph, to: shown)
            return Change(kind: .line(paragraph, previous: previous), name: shown)
        }
        renameVoice(tag, to: shown)
        return Change(kind: .rename(Rename(from: tag, to: shown)), name: shown)
    }

    /// Takes back one recorded answer (after its undo) and leaves every other one in place.
    /// A line goes back to what it showed before, unless a later answer has changed it since.
    public mutating func revert(_ change: Change) {
        switch change.kind {
        case let .line(i, previous):
            guard lines[i] == change.name else { return }
            lines[i] = previous
        case let .rename(r):
            if let at = renames.lastIndex(of: r) { renames.remove(at: at) }
        }
    }

    /// The name to show for a paragraph whose server label is `tag`. Lines
    /// reassigned one by one win; a voice-wide rename skips lines the server
    /// already shows as individually reassigned (`overrides`).
    public func label(index: Int?, tag: String, overrides: Set<Int> = []) -> String {
        if let index, let n = lines[index] { return n }
        if let index, overrides.contains(index) { return tag }
        var out = tag
        for r in renames where r.from == out { out = r.to }
        return out
    }

    /// Drops what a reload's transcript (`serverTags`: paragraph index -> label)
    /// already shows.
    public mutating func reconcile(serverTags: [Int: String], overrides: Set<Int> = []) {
        lines = lines.filter { i, n in serverTags[i]?.lowercased() != n.lowercased() }
        renames = renames.filter { r in
            serverTags.contains { i, t in !overrides.contains(i) && !lines.keys.contains(i) && t == r.from }
        }
    }
}

/// The server's answer to a saved reassignment: the change's id for Undo and the time (epoch
/// seconds) until which it can be undone. Older servers send neither, and then there is no Undo.
/// `name` is the label the transcript now shows (for Remove name, the fresh "Speaker X").
public struct ReassignReceipt: Decodable, Equatable {
    public let changeId: String?
    public let undoUntil: Double?
    public let name: String?
    /// For a name given to this conversation: the sitting it covered and what else it named.
    public let sitting: SittingScope?

    enum CodingKeys: String, CodingKey {
        case changeId = "change_id"
        case undoUntil = "undo_until"
        case name, sitting
    }

    public init(changeId: String?, undoUntil: Double?, name: String? = nil, sitting: SittingScope? = nil) {
        self.changeId = changeId; self.undoUntil = undoUntil; self.name = name; self.sitting = sitting
    }

    /// The change id of the screen tour's inert sample reassignments (its undo sends nothing).
    public static let tourSampleId = "TOURSAMPLE"
    public static func tourSample(now: Date = Date(), name: String? = nil, sitting: SittingScope? = nil) -> ReassignReceipt {
        ReassignReceipt(changeId: tourSampleId, undoUntil: now.timeIntervalSince1970 + 86400, name: name, sitting: sitting)
    }

    /// The voices the answer named in other pieces (empty when none).
    public var otherIds: Set<Int> { Set(sitting?.ids ?? []) }

    /// The id to undo with, or nil when there is none or its window has closed.
    public func undoId(now: Date = Date()) -> String? {
        guard let changeId, !changeId.isEmpty else { return nil }
        if let undoUntil, undoUntil <= now.timeIntervalSince1970 { return nil }
        return changeId
    }
}

/// Why an undo didn't go through, from the server's status (nil: no answer at all).
public struct UndoFailure: Equatable {
    public let message: String
    /// Trying again can help (busy or offline); the other refusals are final.
    public let canRetry: Bool

    public init(status: Int?) {
        switch status {
        case 409: message = "Edited since, so it can't be undone."; canRetry = false
        case 410: message = "Too old to undo."; canRetry = false
        case 404: message = "This change can't be found."; canRetry = false
        case .some(let s) where s >= 500: message = "Busy — retry"; canRetry = true
        case nil: message = "Couldn't reach \(AppSettings.serverName)."; canRetry = true
        default: message = "Couldn't undo."; canRetry = true
        }
    }
}

/// One inline "Changed to <Name> · Undo" (or "Name removed · Undo") row a conversation shows after a reassignment. It
/// goes away by itself at `expires` only while nothing is happening: during its undo, or after
/// one failed, it stays until the undo succeeds or the wearer closes it.
public struct UndoOffer: Equatable, Identifiable {
    public enum Phase: Equatable {
        case offered, undoing
        case failed(UndoFailure)
    }

    public static let shownFor: Double = 10

    public let id = UUID()
    public let name: String
    public let changeId: String
    public let scope: ReassignScope
    /// Transcript row the row sits under (the line that was tapped); nil: at the top.
    public let row: Int?
    /// What the change added to the app's pending labels: an undo takes just that back.
    public let change: PendingLabels.Change
    public let expires: Date
    /// The change was Remove name (`name` is then the unnamed tag it got).
    public let removed: Bool
    /// What else the change named ("Also named in 3 earlier parts"); one Undo takes it all back.
    public let detail: String?
    public var phase: Phase = .offered

    public init(name: String, changeId: String, scope: ReassignScope, row: Int?, change: PendingLabels.Change,
                removed: Bool = false, detail: String? = nil, now: Date = Date(),
                shownFor: Double = UndoOffer.shownFor) {
        self.name = Reassign.shownName(name); self.changeId = changeId; self.scope = scope
        self.row = row; self.change = change; self.removed = removed; self.detail = detail
        self.expires = now.addingTimeInterval(shownFor)
    }

    public var title: String { removed ? "Name removed" : "Changed to \(name)" }
    public var dismissesItself: Bool { phase == .offered }

    /// Starts an undo. False when one is already running or the last failure can't be retried.
    public mutating func begin() -> Bool {
        switch phase {
        case .undoing: return false
        case .failed(let f) where !f.canRetry: return false
        default: phase = .undoing; return true
        }
    }

    public mutating func fail(status: Int?) { phase = .failed(UndoFailure(status: status)) }
}

/// Every Undo row a conversation is showing, one per recent change, oldest first. Each keeps
/// its own timer and its own undo state.
public struct UndoOffers: Equatable {
    public private(set) var all: [UndoOffer] = []
    public init() {}

    public var isEmpty: Bool { all.isEmpty }

    public mutating func add(_ offer: UndoOffer) { all.append(offer) }

    /// Drops the rows whose time is up and that aren't undoing or showing a failure.
    public mutating func expire(now: Date = Date()) {
        all.removeAll { $0.dismissesItself && $0.expires <= now }
    }

    /// When the next row would go away by itself (nil: none will).
    public var nextExpiry: Date? { all.filter(\.dismissesItself).map(\.expires).min() }

    /// Starts undoing `id`; the offer to undo, or nil when it's gone, already undoing or can't retry.
    public mutating func begin(_ id: UUID) -> UndoOffer? {
        guard let i = all.firstIndex(where: { $0.id == id }), all[i].begin() else { return nil }
        return all[i]
    }

    public mutating func fail(_ id: UUID, status: Int?) {
        guard let i = all.firstIndex(where: { $0.id == id }) else { return }
        all[i].fail(status: status)
    }

    /// Removes a row (its undo went through, or the wearer closed it).
    public mutating func remove(_ id: UUID) { all.removeAll { $0.id == id } }

    /// The rows under transcript row `row` (nil: the ones shown at the top).
    public func at(_ row: Int?) -> [UndoOffer] { all.filter { $0.row == row } }
}
