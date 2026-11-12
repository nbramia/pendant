#if canImport(AppKit)
import AppKit
import SwiftUI
import XCTest
@testable import PendantCore

/// Button sizes come from the role, not the label: equal widths in a `ButtonRow`, and an
/// icon never changes a button's height. Measured by laying the views out on macOS.
@MainActor
final class ButtonLayoutTests: XCTestCase {
    /// Records each tagged view's laid-out size.
    private final class Sizes { var byTag: [String: CGSize] = [:] }

    private struct Measured<V: View>: View {
        let tag: String
        let sizes: Sizes
        let content: V
        var body: some View {
            content.background(GeometryReader { g -> Color in
                sizes.byTag[tag] = g.size
                return .clear
            })
        }
    }

    private func layout<V: View>(width: CGFloat = 358, _ view: V) {
        let host = NSHostingView(rootView: view.frame(width: width).environment(\.colorScheme, .dark))
        host.frame = CGRect(x: 0, y: 0, width: width, height: 600)
        host.layoutSubtreeIfNeeded()
        _ = ImageRenderer(content: view.frame(width: width)).cgImage
    }

    func testSlotWidthsAreEqualAndFillTheRow() {
        let w = EqualWidthLayout.slotWidth(width: 358, count: 2, spacing: 8)
        XCTAssertEqual(w, 175)
        XCTAssertEqual(EqualWidthLayout.slotWidth(width: 358, count: 3, spacing: 8) * 3 + 16, 358, accuracy: 0.001)
    }

    func testButtonRowGivesShortAndLongLabelsEqualWidths() {
        let short = "Yes", long = "Change the LifeOS person link."
        XCTAssertEqual(short.count, 3)
        XCTAssertEqual(long.count, 30)
        for size in ButtonSize.allCases where size != .large {
            let s = Sizes()
            layout(ButtonRow {
                Measured(tag: "short", sizes: s, content: Button(short) {}.buttonStyle(.secondary(size)))
                Measured(tag: "long", sizes: s, content: Button(long) {}.buttonStyle(.primary(size)))
            })
            guard let a = s.byTag["short"], let b = s.byTag["long"] else { return XCTFail("\(size) not laid out") }
            XCTAssertEqual(a.width, b.width, accuracy: 0.5, "\(size): \(a) vs \(b)")
            XCTAssertEqual(a.width, EqualWidthLayout.slotWidth(width: 358, count: 2, spacing: Spacing.s), accuracy: 0.5)
            XCTAssertEqual(a.height, b.height, accuracy: 0.5, "\(size): a long label must not make its button taller")
        }
    }

    func testButtonRowWithColumnsKeepsTheColumnWidthForALoneButton() {
        let s = Sizes()
        layout(ButtonRow(columns: 2) {
            Measured(tag: "only", sizes: s, content: Button("Someone else") {}.buttonStyle(.secondary(.medium)))
        })
        XCTAssertEqual(s.byTag["only"]?.width ?? 0, EqualWidthLayout.slotWidth(width: 358, count: 2, spacing: Spacing.s),
                       accuracy: 0.5)
    }

    func testAnIconDoesNotChangeAButtonsHeight() {
        for size in ButtonSize.allCases {
            let s = Sizes()
            layout(VStack {
                Measured(tag: "text", sizes: s, content: Button("Alex Rivera") {}.buttonStyle(.secondary(size)))
                Measured(tag: "icon", sizes: s, content:
                    Button { } label: { Label("Alex Rivera", systemImage: "checkmark") }.buttonStyle(.primary(size)))
            })
            let t = s.byTag["text"]?.height ?? -1, i = s.byTag["icon"]?.height ?? -2
            XCTAssertEqual(t, i, accuracy: 0.5, "\(size)")
            XCTAssertEqual(t, max(size.height, minTapTarget), accuracy: 0.5, "\(size) draws \(size.height) pt with a 44 pt tap area")
        }
    }

    func testLargeFillsTheWidthAndShortLabelsKeepTheMinimumWidth() {
        let s = Sizes()
        layout(VStack(alignment: .leading) {
            Measured(tag: "large", sizes: s, content: Button("Go") {}.buttonStyle(.primary(.large)))
            Measured(tag: "medium", sizes: s, content: Button("Go") {}.buttonStyle(.secondary(.medium)))
            Measured(tag: "small", sizes: s, content: Button("Go") {}.buttonStyle(.secondary(.small)))
        })
        XCTAssertEqual(s.byTag["large"]?.width ?? 0, 358, accuracy: 0.5)
        XCTAssertEqual(s.byTag["medium"]?.width ?? 0, ButtonSize.medium.minWidth, accuracy: 0.5)
        XCTAssertEqual(s.byTag["small"]?.width ?? 0, ButtonSize.small.minWidth, accuracy: 0.5)
    }

    func testChipsInAButtonRowShareTheRowEqually() {
        let s = Sizes()
        layout(ButtonRow(columns: 3) {
            Measured(tag: "a", sizes: s, content: Chip(text: "Notable", symbol: "star.fill", on: false))
            Measured(tag: "b", sizes: s, content: Chip(text: "Has to-dos", symbol: "checklist", on: true))
            Measured(tag: "c", sizes: s, content: Chip(text: "10+ min", symbol: "clock", on: false))
        })
        let w = EqualWidthLayout.slotWidth(width: 358, count: 3, spacing: Spacing.s)
        for t in ["a", "b", "c"] { XCTAssertEqual(s.byTag[t]?.width ?? 0, w, accuracy: 0.5, t) }
    }

    func testWhosThisRowsPairNamesThenSomeoneElse() {
        typealias P = WhosThisPanel<EmptyView>
        XCTAssertEqual(P.rows(["Noah Park", "Alex Rivera", "Riley"]),
                       [["Noah Park", "Alex Rivera"], ["Riley", P.someoneElse]])
        XCTAssertEqual(P.rows(["Noah Park"]), [["Noah Park", P.someoneElse]])
        XCTAssertEqual(P.rows([]), [[P.someoneElse]])
    }
}
#endif
