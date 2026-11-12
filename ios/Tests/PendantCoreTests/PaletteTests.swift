import XCTest
@testable import PendantCore

/// WCAG contrast for every text token on every surface it is used on (`Palette.usage`).
final class PaletteTests: XCTestCase {
    func testContrastFormulaMatchesKnownValues() {
        XCTAssertEqual(RGB.contrast(RGB(1, 1, 1), RGB(0, 0, 0)), 21, accuracy: 0.001)
        XCTAssertEqual(RGB.contrast(RGB(hex: 0x777777), RGB(1, 1, 1)), 4.48, accuracy: 0.01)
        XCTAssertEqual(RGB.contrast(RGB(hex: 0x123456), RGB(hex: 0x123456)), 1, accuracy: 0.0001)
    }

    func testEveryTextTokenMeetsWCAGOnItsSurfaces() {
        let pairs = Palette.pairings
        XCTAssertGreaterThan(pairs.count, 40)
        for p in pairs {
            XCTAssertGreaterThanOrEqual(p.ratio, p.minimum, "\(p)")
        }
        // The table, for the PR and for whoever changes a colour next.
        print(pairs.map(\.description).joined(separator: "\n"))
    }

    func testUsageNamesRealTokensAndCoversEveryTextToken() {
        let used = Set(Palette.usage.map(\.text))
        XCTAssertEqual(used, Set(Palette.texts.keys), "every text token must be checked")
        for u in Palette.usage {
            XCTAssertNotNil(Palette.texts[u.text], u.text)
            for s in u.surfaces { XCTAssertNotNil(Palette.surfaces[s], "\(u.text) on unknown surface \(s)") }
        }
    }

    func testPrimaryAndSecondaryTextWorkOnEveryBackgroundSurface() {
        let backgrounds = ["nightTop", "nightBottom", "surface", "raised", "sunken"]
        for text in ["textPrimary", "textSecondary", "textTertiary", "accent"] {
            let surfaces = Palette.usage.first { $0.text == text }!.surfaces
            for b in backgrounds { XCTAssertTrue(surfaces.contains(b), "\(text) is not checked on \(b)") }
        }
    }

    /// Pairs found in the views that the first table missed.
    func testTranscriptLineAndSwipePairsAreChecked() {
        let checked = Set(Palette.pairings.map { "\($0.text)|\($0.surface)" })
        for pair in ["accent|starWash", "star|accentWash", "star|playingWash", "accent|playingWash",
                     "systemWhite|swipeStar", "systemWhite|swipeDelete"] {
            XCTAssertTrue(checked.contains(pair), "\(pair) is not checked")
        }
    }

    func testDisabledButtonsStayReadableButLookOff() {
        let disabled = Palette.pairings.filter { $0.text == "disabledText" }
        XCTAssertEqual(Set(disabled.map(\.surface)), ["disabledFill", "nightTop", "nightBottom", "surface", "raised", "sunken"])
        for p in disabled { XCTAssertGreaterThanOrEqual(p.ratio, 3.0, "\(p)") }
        // dimmer than every enabled label, so it doesn't pass for an active button
        XCTAssertLessThan(Palette.disabledText.luminance, Palette.textTertiary.luminance)
        XCTAssertLessThan(Palette.disabledText.luminance, Palette.accent.luminance)
    }

    /// The reassign sheet's unavailable scope rows and its disabled confirm button, on what they're drawn over.
    func testUnavailableScopesAndTheDisabledConfirmAreReadable() {
        let o = Palette.unavailableOption
        let title = RGB.contrast(o.title, o.surface), reason = RGB.contrast(o.reason, o.surface)
        let button = RGB.contrast(Palette.disabledButton.label, Palette.disabledButton.fill)
        print(String(format: "unavailable scope title %.2f:1, reason %.2f:1, disabled confirm %.2f:1", title, reason, button))
        XCTAssertGreaterThanOrEqual(title, 3.0)
        XCTAssertGreaterThanOrEqual(reason, 3.0)
        XCTAssertGreaterThanOrEqual(button, 3.0)
        // the title reads as off: dimmer than an available option's
        XCTAssertLessThan(o.title.luminance, Palette.textPrimary.luminance)
    }

    func testBrandColoursAreUnchanged() {
        XCTAssertEqual(Palette.nightTop, RGB(0.10, 0.13, 0.25))
        XCTAssertEqual(Palette.nightBottom, RGB(0.03, 0.04, 0.10))
        XCTAssertEqual(Palette.accent, RGB(0.43, 0.86, 0.90))
    }
}
