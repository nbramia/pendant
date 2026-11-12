import Foundation

/// The glanceable summary shown on the lock screen (Live Activity), in the
/// Dynamic Island and in the home-screen widget. Pure value types so the
/// mapping and counters are unit-tested; the app and the widget extension both
/// compile this file.

/// What the pendant is doing, in the words the glance surfaces use.
public enum PendantPhase: String, Codable, Hashable, Sendable, CaseIterable {
    case listening, quiet, syncing, connecting, connected, notConnected

    public var title: String {
        switch self {
        case .listening: "Listening"
        case .quiet: "Quiet"
        case .syncing: "Syncing"
        case .connecting: "Connecting"
        case .connected: "Connected"
        case .notConnected: "Not connected"
        }
    }

    /// SF Symbol for the phase.
    public var symbol: String {
        switch self {
        case .listening: "waveform"
        case .quiet: "mic.slash"
        case .syncing: "arrow.triangle.2.circlepath"
        case .connecting: "dot.radiowaves.left.and.right"
        case .connected: "checkmark.circle"
        case .notConnected: "antenna.radiowaves.left.and.right.slash"
        }
    }

    public var isConnected: Bool { self != .notConnected && self != .connecting }

    /// Maps `PendantManager.state` (a free-form status line) to a phase.
    public init(appState: String) {
        switch appState {
        case "live": self = .listening
        case "quiet": self = .quiet
        case "draining": self = .syncing
        case "discovering": self = .connecting
        case _ where appState.hasPrefix("connected"): self = .connected
        default: self = .notConnected  // "starting", "waiting for a pendant", "not paired…", "scanning…"
        }
    }
}

/// One snapshot of the summary. Doubles as the Live Activity's ContentState.
public struct PendantStatus: Codable, Hashable, Sendable {
    public var phase: PendantPhase
    /// Friendly pendant name, e.g. "New pendant"; the last known one when none is connected.
    public var pendantName: String?
    public var battery: Int?
    /// Minutes of audio heard on `updatedAt`'s local day.
    public var minutesToday: Int
    public var updatedAt: Date

    public init(phase: PendantPhase, pendantName: String? = nil, battery: Int? = nil,
                minutesToday: Int = 0, updatedAt: Date = Date()) {
        self.phase = phase
        self.pendantName = pendantName
        self.battery = battery
        self.minutesToday = minutesToday
        self.updatedAt = updatedAt
    }

    /// Builds the summary from the app's published state.
    public init(appState: String, pendantName: String?, battery: Int?, minutesToday: Int, now: Date = Date()) {
        self.init(phase: PendantPhase(appState: appState), pendantName: pendantName,
                  battery: battery.map { min(max($0, 0), 100) }, minutesToday: max(minutesToday, 0), updatedAt: now)
    }

    /// Minutes heard on `date`'s day: a summary written yesterday counts as 0 today.
    public func minutesToday(asOf date: Date, calendar: Calendar = .current) -> Int {
        calendar.isDate(updatedAt, inSameDayAs: date) ? minutesToday : 0
    }

    /// True when the change is worth showing right away (anything but the minute count).
    public func differsVisibly(from other: PendantStatus?) -> Bool {
        guard let other else { return true }
        return phase != other.phase || pendantName != other.pendantName || battery != other.battery
    }
}

/// Counts audio pages stored today (live and drained) and converts them to minutes.
///
/// The app never sees individual Opus packets (the protocol's 20 ms frames are
/// packed inside the pendant's flash pages), so we count pages instead. One page
/// holds roughly 1.3-1.6 s of voice audio (a full ~95k-page flash is ~35 h; USB
/// dumps measured ~1.6 s per page), so we use 1.4 s. That makes "minutes heard"
/// an approximation good to about ±15%, which is plenty for a glance. Drained pages are counted on the
/// day they arrive on the phone, not the day they were recorded.
public struct DailyHeardCounter: Codable, Equatable, Sendable {
    public static let secondsPerPage = 1.4

    /// Local day the count belongs to, "yyyy-MM-dd".
    public private(set) var day: String
    public private(set) var pages: Int

    public init(day: String = "", pages: Int = 0) {
        self.day = day
        self.pages = pages
    }

