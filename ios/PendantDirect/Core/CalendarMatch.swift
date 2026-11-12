import Foundation

/// A conversation's calendar match from the day feed (`calendar`): the event that best
/// overlaps it, with its attendees resolved to LifeOS people where possible. `shared` marks an
/// event that came from another part of the same meeting (a meeting split by the 30-minute cap).
public struct CalendarMatch: Codable, Equatable, Hashable {
    public struct Attendee: Codable, Equatable, Hashable {
        public let name: String
        public let email: String?
        public let entityId: String?
        /// The wearer (the wearer himself).
        public let wearer: Bool?

        enum CodingKeys: String, CodingKey {
            case name, email, wearer
            case entityId = "entity_id"
        }

        public init(name: String, email: String? = nil, entityId: String? = nil, wearer: Bool? = nil) {
            self.name = name; self.email = email; self.entityId = entityId; self.wearer = wearer
        }
    }

    public let title: String
    public let startMs: Int?
    public let endMs: Int?
    public let attendees: [Attendee]
    public let shared: Bool?

    enum CodingKeys: String, CodingKey {
        case title, attendees, shared
        case startMs = "start_ms"
        case endMs = "end_ms"
    }

    public init(title: String, startMs: Int? = nil, endMs: Int? = nil, attendees: [Attendee] = [], shared: Bool? = nil) {
        self.title = title; self.startMs = startMs; self.endMs = endMs; self.attendees = attendees; self.shared = shared
    }

    /// Everyone on the invite except the wearer, by full name.
    public var others: [String] { attendees.filter { $0.wearer != true }.map(\.name) }

    /// "Morgan-the wearer week-ahead prep · Morgan Hale": the line the conversation view shows.
    public var line: String {
        others.isEmpty ? title : "\(title) · \(others.joined(separator: ", "))"
    }
}

/// A Granola/Wispr note linked to a conversation (the day feed's `meetings`).
public struct MeetingLink: Codable, Equatable, Hashable {
    public let note: String
    public let source: String
    /// Only another part of the same meeting overlaps this note (it didn't record this part).
    public let shared: Bool?

    public init(note: String, source: String, shared: Bool? = nil) {
        self.note = note; self.source = source; self.shared = shared
    }

    /// "Also recorded in Granola:" or "Same meeting in Granola:" (the note's name follows).
    public var lead: String { "\(shared == true ? "Same meeting in" : "Also recorded in") \(app):" }

    /// "Also recorded in Granola: <name>" or "Same meeting in Granola: <name>".
    public var title: String { "\(lead) \(name)" }

    /// "Wispr" / "Granola" (or "a meeting").
    public var app: String { source.isEmpty ? "a meeting" : source.prefix(1).uppercased() + source.dropFirst() }

    /// The note's name without its vault folders.
    public var name: String { String(note.split(separator: "/").last ?? Substring(note)) }

    /// Opens the note in Obsidian.
    public var url: URL? {
        guard let vault = AppSettings.obsidianVault else { return nil }
        var c = URLComponents(string: "obsidian://open")
        c?.queryItems = [URLQueryItem(name: "vault", value: vault), URLQueryItem(name: "file", value: note)]
        return c?.url
    }
}

/// Who a voice probably is, from the conversation's calendar event: the one attendee left
/// after the wearer and the people already named in the conversation. Only a suggestion:
/// confirming it is an ordinary hand label linked to `entityId`.
public struct LikelyPerson: Codable, Equatable, Hashable {
    /// The full name ("Morgan Hale").
    public let name: String
    /// The speaker name a confirm saves (the full name, or the name the person is linked under).
    public let label: String
    public let entityId: String
    /// The calendar event's title.
    public let event: String

    enum CodingKeys: String, CodingKey {
        case name, label, event
        case entityId = "entity_id"
    }

    public init(name: String, label: String? = nil, entityId: String, event: String) {
        self.name = name; self.label = label ?? name; self.entityId = entityId; self.event = event
    }

    public var prompt: String { "Probably \(name)" }
    public var source: String { "from \(event)" }
    public var confirmTitle: String { "Yes, \(name)" }
}

/// `likely` and `candidates` for one voice (the Voice Scoring / Who's this cards carry the same
/// two fields; the reassign sheet asks `Suggestions.hintPath`).
public struct CalendarHint: Codable, Equatable {
    public let likely: LikelyPerson?
    public let candidates: [PersonSuggestion]

    enum CodingKeys: String, CodingKey { case likely, candidates }

    public init(likely: LikelyPerson? = nil, candidates: [PersonSuggestion] = []) {
        self.likely = likely; self.candidates = candidates
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        likely = try c.decodeIfPresent(LikelyPerson.self, forKey: .likely)
        candidates = try c.decodeIfPresent([PersonSuggestion].self, forKey: .candidates) ?? []
    }

    public static let none = CalendarHint()

    /// Screen tour only (debug builds): an inert sample, so the confirm can be reviewed.
    public static let tourSample = CalendarHint(likely: LikelyPerson(name: "Sample Attendee", entityId: "tour-sample",
                                                                     event: "Sample week-ahead prep"))
}

extension Suggestions {
    /// Path of the calendar suggestion for the voice tagged `tag` in a conversation.
    public static func hintPath(deviceId: String, startMs: Int, tag: String) -> String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/?#"))
        return "conversations/\(deviceId)/\(startMs)/speaker/\(tag.addingPercentEncoding(withAllowedCharacters: allowed) ?? tag)/suggest"
    }

    /// The picker rows with the event's remaining attendees first (those matching what was
    /// typed), each person once.
    public static func leading(_ rows: [PersonSuggestion], with lead: [PersonSuggestion], query: String = "") -> [PersonSuggestion] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        let first = lead.filter { q.isEmpty || $0.name.lowercased().contains(q) || $0.label.lowercased().contains(q) }
        guard !first.isEmpty else { return rows }
        let ids = Set(first.compactMap(\.entityId))
        let labels = Set(first.map { $0.label.lowercased() })
        return first + rows.filter { r in
            !(r.entityId.map(ids.contains) ?? false) && (r.isNew || !labels.contains(r.label.lowercased()))
        }
    }
}
