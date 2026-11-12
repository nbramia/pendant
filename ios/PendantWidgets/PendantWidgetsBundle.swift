import SwiftUI
import WidgetKit

@main
struct PendantWidgetsBundle: WidgetBundle {
    var body: some Widget {
        PendantStatusWidget()
        PendantLiveActivity()
    }
}

/// Shared look for the glance surfaces (matches the app's Theme).
enum GlanceStyle {
    static let teal = Color(red: 0.43, green: 0.86, blue: 0.90)
    static let night = LinearGradient(colors: [Color(red: 0.10, green: 0.13, blue: 0.25),
                                               Color(red: 0.03, green: 0.04, blue: 0.10)],
                                      startPoint: .top, endPoint: .bottom)

    static func tint(_ phase: PendantPhase) -> Color {
        switch phase {
        case .listening, .syncing: teal
        case .quiet, .connected, .connecting: .white.opacity(0.8)
        case .notConnected: .orange
        }
    }

    static func batterySymbol(_ b: Int) -> String {
        b > 85 ? "battery.100" : b > 60 ? "battery.75" : b > 35 ? "battery.50" : b > 10 ? "battery.25" : "battery.0"
    }

    static func minutes(_ m: Int) -> String {
        m < 60 ? "\(m) min" : "\(m / 60)h \(m % 60)m"
    }
}

struct BatteryLabel: View {
    let battery: Int?
    var body: some View {
        if let b = battery {
            Label("\(b)%", systemImage: GlanceStyle.batterySymbol(b))
                .monospacedDigit()
                .foregroundStyle(b <= 15 ? .red : .secondary)
        }
    }
}
