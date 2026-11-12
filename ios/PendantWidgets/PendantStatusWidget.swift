import SwiftUI
import WidgetKit

/// Home-screen widget: the last state the app wrote to the app group.
struct PendantStatusWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: PendantWidgetKind.status, provider: StatusProvider()) { entry in
            StatusWidgetView(entry: entry)
                .containerBackground(for: .widget) { GlanceStyle.night }
        }
        .configurationDisplayName("Pendant")
        .description("Whether your pendant is listening, its battery and minutes heard today.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct StatusEntry: TimelineEntry {
    let date: Date
    let status: PendantStatus?
}

struct StatusProvider: TimelineProvider {
    func placeholder(in context: Context) -> StatusEntry {
        StatusEntry(date: Date(), status: PendantStatus(phase: .listening, pendantName: "New pendant",
                                                        battery: 80, minutesToday: 42))
    }

    func getSnapshot(in context: Context, completion: @escaping (StatusEntry) -> Void) {
        completion(context.isPreview ? placeholder(in: context) : current())
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<StatusEntry>) -> Void) {
        // The app reloads us on every visible change; the periodic refresh only
        // matters for rolling "minutes today" over at midnight.
        let now = Date()
        let midnight = Calendar.current.startOfDay(for: now).addingTimeInterval(86_400 + 60)
        let next = min(now.addingTimeInterval(30 * 60), midnight)
        completion(Timeline(entries: [current(), StatusEntry(date: midnight, status: SharedStatusStore().loadStatus())],
                            policy: .after(next)))
    }

    private func current() -> StatusEntry {
        StatusEntry(date: Date(), status: SharedStatusStore().loadStatus())
    }
}

struct StatusWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: StatusEntry

    private var phase: PendantPhase { entry.status?.phase ?? .notConnected }
    private var minutes: Int { entry.status?.minutesToday(asOf: entry.date) ?? 0 }

    var body: some View {
        switch family {
        case .systemMedium: medium
        default: small
        }
    }

    private var small: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: phase.symbol)
                .font(.title2)
                .foregroundStyle(GlanceStyle.tint(phase))
            Text(phase.title).font(.headline)
            Text(entry.status?.pendantName ?? "No pendant yet")
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
            HStack {
                Text(GlanceStyle.minutes(minutes)).font(.caption.weight(.semibold)).monospacedDigit()
                Spacer()
                BatteryLabel(battery: entry.status?.battery).font(.caption2).labelStyle(.titleAndIcon)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .foregroundStyle(.white)
    }

    private var medium: some View {
        HStack(spacing: 16) {
            ZStack {
                Circle().fill(GlanceStyle.tint(phase).opacity(0.18))
                Image(systemName: phase.symbol).font(.largeTitle).foregroundStyle(GlanceStyle.tint(phase))
            }
            .frame(width: 84, height: 84)
            VStack(alignment: .leading, spacing: 4) {
                Text(phase.title).font(.title3.weight(.semibold))
                Text(entry.status?.pendantName ?? "No pendant yet").font(.subheadline).foregroundStyle(.secondary)
                BatteryLabel(battery: entry.status?.battery).font(.caption)
                Spacer(minLength: 0)
                Text("\(GlanceStyle.minutes(minutes)) heard today").font(.caption.weight(.semibold)).monospacedDigit()
                if let updated = entry.status?.updatedAt {
                    Text("Updated \(updated, style: .relative) ago").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(.white)
    }
}
