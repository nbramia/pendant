import XCTest

/// Speaker names and play buttons take taps across at least 44 × 44 pt. Runs on the simulator against
/// the screen tour's inert sample meeting (`-screenTourSamples`; Feed sends nothing for it), tapping
/// just outside each chip's drawn capsule.
final class TapTargetUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["-screenTourSamples"]
        app.launch()
    }

    /// The Who's this card on Today: its play button and name buttons draw at 44 pt or more.
    func testWhosThisControlsAreFullSize() {
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 20))
        let card = app.otherElements["whos-this"]
        guard card.waitForExistence(timeout: 15) else { return XCTFail("no Who's this card") }
        let play = card.buttons.matching(NSPredicate(format: "label IN {'Play voice', 'Playing'}")).firstMatch
        XCTAssertTrue(play.exists)
        XCTAssertGreaterThanOrEqual(play.frame.width, 44)
        XCTAssertGreaterThanOrEqual(play.frame.height, 44)
        let names = card.buttons.allElementsBoundByIndex.filter { $0.label != play.label }
        XCTAssertFalse(names.isEmpty)
        for b in names { XCTAssertGreaterThanOrEqual(b.frame.height, 44, "\(b.label)") }
    }

    func testASpeakerNameTapsJustOutsideItsChip() {
        openSampleMeeting()
        let chip = app.buttons.matching(identifier: "speaker-chip").firstMatch
        XCTAssertTrue(chip.waitForExistence(timeout: 5))
        let f = chip.frame
        XCTAssertLessThan(f.height, 44, "the chip draws smaller than its tap area")
        // the label sits 3 pt inside the capsule: 7 pt past its edge is 10 pt past the text
        for (dy, side) in [(f.height / 2 + 7, "below"), (-(f.height / 2 + 7), "above"),
                           (21, "at the bottom of 44 pt"), (-21, "at the top of 44 pt")] {
            tap(chip, dy: dy)
            XCTAssertTrue(sheetOpened(), "a tap \(side) the speaker chip opens the picker")
            XCTAssertFalse(app.descendants(matching: .any)["line-loading"].exists, "the line didn't start playing")
            closeSheet()
        }
    }

    func testAPlayStampTapsJustOutsideItsChip() {
        openSampleMeeting()
        let stamp = app.buttons.matching(identifier: "line-stamp").firstMatch
        XCTAssertTrue(stamp.waitForExistence(timeout: 5))
        let f = stamp.frame
        XCTAssertLessThan(f.height, 44, "the stamp draws smaller than its tap area")
        tap(stamp, dy: f.height / 2 + 7)
        XCTAssertTrue(app.descendants(matching: .any)["line-loading"].waitForExistence(timeout: 3),
                      "a tap 10 pt below the play stamp's text starts the line")
        XCTAssertFalse(sheetOpened(timeout: 1), "the play tap didn't open the speaker picker")
    }

    func testAStampAtTheTopOfItsTapAreaPlays() {
        openSampleMeeting()
        let stamp = app.buttons.matching(identifier: "line-stamp").firstMatch
        XCTAssertTrue(stamp.waitForExistence(timeout: 5))
        tap(stamp, dy: -21)
        XCTAssertTrue(app.descendants(matching: .any)["line-loading"].waitForExistence(timeout: 3),
                      "a tap at the top of the stamp's 44 pt area starts the line")
    }

    // MARK: helpers

    /// Opens the tour's sample meeting: on Today, or (when Today has no conversations yet, so no samples)
    /// on the newest History day that has them.
    private func openSampleMeeting() {
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 20))
        let row = app.buttons["tour-sample-23:58"]
        var found = reach(row, wait: 20)
        if !found {
            app.tabBars.buttons["History"].tap()
            let days = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'day-'"))
            if days.firstMatch.waitForExistence(timeout: 15) {
                for i in 0..<min(days.count, 4) where !found {
                    days.element(boundBy: i).tap()
                    found = reach(row, wait: 15)
                    if !found { app.navigationBars.buttons.firstMatch.tap() }
                }
            }
        }
        XCTAssertTrue(found, "the sample meeting is on Today or a recent History day")
        row.tap()
        XCTAssertTrue(app.buttons.matching(identifier: "speaker-chip").firstMatch.waitForExistence(timeout: 10))
        _ = XCTWaiter.wait(for: [XCTestExpectation(description: "settle")], timeout: 1)
    }

    /// Waits for the samples to load, then scrolls `row` into reach.
    private func reach(_ row: XCUIElement, wait: TimeInterval) -> Bool {
        _ = row.waitForExistence(timeout: wait)
        var tries = 0
        while !(row.exists && row.isHittable) && tries < 10 { app.swipeUp(velocity: .slow); tries += 1 }
        tries = 0
        while !(row.exists && row.isHittable) && tries < 10 { app.swipeDown(velocity: .slow); tries += 1 }
        return row.exists && row.isHittable
    }

    /// Taps `dy` points above (negative) or below the element's centre, at its horizontal centre.
    private func tap(_ e: XCUIElement, dy: CGFloat) {
        e.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).withOffset(CGVector(dx: 0, dy: dy)).tap()
    }

    private func sheetOpened(timeout: TimeInterval = 4) -> Bool {
        app.navigationBars.matching(NSPredicate(format: "identifier BEGINSWITH 'Who'")).firstMatch
            .waitForExistence(timeout: timeout)
    }

    private func closeSheet() {
        let cancel = app.buttons["Cancel"].firstMatch
        if cancel.waitForExistence(timeout: 3) { cancel.tap() } else { app.swipeDown(velocity: .fast) }
        _ = XCTWaiter.wait(for: [XCTestExpectation(description: "settle")], timeout: 1)
    }
}
