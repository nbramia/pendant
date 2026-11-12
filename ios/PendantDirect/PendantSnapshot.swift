import Foundation

/// What the app remembers about one pendant for display.
struct PendantSnapshot: Codable, Identifiable, Equatable {
    let id: String              // pendant hex ID (16 hex digits)
    var lastSeen: Date?
    var lastAudio: Date?
    var battery: Int?
    var freePages: Int?
    var totalPages: Int?
    var firmware: String?

    init(id: String) { self.id = id }

    /// Friendly name: the wearer's names for his pendants, else the last four hex digits.
    var name: String { Self.displayName(id) }

    static let knownNames = AppSettings.pendantNames

    static func displayName(_ idOrName: String) -> String {
        if let n = knownNames[idOrName] { return "\(n.capitalized) pendant" }
        if idOrName.count == 16, idOrName.allSatisfy(\.isHexDigit) { return "Pendant ·\(idOrName.suffix(4))" }
        return "\(idOrName.capitalized) pendant"
    }

    /// Fraction of flash in use (pages waiting to sync), if known.
    var storageUsed: Double? {
        guard let f = freePages, let t = totalPages, t > 0 else { return nil }
        return 1 - Double(f) / Double(t)
    }

    private static let key = "pendantSnapshots"

    static func load() -> [String: PendantSnapshot] {
        guard let data = UserDefaults.standard.data(forKey: key),
              let list = try? JSONDecoder().decode([PendantSnapshot].self, from: data) else { return [:] }
        return Dictionary(uniqueKeysWithValues: list.map { ($0.id, $0) })
    }

    static func save(_ map: [String: PendantSnapshot]) {
        if let data = try? JSONEncoder().encode(Array(map.values)) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }
}
