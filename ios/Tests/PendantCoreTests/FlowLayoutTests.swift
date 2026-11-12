import CoreGraphics
import XCTest
@testable import PendantCore

final class FlowLayoutTests: XCTestCase {
    /// A fake label: `ideal` wide on one line; offered less, it wraps (same area, rows 44 tall).
    private func measure(_ ideals: [CGFloat]) -> (Int, CGFloat?) -> CGSize {
        { i, w in
            let ideal = ideals[i]
            guard let w, w < ideal else { return CGSize(width: ideal, height: 44) }
            return CGSize(width: w, height: 44 * (ideal / w).rounded(.up))
        }
    }

    func testShortItemsShareRowsAndWrap() {
        let f = FlowLayout.frames(count: 4, width: 300, spacing: 8, measure: measure([90, 90, 90, 90]))
        XCTAssertEqual(f.map(\.minY), [0, 0, 0, 52])
        XCTAssertEqual(f.map(\.minX), [0, 98, 196, 0])
    }

    func testItemWiderThanTheRowIsCappedToTheRow() {
        // "Riley Doctor Paula Grant Abernathy" as a button is ~420 pt on one line.
        let f = FlowLayout.frames(count: 3, width: 260, spacing: 8, measure: measure([80, 420, 80]))
        for frame in f { XCTAssertLessThanOrEqual(frame.maxX, 260, "\(frame) runs off the row") }
        XCTAssertEqual(f[1].minX, 0, "the long item starts its own row")
        XCTAssertEqual(f[1].width, 260)
        XCTAssertEqual(f[1].height, 88, "it wraps to two lines")
        XCTAssertEqual(f[2].minY, f[1].maxY + 8, "the next item goes below the wrapped one")
    }

    func testLongItemFirstOnARowDoesNotLeaveAnEmptyRow() {
        let f = FlowLayout.frames(count: 1, width: 200, spacing: 8, measure: measure([500]))
        XCTAssertEqual(f, [CGRect(x: 0, y: 0, width: 200, height: 132)])
    }
}
