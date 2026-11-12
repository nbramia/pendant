import XCTest
@testable import PendantCore

final class FeedRowsTests: XCTestCase {
    func testRequests() {
        XCTAssertEqual(RowRequest.path(device: "A1B2C3D4E5F60718", startMs: 1700000000000, "delete"),
                       "conversations/A1B2C3D4E5F60718/1700000000000/delete")
        XCTAssertEqual(RowRequest.starBody(appStarred: false), ["on": true])
        XCTAssertEqual(RowRequest.starBody(appStarred: true), ["on": false])
        XCTAssertEqual(UndoWindow.seconds, 5)
    }

    func testDecodesTheTrashFeed() throws {
        let json = """
        {"trash": [{"id": "E1-1700000000000", "device_id": "E1", "start_ms": 1700000000000, "end_ms": 1700000060000,
          "day": "2023-11-14", "title": "Movie night", "heading": "17:13 — Movie night (1m00s) · new",
          "device": "new", "duration_s": 60, "deleted_at": 1700000100.5, "purge_at": 1700604900.5}]}
        """.data(using: .utf8)!
        let t = try JSONDecoder().decode(TrashFeed.self, from: json).trash[0]
        XCTAssertEqual(t.deviceId, "E1")
        XCTAssertEqual(t.startMs, 1700000000000)
        XCTAssertEqual(t.title, "Movie night")
        XCTAssertEqual(t.start.timeIntervalSince1970, 1700000000)
        let now = Date(timeIntervalSince1970: 1700000100.5)
        XCTAssertEqual(t.daysLeft(now: now), 7)
        XCTAssertEqual(t.daysLeftLabel(now: now.addingTimeInterval(6.5 * 86400)), "1 day left")
        XCTAssertEqual(t.daysLeftLabel(now: now.addingTimeInterval(8 * 86400)), "Removed today")
    }
}
