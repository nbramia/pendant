import XCTest
@testable import PendantCore

final class AckTrackerTests: XCTestCase {
    func testAcksHighestBatchPageAfterCommit() {
        var t = AckTracker()
        t.applyStatus(oldest: 0, newest: 20, localWatermark: -1)
        // The pendant's batch stream resumes mid-way (pages 0..9 went out earlier).
        for i in Int64(10)...15 { XCTAssertTrue(t.shouldStore(i)); t.didStore(i, batch: true) }
        XCTAssertEqual(t.commit(), 15)
        XCTAssertEqual(t.ackedThrough, 15)
        XCTAssertNil(t.commit(), "nothing new to ack")
    }

    func testLivePagesNeverTriggerAnAck() {
        var t = AckTracker()
        t.applyStatus(oldest: 0, newest: 40, localWatermark: -1)
        t.didStore(39, batch: false)
        XCTAssertNil(t.commit())
        XCTAssertFalse(t.shouldStore(39), "already stored this connection")
    }

    func testDuplicatesBelowWatermarkAreSkipped() {
        var t = AckTracker()
        t.applyStatus(oldest: 0, newest: 100, localWatermark: 60)
        XCTAssertEqual(t.ackedThrough, 60, "our watermark beats a lagging oldest")
        XCTAssertFalse(t.shouldStore(60))
        XCTAssertTrue(t.shouldStore(61))
    }

    func testWipeResetsWatermark() {
        var t = AckTracker()
        t.applyStatus(oldest: 0, newest: 100, localWatermark: 90)
        t.applyStatus(oldest: 0, newest: 0xFFFF_FFFF, localWatermark: 90)
        XCTAssertEqual(t.ackedThrough, -1)
        t.applyStatus(oldest: 0, newest: -1, localWatermark: 90)
        XCTAssertEqual(t.ackedThrough, -1)
    }

    func testWatermarkCappedAtNewest() {
        var t = AckTracker()
        // A stale watermark from a previous storage instance must not hide new pages.
        t.applyStatus(oldest: 0, newest: 5, localWatermark: 500)
        XCTAssertEqual(t.ackedThrough, 5)
    }

    func testNeedsDrainIgnoresOpenNewestPage() {
        var t = AckTracker()
        t.applyStatus(oldest: 0, newest: 11, localWatermark: 10)
        XCTAssertFalse(t.needsDrain(newest: 11), "page 11 is still being written")
        XCTAssertTrue(t.needsDrain(newest: 12))
        XCTAssertFalse(t.needsDrain(newest: 0xFFFF_FFFF))
    }
}

final class LiveThenBatchTests: XCTestCase {
    /// A page that arrived live and then again in a drain is already in the
    /// spool; the drain must still be able to ack it.
    func testBatchRepeatOfLivePageIsAckable() {
        var t = AckTracker()
        t.applyStatus(oldest: 35, newest: 37, localWatermark: 34)
        t.didStore(35, batch: false)
        t.didStore(36, batch: false)
        XCTAssertNil(t.commit())
        XCTAssertFalse(t.shouldStore(35))
        t.didSeeStoredBatchPage(35)
        t.didSeeStoredBatchPage(36)
        XCTAssertEqual(t.commit(), 36)
    }

    func testUnseenPageIsNotAckedBySeeing() {
        var t = AckTracker()
        t.applyStatus(oldest: 0, newest: 10, localWatermark: -1)
        t.didSeeStoredBatchPage(5)  // never stored
        XCTAssertNil(t.commit())
    }
}
