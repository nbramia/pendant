#if canImport(AppKit)
import AppKit
import SwiftUI
import XCTest
@testable import PendantCore

/// Inline chips that are buttons tap across at least 44 × 44 pt while drawing at their own size.
@MainActor
final class TapAreaTests: XCTestCase {
    func testASmallChipGetsAFullTapAreaCentredOnIt() {
        // a line's time stamp is about this size
        let chip = CGRect(x: 20, y: 100, width: 58, height: 19)
        let area = MinimumTapShape.area(for: chip)
        XCTAssertGreaterThanOrEqual(area.width, minTapTarget)
        XCTAssertGreaterThanOrEqual(area.height, minTapTarget)
        XCTAssertEqual(area.midX, chip.midX, accuracy: 0.001)
        XCTAssertEqual(area.midY, chip.midY, accuracy: 0.001)
        XCTAssertTrue(area.contains(chip), "the chip itself still taps")
        XCTAssertEqual(area.width, chip.width, "a chip already wider than 44 pt keeps its width")
    }

    func testANarrowChipGrowsBothWays() {
        let chip = CGRect(x: 0, y: 0, width: 20, height: 20)
        let area = MinimumTapShape.area(for: chip)
        XCTAssertEqual(area.size, CGSize(width: 44, height: 44))
        XCTAssertEqual(area.origin, CGPoint(x: -12, y: -12))
    }

    func testABigControlIsLeftAlone() {
        let row = CGRect(x: 0, y: 0, width: 300, height: 52)
        XCTAssertEqual(MinimumTapShape.area(for: row), row)
        XCTAssertEqual(MinimumTapShape().path(in: row).boundingRect, row)
    }

    func testTheShapesPathIsTheArea() {
        let chip = CGRect(x: 4, y: 4, width: 70, height: 27)
        XCTAssertEqual(MinimumTapShape().path(in: chip).boundingRect, MinimumTapShape.area(for: chip))
    }

    /// Laid out with the tap area, a speaker chip is exactly as big as without it, and its tap area
    /// is at least 44 × 44 pt: the look and the layout don't change.
    func testTheTapAreaDoesNotChangeTheChipsSize() {
        final class Box { var plain: CGSize = .zero; var tappable: CGSize = .zero }
        let box = Box()
        let view = VStack(alignment: .leading) {
            SpeakerChip(name: "Alex Rivera")
                .background(GeometryReader { g -> Color in box.plain = g.size; return .clear })
            Button {} label: { SpeakerChip(name: "Alex Rivera") }
                .buttonStyle(.plain)
                .minimumTapArea()
                .background(GeometryReader { g -> Color in box.tappable = g.size; return .clear })
        }
        let host = NSHostingView(rootView: view.frame(width: 358).environment(\.colorScheme, .dark))
        host.frame = CGRect(x: 0, y: 0, width: 358, height: 200)
        host.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(box.plain.height, 0, "not laid out")
        XCTAssertEqual(box.tappable.width, box.plain.width, accuracy: 0.5)
        XCTAssertEqual(box.tappable.height, box.plain.height, accuracy: 0.5)
        XCTAssertLessThan(box.plain.height, minTapTarget, "the chip draws smaller than its tap area")
        let area = MinimumTapShape.area(for: CGRect(origin: .zero, size: box.tappable))
        XCTAssertGreaterThanOrEqual(area.height, minTapTarget)
        XCTAssertGreaterThanOrEqual(area.width, minTapTarget)
    }
}
#endif
