import Foundation

/// Folds a day's unstarred fragments and media into one "N short clips hidden" row.
/// Pure logic over the day's rows (newest first, as listed); `DayView` renders the result.
public enum JunkFold {
    /// A row of the day list: a recording (by id) or the fold toggle.
    public enum Entry: Equatable, Identifiable {
        case item(String)
        /// A folded clip shown grouped under the toggle.
        case clip(String)
        /// The "N short clips hidden" / "Hide N short clips" row; `count` is the clips it covers.
        case toggle(count: Int, expanded: Bool)

        public var id: String {
            switch self {
            case let .item(id): "item-\(id)"
            case let .clip(id): "clip-\(id)"
            case .toggle: "short-clips-toggle"
            }
        }
    }

    /// One recording as the fold sees it.
    public struct Row: Equatable {
        public let id: String
        /// Unstarred fragment or media (starred and notable recordings are never junk).
        public let isJunk: Bool
        public init(id: String, isJunk: Bool) { self.id = id; self.isJunk = isJunk }
    }

    /// Whether a recording folds: it needs review and carries no star of any kind.
    public static func isJunk(kind: RecordingKind, starCount: Int, appStar: Bool, notable: Bool) -> Bool {
        kind.needsReview && starCount == 0 && !appStar && !notable
    }

    /// The day list. Collapsed: junk rows are dropped and one toggle sits where the first junk
    /// row was. Expanded: the toggle sits there too, followed by every junk row grouped directly
    /// under it (`.clip`, in the given order); the other rows keep their places.
    /// No junk, no toggle. `rows` keep their given order.
    public static func entries(_ rows: [Row], expanded: Bool) -> [Entry] {
        let junk = rows.filter(\.isJunk)
        guard !junk.isEmpty else { return rows.map { .item($0.id) } }
        var out: [Entry] = []
        var placed = false
        for row in rows {
            if row.isJunk {
                if !placed {
                    out.append(.toggle(count: junk.count, expanded: expanded)); placed = true
                    if expanded { out += junk.map { .clip($0.id) } }
                }
            } else {
                out.append(.item(row.id))
            }
        }
        return out
    }

    /// "3 hidden · Show" while folded, "Hide" while open.
    public static func label(count: Int, expanded: Bool) -> String {
        expanded ? "Hide" : "\(count) hidden · Show"
    }
}
