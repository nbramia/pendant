import Foundation

/// Per-setup values from `Secrets.xcconfig` (through Info.plist), so nothing about one person's
/// machines is compiled in. Missing or empty values fall back to neutral defaults.
public enum AppSettings {
    /// What the app calls the server in its messages (`PENDANT_SERVER_NAME`, e.g. a hostname).
    public static var serverName: String { value("PendantServerName") ?? "the server" }

    /// serverName at the start of a sentence: a configured name as written (hostnames keep their
    /// case), the default capitalized.
    public static var serverNameAtStart: String { value("PendantServerName") ?? "The server" }

    /// The Obsidian vault holding the notes (`PENDANT_OBSIDIAN_VAULT`); nil: no "open in Obsidian" links.
    public static var obsidianVault: String? { value("PendantObsidianVault") }

    /// Friendly pendant names by device ID (`PENDANT_NAMES`, "HEX=name,HEX=name").
    public static var pendantNames: [String: String] { parseNames(value("PendantNames") ?? "") }

    public static func parseNames(_ s: String) -> [String: String] {
        var out: [String: String] = [:]
        for pair in s.split(separator: ",") {
            let kv = pair.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
            if kv.count == 2, !kv[0].isEmpty, !kv[1].isEmpty { out[kv[0].uppercased()] = kv[1] }
        }
        return out
    }

    private static func value(_ key: String) -> String? {
        guard let s = Bundle.main.object(forInfoDictionaryKey: key) as? String else { return nil }
        let t = s.trimmingCharacters(in: .whitespaces)
        return t.isEmpty || t.hasPrefix("$(") ? nil : t
    }
}

