import Foundation

/// One row of the name picker (`GET /pendant/feed/people/suggest?q=`): a LifeOS person, a known
/// voice, or (last) the typed text as a new name. A LifeOS person is only a suggestion; picking
/// one is what links the name.
public struct PersonSuggestion: Codable, Equatable, Hashable, Identifiable {
    /// The full name shown in the row.
    public let name: String
    /// The speaker name picking this row gives: the LifeOS full name, or the name the person
    /// is already linked under. Everything that saves a pick sends this.
    public let label: String
    public let entityId: String?
    /// Dunbar circle in LifeOS (0 is closest), when known.
    public let circle: Int?
    public let category: String?
    public let strength: Double?
    /// A voiceprint exists for this name.
    public let voiceprint: Bool
    /// An enrolled or allowed name (as opposed to a LifeOS person not yet picked).
    public let known: Bool
    /// The typed text, offered as a new name (always the last row).
    public let isNew: Bool
    /// Company or email domain: tells two people of one name apart.
    public let detail: String?

    public var id: String { isNew ? "new:\(label.lowercased())" : entityId ?? "name:\(label.lowercased())" }

    enum CodingKeys: String, CodingKey {
        case name, label, circle, category, strength, voiceprint, known, detail
        case entityId = "entity_id"
        case isNew = "new"
    }

    public init(name: String, label: String? = nil, entityId: String? = nil, circle: Int? = nil, category: String? = nil,
                strength: Double? = nil, voiceprint: Bool = false, known: Bool = false, isNew: Bool = false,
                detail: String? = nil) {
        self.name = name; self.label = label ?? name; self.entityId = entityId; self.circle = circle
        self.category = category; self.strength = strength; self.voiceprint = voiceprint; self.known = known
        self.isNew = isNew; self.detail = detail
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let name = try c.decode(String.self, forKey: .name)
        self.init(name: name, label: try c.decodeIfPresent(String.self, forKey: .label),
                  entityId: try c.decodeIfPresent(String.self, forKey: .entityId),
                  circle: try c.decodeIfPresent(Int.self, forKey: .circle),
                  category: try c.decodeIfPresent(String.self, forKey: .category),
                  strength: try c.decodeIfPresent(Double.self, forKey: .strength),
                  voiceprint: try c.decodeIfPresent(Bool.self, forKey: .voiceprint) ?? false,
                  known: try c.decodeIfPresent(Bool.self, forKey: .known) ?? false,
                  isNew: try c.decodeIfPresent(Bool.self, forKey: .isNew) ?? false,
                  detail: try c.decodeIfPresent(String.self, forKey: .detail))
    }

    /// "Circle 0", or nil when LifeOS gave none.
    public var circleBadge: String? { circle.map { "Circle \($0)" } }

    /// "voice saved · Acme · family" style hint under the name.
    public var subtitle: String {
        var parts: [String] = []
        if voiceprint { parts.append("voice saved") } else if known { parts.append("no voice yet") }
        if let detail, !detail.isEmpty { parts.append(detail) }
        if let category, !category.isEmpty { parts.append(category) }
        return parts.joined(separator: " · ")
    }
}

public struct SuggestionResponse: Codable, Equatable {
    public let people: [PersonSuggestion]
    /// False when LifeOS couldn't answer and only known voices are listed.
    public let lifeos: Bool
}

public enum Suggestions {
    /// Path (with query) of the suggestions for what has been typed so far.
    public static func path(_ query: String) -> String {
        let safe = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        let q = query.trimmingCharacters(in: .whitespaces)
        return "people/suggest?q=\(q.addingPercentEncoding(withAllowedCharacters: safe) ?? "")"
    }

    /// Rows to show when the server can't be asked: the plain known names, then the typed text
    /// as a new name. Nothing is linked.
    public static func offline(_ names: [String], matching query: String) -> [PersonSuggestion] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        var rows = names.filter { q.isEmpty || $0.lowercased().contains(q) }.map { PersonSuggestion(name: $0, known: true) }
        if let typed = VoiceSampleFlow.clean(query), !rows.contains(where: { $0.label.lowercased() == typed.lowercased() }) {
            rows.append(PersonSuggestion(name: typed, isNew: true))
        }
        return rows
    }

    /// The row the keyboard's submit key picks: the first row, only when it is the person typed
    /// exactly (its name or speaker name, ignoring case). Nothing else is chosen by submitting.
    public static func exactFirst(_ rows: [PersonSuggestion], query: String) -> PersonSuggestion? {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty, let first = rows.first, !first.isNew else { return nil }
        return first.name.lowercased() == q || first.label.lowercased() == q ? first : nil
    }

    /// Path of the relink action for a speaker name (`POST`, body `{"entity_id": ...}`).
    public static func linkPath(_ name: String) -> String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        return "people/\(name.addingPercentEncoding(withAllowedCharacters: allowed) ?? name)/link"
    }

    /// What the relink picker asks for: what was typed, else the speaker's own name (its LifeOS matches).
    public static func relinkQuery(_ typed: String, name: String) -> String {
        typed.trimmingCharacters(in: .whitespaces).isEmpty ? name : typed
    }

    /// "LifeOS: Alex Rivera · circle 0" or "Not linked", for the People view.
    public static func linkLine(entityId: String?, name: String?, circle: Int?) -> String {
        guard entityId != nil else { return "Not linked" }
        guard let name else { return "LifeOS: linked" }
        return "LifeOS: \(name)" + (circle.map { " · circle \($0)" } ?? "")
    }
}

/// The answer to the relink action.
public struct PersonLinkResult: Codable, Equatable {
    public let name: String
    public let entityId: String
    public let lifeosName: String?
    public let lifeosCircle: Int?
    public let lifeosCategory: String?

    enum CodingKeys: String, CodingKey {
        case name
        case entityId = "entity_id"
        case lifeosName = "lifeos_name"
        case lifeosCircle = "lifeos_circle"
        case lifeosCategory = "lifeos_category"
    }
}
