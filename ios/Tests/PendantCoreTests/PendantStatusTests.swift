import XCTest
@testable import PendantCore

final class PendantStatusTests: XCTestCase {
    private var cal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/New_York")!
        return c
    }()

    private func date(_ y: Int, _ m: Int, _ d: Int, _ h: Int, _ min: Int = 0) -> Date {
        cal.date(from: DateComponents(year: y, month: m, day: d, hour: h, minute: min))!
    }

    // MARK: state mapping

    func testAppStatesMapToPhases() {
        XCTAssertEqual(PendantPhase(appState: "live"), .listening)
        XCTAssertEqual(PendantPhase(appState: "quiet"), .quiet)
        XCTAssertEqual(PendantPhase(appState: "draining"), .syncing)
        XCTAssertEqual(PendantPhase(appState: "discovering"), .connecting)
        XCTAssertEqual(PendantPhase(appState: "connected (flash only)"), .connected)
        XCTAssertEqual(PendantPhase(appState: "waiting for a pendant"), .notConnected)
        XCTAssertEqual(PendantPhase(appState: "starting"), .notConnected)
        XCTAssertEqual(PendantPhase(appState: "not paired: tap Add pendant"), .notConnected)
        XCTAssertEqual(PendantPhase(appState: "scanning: hold the new pendant near the phone"), .notConnected)
    }

    func testPhaseTitlesMatchTheIssue() {
        XCTAssertEqual(PendantPhase.listening.title, "Listening")
        XCTAssertEqual(PendantPhase.quiet.title, "Quiet")
        XCTAssertEqual(PendantPhase.syncing.title, "Syncing")
        XCTAssertEqual(PendantPhase.notConnected.title, "Not connected")
        XCTAssertTrue(PendantPhase.listening.isConnected)
        XCTAssertFalse(PendantPhase.notConnected.isConnected)
        XCTAssertFalse(PendantPhase.connecting.isConnected)
    }

    func testStatusFromAppStateClampsValues() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let s = PendantStatus(appState: "live", pendantName: "New pendant", battery: 140, minutesToday: -3, now: now)
        XCTAssertEqual(s.phase, .listening)
        XCTAssertEqual(s.pendantName, "New pendant")
        XCTAssertEqual(s.battery, 100)
        XCTAssertEqual(s.minutesToday, 0)
        XCTAssertEqual(s.updatedAt, now)
    }

    func testStatusRoundTripsThroughJSON() throws {
        let s = PendantStatus(phase: .syncing, pendantName: "Old pendant", battery: 42, minutesToday: 17,
                              updatedAt: Date(timeIntervalSince1970: 1_700_000_000))
        let back = try JSONDecoder().decode(PendantStatus.self, from: JSONEncoder().encode(s))
        XCTAssertEqual(back, s)
    }

    func testMinutesFromAnotherDayReadAsZero() {
        let s = PendantStatus(phase: .quiet, minutesToday: 30, updatedAt: date(2026, 11, 8, 23, 50))
        XCTAssertEqual(s.minutesToday(asOf: date(2026, 11, 8, 23, 59), calendar: cal), 30)
        XCTAssertEqual(s.minutesToday(asOf: date(2026, 11, 9, 0, 1), calendar: cal), 0)
    }

    // MARK: minutes counter

    func testCounterConvertsPagesToMinutes() {
        var c = DailyHeardCounter()
        let t = date(2026, 11, 8, 10)
        c.add(100, at: t, calendar: cal)  // 100 * 1.4 s = 140 s
        XCTAssertEqual(c.pages, 100)
        XCTAssertEqual(c.minutes(at: t, calendar: cal), 2)
        c.add(at: t, calendar: cal)
        XCTAssertEqual(c.pages, 101)
    }

    func testCounterResetsAtLocalMidnight() {
        var c = DailyHeardCounter()
        c.add(500, at: date(2026, 11, 8, 23, 59), calendar: cal)
        XCTAssertEqual(c.minutes(at: date(2026, 11, 8, 23, 59), calendar: cal), 11)
        XCTAssertEqual(c.minutes(at: date(2026, 11, 9, 0, 0), calendar: cal), 0, "a new day reads zero before any page")
        c.add(50, at: date(2026, 11, 9, 0, 1), calendar: cal)
        XCTAssertEqual(c.day, "2026-11-09")
        XCTAssertEqual(c.pages, 50)
        XCTAssertEqual(c.minutes(at: date(2026, 11, 9, 0, 1), calendar: cal), 1)
    }

    func testMidnightIsLocalNotUTC() {
        // 21:00 in New York is already the next day in UTC; it's still the same local day.
        var c = DailyHeardCounter()
        c.add(10, at: date(2026, 11, 8, 9), calendar: cal)
        c.add(10, at: date(2026, 11, 8, 21), calendar: cal)
        XCTAssertEqual(c.pages, 20)
    }

    func testCounterIgnoresNegativeCounts() {
        var c = DailyHeardCounter()
        c.add(-5, at: date(2026, 11, 8, 9), calendar: cal)
        XCTAssertEqual(c.pages, 0)
    }

    // MARK: live activity policy

    func testVisibleChangesUpdateImmediately() {
        let p = LiveActivityPolicy()
        let now = Date()
        let a = PendantStatus(phase: .listening, pendantName: "New pendant", battery: 80, minutesToday: 5, updatedAt: now)
        var b = a; b.phase = .quiet
        XCTAssertTrue(p.shouldUpdate(last: a, lastUpdateAt: now, next: b, connected: true, now: now))
        var c = a; c.battery = 79
        XCTAssertTrue(p.shouldUpdate(last: a, lastUpdateAt: now, next: c, connected: true, now: now))
        XCTAssertTrue(p.shouldUpdate(last: nil, lastUpdateAt: nil, next: a, connected: true, now: now))
    }

    func testMinuteOnlyChangesAreThrottledToOncePerMinute() {
        let p = LiveActivityPolicy()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        let a = PendantStatus(phase: .listening, minutesToday: 5, updatedAt: t0)
        var b = a; b.minutesToday = 6
        XCTAssertFalse(p.shouldUpdate(last: a, lastUpdateAt: t0, next: b, connected: true, now: t0.addingTimeInterval(30)))
        XCTAssertTrue(p.shouldUpdate(last: a, lastUpdateAt: t0, next: b, connected: true, now: t0.addingTimeInterval(60)))
        XCTAssertTrue(p.shouldUpdate(last: a, lastUpdateAt: t0, next: b, connected: false, now: t0.addingTimeInterval(60)))
    }

    func testConnectedUnchangedStateGetsKeepAliveEveryTenMinutes() {
        let p = LiveActivityPolicy()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        let quiet = PendantStatus(phase: .quiet, pendantName: "New pendant", battery: 70, minutesToday: 5, updatedAt: t0)
        XCTAssertFalse(p.shouldUpdate(last: quiet, lastUpdateAt: t0, next: quiet, connected: true,
                                      now: t0.addingTimeInterval(9 * 60)))
        XCTAssertTrue(p.shouldUpdate(last: quiet, lastUpdateAt: t0, next: quiet, connected: true,
                                     now: t0.addingTimeInterval(10 * 60)))
        XCTAssertLessThan(p.keepAliveInterval, p.staleAfter, "keep-alive lands before the content goes stale")
        XCTAssertGreaterThanOrEqual(p.keepAliveInterval, p.minUpdateInterval)
    }

    func testDisconnectedUnchangedStateSendsNoKeepAlive() {
        let p = LiveActivityPolicy()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        let gone = PendantStatus(phase: .notConnected, pendantName: "New pendant", minutesToday: 5, updatedAt: t0)
        XCTAssertFalse(p.shouldUpdate(last: gone, lastUpdateAt: t0, next: gone, connected: false,
                                      now: t0.addingTimeInterval(20 * 60)))
    }

    func testEndsAfterThirtyMinutesWithoutAPendant() {
        let p = LiveActivityPolicy()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertFalse(p.shouldEnd(lastConnectedAt: t0, now: t0.addingTimeInterval(29 * 60)))
        XCTAssertTrue(p.shouldEnd(lastConnectedAt: t0, now: t0.addingTimeInterval(30 * 60)))
        XCTAssertTrue(p.shouldEnd(lastConnectedAt: nil, now: t0))
        XCTAssertEqual(p.endDeadline(lastConnectedAt: t0), t0.addingTimeInterval(30 * 60))
    }

    func testRelaunchDoesNotExtendTheDeadline() {
        // The deadline comes only from the persisted connect time, so a launch
        // 40 minutes after the last connection ends the activity at once.
        let p = LiveActivityPolicy()
        let lastConnected = Date(timeIntervalSince1970: 1_000_000)
        let launch = lastConnected.addingTimeInterval(40 * 60)
        XCTAssertTrue(p.shouldEnd(lastConnectedAt: lastConnected, now: launch))
    }

    func testStaleDateRollsWhileConnectedAndPinsToDeadlineOnceDisconnected() {
        let p = LiveActivityPolicy()
        let lastConnected = Date(timeIntervalSince1970: 1_000_000)
        let now = lastConnected.addingTimeInterval(5 * 60)
        XCTAssertEqual(p.staleDate(connected: true, lastConnectedAt: now, now: now), now.addingTimeInterval(15 * 60))
        XCTAssertEqual(p.staleDate(connected: false, lastConnectedAt: lastConnected, now: now),
                       lastConnected.addingTimeInterval(30 * 60))
        XCTAssertEqual(p.staleDate(connected: false, lastConnectedAt: nil, now: now), now)
    }

    // MARK: shared store

    func testSharedStoreRoundTrip() {
        let suite = "PendantStatusTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = SharedStatusStore(defaults: defaults)
        XCTAssertNil(store.loadStatus())
        XCTAssertEqual(store.loadCounter(), DailyHeardCounter())
        let s = PendantStatus(phase: .listening, pendantName: "New pendant", battery: 60, minutesToday: 3,
                              updatedAt: Date(timeIntervalSince1970: 1_700_000_000))
        store.save(s)
        var c = DailyHeardCounter(); c.add(7, at: Date(), calendar: cal)
        store.save(c)
        XCTAssertNil(store.loadLastConnectedAt())
        let connected = Date(timeIntervalSince1970: 1_700_000_100)
        store.saveLastConnectedAt(connected)
        XCTAssertEqual(store.loadStatus(), s)
        XCTAssertEqual(store.loadCounter(), c)
        XCTAssertEqual(store.loadLastConnectedAt(), connected)
    }
}
