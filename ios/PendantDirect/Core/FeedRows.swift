import Foundation

/// Feed requests for one conversation (paths under /pendant/feed).
public enum RowRequest {
    public static func path(device: String, startMs: Int, _ verb: String) -> String {
        "conversations/\(device)/\(startMs)/\(verb)"
    }

    /// Leading swipe toggles the app star; button-press stars are the server's and stay.
    public static func starBody(appStarred: Bool) -> [String: Bool] { ["on": !appStarred] }
}

/// How long a deleted row offers Undo in place.
public enum UndoWindow {
    public static let seconds: Double = 5
}

/// One entry of `GET /pendant/feed/trash`.
public struct TrashEntry: Codable, Identifiable, Hashable {
    public let id: String
    public let deviceId: String
    public let startMs: Int
    public let day: String
    public let title: String?
    public let device: String?
    public let durationS: Int?
    public let deletedAt: Double
    public let purgeAt: Double

    enum CodingKeys: String, CodingKey {
        case id, day, title, device
        case deviceId = "device_id"
        case startMs = "start_ms"
        case durationS = "duration_s"
        case deletedAt = "deleted_at"
        case purgeAt = "purge_at"
    }

    public init(id: String, deviceId: String, startMs: Int, day: String, title: String? = nil, device: String? = nil,
                durationS: Int? = nil, deletedAt: Double, purgeAt: Double) {
        self.id = id; self.deviceId = deviceId; self.startMs = startMs; self.day = day; self.title = title
        self.device = device; self.durationS = durationS; self.deletedAt = deletedAt; self.purgeAt = purgeAt
    }

    public var start: Date { Date(timeIntervalSince1970: Double(startMs) / 1000) }

    /// Whole days until it's gone for good (at least 0).
    public func daysLeft(now: Date = Date()) -> Int {
        max(0, Int((purgeAt - now.timeIntervalSince1970) / 86400 + 0.999))
    }

    public func daysLeftLabel(now: Date = Date()) -> String {
        let d = daysLeft(now: now)
        return d == 0 ? "Removed today" : d == 1 ? "1 day left" : "\(d) days left"
    }
}

public struct TrashFeed: Codable { public let trash: [TrashEntry] }
