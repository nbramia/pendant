import ActivityKit
import Foundation
import WidgetKit

/// Mirrors PendantManager's state onto the lock-screen Live Activity, the
/// Dynamic Island and the home-screen widget (through the app group).
///
/// Driven entirely by the app's own Bluetooth events (no push server):
/// - `heard(pages:)` counts audio pages for "minutes heard today";
/// - `refresh(...)` runs from `sessionsChanged()` (every state change and the
///   90 s periodic tick) and starts, updates or ends the activity.
@MainActor
final class LiveActivityController {
    private let store = SharedStatusStore()
    private let policy = LiveActivityPolicy()
    private var counter: DailyHeardCounter
    private var activity: Activity<PendantActivityAttributes>?
    private var lastPushed: PendantStatus?
    private var lastPushedAt: Date?
    private var lastConnectedAt: Date?
    private var lastWidgetStatus: PendantStatus?
    private var lastWidgetReload: Date?
    private var startFailedAt: Date?
    private let log: (String) -> Void

    init(log: @escaping (String) -> Void) {
        self.log = log
        counter = store.loadCounter()
        lastWidgetStatus = store.loadStatus()
        // Adopt an activity left over from a previous launch (iOS keeps them
        // across app restarts). The persisted connect time decides whether it
        // has already outlived its 30 minutes; the first refresh ends it if so.
        lastConnectedAt = store.loadLastConnectedAt()
        activity = Activity<PendantActivityAttributes>.activities.first
        if let activity { lastPushed = activity.content.state }
    }

    /// Audio pages stored (live or drained); call once per stored page batch.
    func heard(pages: Int, at date: Date = Date()) {
        guard pages > 0 else { return }
        counter.add(pages, at: date)
        store.save(counter)
    }

    func refresh(appState: String, pendantName: String?, battery: Int?, connected: Bool) {
        let now = Date()
        // Keep showing the last pendant we knew about once it disconnects.
        let name = pendantName ?? lastWidgetStatus?.pendantName
        let bat = pendantName == nil ? (battery ?? lastWidgetStatus?.battery) : battery
        let status = PendantStatus(appState: appState, pendantName: name, battery: bat,
                                   minutesToday: counter.minutes(at: now), now: now)
        if connected {
            lastConnectedAt = now
            store.saveLastConnectedAt(now)
        }
        updateWidget(status, now: now)
        updateActivity(status, connected: connected, now: now)
    }

    // MARK: Widget

    private func updateWidget(_ status: PendantStatus, now: Date) {
        store.save(status)
        // Reload on a visible change; minute-only changes at most every 5 min
        // (the widget's reload budget is limited while we're in the background).
        let minuteChange = status.minutesToday != lastWidgetStatus?.minutesToday
        let due = lastWidgetReload.map { now.timeIntervalSince($0) >= 300 } ?? true
        if status.differsVisibly(from: lastWidgetStatus) || (minuteChange && due) {
            WidgetCenter.shared.reloadTimelines(ofKind: PendantWidgetKind.status)
            lastWidgetReload = now
        }
        lastWidgetStatus = status
    }

    // MARK: Live Activity

    private func updateActivity(_ status: PendantStatus, connected: Bool, now: Date) {
        if let activity, activity.activityState == .ended || activity.activityState == .dismissed {
            self.activity = nil
            lastPushed = nil
        }
        if !connected, policy.shouldEnd(lastConnectedAt: lastConnectedAt, now: now) {
            end(status)
            return
        }
        guard let activity else {
            if connected { start(status, now: now) }
            return
        }
        guard policy.shouldUpdate(last: lastPushed, lastUpdateAt: lastPushedAt, next: status,
                                  connected: connected, now: now) else { return }
        lastPushed = status
        lastPushedAt = now
        let content = content(status, connected: connected, now: now)
        Task { await activity.update(content) }
    }

    private func start(_ status: PendantStatus, now: Date) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        // Starting only works while the app is in the foreground; when it fails in
        // the background, retry on a later refresh (at most every 5 min).
        if let failed = startFailedAt, now.timeIntervalSince(failed) < 300 { return }
        do {
            activity = try Activity.request(attributes: PendantActivityAttributes(),
                                            content: content(status, connected: true, now: now), pushType: nil)
            lastPushed = status
            lastPushedAt = now
            startFailedAt = nil
            log("live activity started")
        } catch {
            startFailedAt = now
            log("live activity start failed: \(error.localizedDescription)")
        }
    }

    private func end(_ status: PendantStatus) {
        guard let activity else { return }
        self.activity = nil
        lastPushed = nil
        lastPushedAt = nil
        log("live activity ended: no pendant for 30 min")
        Task { await activity.end(ActivityContent(state: status, staleDate: nil), dismissalPolicy: .immediate) }
    }

    /// While connected the stale date rolls forward with each (keep-alive)
    /// update; once disconnected it's the 30-minute deadline, so the activity
    /// reads as out of date then even if iOS never runs the app again.
    private func content(_ status: PendantStatus, connected: Bool, now: Date) -> ActivityContent<PendantStatus> {
        ActivityContent(state: status,
                        staleDate: policy.staleDate(connected: connected, lastConnectedAt: lastConnectedAt, now: now))
    }
}
