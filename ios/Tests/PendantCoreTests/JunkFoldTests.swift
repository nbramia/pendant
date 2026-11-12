import XCTest
@testable import PendantCore

final class JunkFoldTests: XCTestCase {
    private func rows(_ spec: String) -> [JunkFold.Row] {
        // "a" = keep, "J" prefix = junk, e.g. "a Jb c"
        spec.split(separator: " ").map { s in
            s.hasPrefix("J") ? .init(id: String(s), isJunk: true) : .init(id: String(s), isJunk: false)
        }
    }

    func testCollapsedDropsJunkAndPlacesOneToggleAtFirstJunk() {
        let e = JunkFold.entries(rows("a Jb c Jd e"), expanded: false)
        XCTAssertEqual(e, [.item("a"), .toggle(count: 2, expanded: false), .item("c"), .item("e")])
    }

    func testExpandedGroupsJunkUnderToggleInOrder() {
        let e = JunkFold.entries(rows("a Jb c Jd e"), expanded: true)
        XCTAssertEqual(e, [.item("a"), .toggle(count: 2, expanded: true), .clip("Jb"), .clip("Jd"),
                           .item("c"), .item("e")])
    }

    func testToggleKeepsFirstJunkPositionWhenExpanded() {
        let e = JunkFold.entries(rows("a b Jc d"), expanded: true)
        XCTAssertEqual(e, [.item("a"), .item("b"), .toggle(count: 1, expanded: true), .clip("Jc"), .item("d")])
    }

    func testNoJunkNoToggle() {
        XCTAssertEqual(JunkFold.entries(rows("a b"), expanded: false), [.item("a"), .item("b")])
        XCTAssertEqual(JunkFold.entries([], expanded: false), [])
    }

    func testAllJunkCollapsesToOneRow() {
        XCTAssertEqual(JunkFold.entries(rows("Ja Jb Jc"), expanded: false), [.toggle(count: 3, expanded: false)])
    }

    func testCountDropsWhenAClipIsRemoved() {
        XCTAssertEqual(JunkFold.entries(rows("Ja Jb"), expanded: false), [.toggle(count: 2, expanded: false)])
        XCTAssertEqual(JunkFold.entries(rows("Jb"), expanded: false), [.toggle(count: 1, expanded: false)])
        XCTAssertEqual(JunkFold.entries(rows(""), expanded: false), [])
    }

    func testStarredAndNotableNeverJunk() {
        let k = RecordingKind.fragment
        XCTAssertTrue(JunkFold.isJunk(kind: k, starCount: 0, appStar: false, notable: false))
        XCTAssertFalse(JunkFold.isJunk(kind: k, starCount: 1, appStar: false, notable: false))
        XCTAssertFalse(JunkFold.isJunk(kind: .media, starCount: 0, appStar: true, notable: false))
        XCTAssertFalse(JunkFold.isJunk(kind: .media, starCount: 0, appStar: false, notable: true))
        XCTAssertFalse(JunkFold.isJunk(kind: .conversation, starCount: 0, appStar: false, notable: false))
    }

    func testLabels() {
        XCTAssertEqual(JunkFold.label(count: 6, expanded: false), "6 hidden · Show")
        XCTAssertEqual(JunkFold.label(count: 6, expanded: true), "Hide")
    }
}