    public static func dayKey(_ date: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// Adds pages heard at `date`, starting a fresh count after local midnight.
    public mutating func add(_ count: Int = 1, at date: Date = Date(), calendar: Calendar = .current) {
        let key = Self.dayKey(date, calendar: calendar)
        if key != day { day = key; pages = 0 }
        pages += max(count, 0)
    }

    /// Whole minutes heard on `date`'s day (0 if the count is from another day).
    public func minutes(at date: Date = Date(), calendar: Calendar = .current) -> Int {
        guard Self.dayKey(date, calendar: calendar) == day else { return 0 }
        return Int(Double(pages) * Self.secondsPerPage / 60)
    }
}

/// When to start, update and end the Live Activity.
///
/// The app may be suspended (or killed) at any time after the last pendant
/// disconnects, so the 30-minute end can't rely on the app running again:
/// every update made while disconnected carries a stale date at the deadline
/// (`lastConnectedAt + endAfterDisconnected`), and the first refresh past the
/// deadline (including on the next launch, from the persisted time) ends it.
public struct LiveActivityPolicy: Equatable, Sendable {
    /// Minimum gap between updates that only change the minute count.
    public var minUpdateInterval: TimeInterval = 60
    /// While connected, push an update at least this often so the content
    /// never reaches its stale date (`staleAfter`) when nothing changes.
    public var keepAliveInterval: TimeInterval = 10 * 60
    /// How far ahead a connected update's stale date is.
    public var staleAfter: TimeInterval = 15 * 60
    /// End the activity after this long without a connected pendant.
    public var endAfterDisconnected: TimeInterval = 30 * 60

    public init() {}

    public func shouldUpdate(last: PendantStatus?, lastUpdateAt: Date?, next: PendantStatus,
                             connected: Bool, now: Date) -> Bool {
        if next.differsVisibly(from: last) { return true }
        guard let last, let lastUpdateAt else { return true }
        let elapsed = now.timeIntervalSince(lastUpdateAt)
        if elapsed < minUpdateInterval { return false }
        if next.minutesToday != last.minutesToday { return true }
        return connected && elapsed >= keepAliveInterval
    }

    /// When the activity should end: `lastConnectedAt` plus the grace period
    /// (nil when no pendant was ever connected, i.e. end now).
    public func endDeadline(lastConnectedAt: Date?) -> Date? {
        lastConnectedAt?.addingTimeInterval(endAfterDisconnected)
    }

    /// `lastConnectedAt` is the last time any pendant was connected, persisted across launches.
    public func shouldEnd(lastConnectedAt: Date?, now: Date) -> Bool {
        guard let deadline = endDeadline(lastConnectedAt: lastConnectedAt) else { return true }
        return now >= deadline
    }

    /// Stale date for content pushed now: a rolling window while connected
    /// (kept fresh by keep-alive updates), else the end deadline, so iOS shows
    /// the activity as out of date then even if the app never runs again.
    public func staleDate(connected: Bool, lastConnectedAt: Date?, now: Date) -> Date {
        if connected { return now.addingTimeInterval(staleAfter) }
        return endDeadline(lastConnectedAt: lastConnectedAt) ?? now
    }
}

/// Last summary shared between the app and the widget through the app group.
public struct SharedStatusStore {
    /// The app group the app and widget share (`group.$(PENDANT_BUNDLE_ID)`, from Info.plist).
    public static let appGroup = (Bundle.main.object(forInfoDictionaryKey: "PendantAppGroup") as? String)
        .flatMap { $0.hasPrefix("group.") && !$0.contains("$(") ? $0 : nil } ?? "group.com.example.pendant"
    static let statusKey = "pendantStatus"
    static let counterKey = "heardCounter"
    static let lastConnectedKey = "lastConnectedAt"

    public let defaults: UserDefaults

    public init(defaults: UserDefaults? = UserDefaults(suiteName: SharedStatusStore.appGroup)) {
        self.defaults = defaults ?? .standard
    }

    public func loadStatus() -> PendantStatus? { decode(Self.statusKey) }
    public func save(_ status: PendantStatus) { encode(status, Self.statusKey) }

    public func loadCounter() -> DailyHeardCounter { decode(Self.counterKey) ?? DailyHeardCounter() }
    public func save(_ counter: DailyHeardCounter) { encode(counter, Self.counterKey) }

    /// Last time any pendant was connected (drives the Live Activity's 30-minute end).
    public func loadLastConnectedAt() -> Date? { defaults.object(forKey: Self.lastConnectedKey) as? Date }
    public func saveLastConnectedAt(_ date: Date) { defaults.set(date, forKey: Self.lastConnectedKey) }

    private func decode<T: Decodable>(_ key: String) -> T? {
        defaults.data(forKey: key).flatMap { try? JSONDecoder().decode(T.self, from: $0) }
    }

    private func encode<T: Encodable>(_ value: T, _ key: String) {
        if let data = try? JSONEncoder().encode(value) { defaults.set(data, forKey: key) }
    }
}
