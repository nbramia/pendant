import Foundation

/// What the server thinks a recording is (`kind` in the day feed). Media and
/// fragments are dimmed and badged, and fold into a day's hidden-clips row; nothing is
/// ever deleted automatically.
public enum RecordingKind: String {
    case conversation, media, fragment

    /// Unknown or missing values (older servers, older rows) read as a conversation.
    public init(feed raw: String?) { self = raw.flatMap(RecordingKind.init(rawValue:)) ?? .conversation }

    /// Badge text; nil for a conversation.
    public var badge: String? {
        switch self {
        case .conversation: nil
        case .media: "Media"
        case .fragment: "Fragment"
        }
    }

    /// Folds into the hidden-clips row unless starred.
    public var needsReview: Bool { self != .conversation }
}
