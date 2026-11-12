import ActivityKit
import SwiftUI
import WidgetKit

/// Lock-screen Live Activity and Dynamic Island, updated by the app's
/// LiveActivityController from its Bluetooth events.
struct PendantLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: PendantActivityAttributes.self) { context in
            LockScreenView(status: context.state, stale: context.isStale)
                .padding(16)
                .activityBackgroundTint(Color(red: 0.05, green: 0.07, blue: 0.15))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            let s = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label(s.phase.title, systemImage: s.phase.symbol)
                        .font(.headline)
                        .foregroundStyle(GlanceStyle.tint(s.phase))
                }
                DynamicIslandExpandedRegion(.trailing) {
                    BatteryLabel(battery: s.battery).font(.subheadline)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text(s.pendantName ?? "Pendant").foregroundStyle(.secondary)
                        Spacer()
                        Text("\(GlanceStyle.minutes(s.minutesToday(asOf: Date()))) today").monospacedDigit()
                    }
                    .font(.subheadline)
                }
            } compactLeading: {
                Image(systemName: s.phase.symbol).foregroundStyle(GlanceStyle.tint(s.phase))
            } compactTrailing: {
                Text("\(s.minutesToday(asOf: Date()))m").monospacedDigit().foregroundStyle(GlanceStyle.tint(s.phase))
            } minimal: {
                Image(systemName: s.phase.symbol).foregroundStyle(GlanceStyle.tint(s.phase))
            }
        }
    }
}

private struct LockScreenView: View {
    let status: PendantStatus
    let stale: Bool

    /// Past its stale date a disconnected activity has hit its 30-minute end
    /// (the app may not have run to remove it); a connected one missed updates.
    private var title: String {
        guard stale else { return status.phase.title }
        return status.phase == .notConnected ? "Pendant disconnected" : "\(status.phase.title) (not updated lately)"
    }

    var body: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle().fill(GlanceStyle.tint(status.phase).opacity(0.2))
                Image(systemName: status.phase.symbol).font(.title2).foregroundStyle(GlanceStyle.tint(status.phase))
            }
            .frame(width: 48, height: 48)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(status.pendantName ?? "Pendant").font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(GlanceStyle.minutes(status.minutesToday(asOf: Date()))).font(.headline).monospacedDigit()
                Text("heard today").font(.caption2).foregroundStyle(.secondary)
                BatteryLabel(battery: status.battery).font(.caption)
            }
        }
        .foregroundStyle(.white)
    }
}
