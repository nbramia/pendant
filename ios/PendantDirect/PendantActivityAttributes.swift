import ActivityKit
import Foundation

/// The Live Activity's type. Compiled into the app (which starts and updates it)
/// and the PendantWidgets extension (which draws it). Everything dynamic lives
/// in `PendantStatus`, so there are no static attributes.
struct PendantActivityAttributes: ActivityAttributes {
    typealias ContentState = PendantStatus
}

/// WidgetKit kind of the home-screen widget, for `WidgetCenter` reloads.
enum PendantWidgetKind {
    static let status = "PendantStatusWidget"
}
