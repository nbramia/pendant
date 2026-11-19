import XCTest

/// Runs on the phone against the live feed: the search-only homepage, the voice-sample picker
/// and the honest feed error. Each test attaches the screenshots the PR reviews.
final class FeedAndVoiceUITests: XCTestCase {
    override func setUp() { continueAfterFailure = false }

    private func shot(_ name: String) {
        let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: id).firstMatch
    }

    private func launch(_ extra: [String] = []) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += extra
        app.launch()
        return app
    }

    func testTodayShowsOnlyMatchesWhileAQueryIsEntered() {
        let app = launch()
        XCTAssertTrue(element(app, "live-header").waitForExistence(timeout: 30), "Today shows its header")
        XCTAssertTrue(element(app, "day-stats").exists)
        shot("20-today-no-query")

        let search = app.searchFields.firstMatch
        if !search.waitForExistence(timeout: 3) { app.swipeDown() }      // the field sits above the list until pulled
        XCTAssertTrue(search.waitForExistence(timeout: 10))
        search.tap()
        search.typeText("the")
        XCTAssertTrue(element(app, "match-count").waitForExistence(timeout: 10)
                      || app.staticTexts["No matches"].waitForExistence(timeout: 2), "a count or No matches")
        XCTAssertFalse(element(app, "live-header").exists, "header hidden while searching")
        XCTAssertFalse(element(app, "day-stats").exists, "stats hidden while searching")
        XCTAssertFalse(element(app, "whos-this").exists, "Who's this card hidden while searching")
        XCTAssertFalse(app.staticTexts["conversations"].exists)
        shot("21-today-with-query")

        search.buttons["Clear text"].tap()
        XCTAssertTrue(element(app, "live-header").waitForExistence(timeout: 10), "header returns when cleared")
    }

    func testVoiceSamplePickerListsTaggedPeopleThenLifeOS() {
        let app = launch()
        let people = app.navigationBars.buttons["People"]
        XCTAssertTrue(people.waitForExistence(timeout: 30))
        people.tap()
        let record = app.buttons["Record a voice sample"]
        XCTAssertTrue(record.waitForExistence(timeout: 15))
        record.tap()

        let rows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'suggestion-'"))
        XCTAssertTrue(rows.firstMatch.waitForExistence(timeout: 20), "no suggestions")
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'suggestion-tagged-'")).firstMatch
            .waitForExistence(timeout: 10), "a tagged person is listed with an empty query")
        XCTAssertTrue(rows.firstMatch.identifier.hasPrefix("suggestion-tagged-"), "tagged people come first")
        shot("22-voice-sample-picker")

        let field = app.textFields["Search for a name"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText("an")
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'suggestion-lifeos-'")).firstMatch
            .waitForExistence(timeout: 20), "a LifeOS search result is listed")
        shot("23-voice-sample-picker-search")

        // the field only searches: no button beside it, and a typed name is the last row
        XCTAssertFalse(app.buttons["Next"].exists && app.buttons["Next"].isHittable, "no Next beside the field")
        field.typeText("zzqxv")
        let newRow = app.buttons["suggestion-new"]
        XCTAssertTrue(newRow.waitForExistence(timeout: 20), "the typed name is offered as the last row")
        let last = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'suggestion-'")).allElementsBoundByIndex.last
        XCTAssertEqual(last?.identifier, "suggestion-new")
        shot("25-voice-sample-new-name-row")
        field.typeText("\n")       // submit: not an exact match, so nothing is chosen
        XCTAssertTrue(app.navigationBars["Voice sample"].exists)
        XCTAssertTrue(field.exists, "still on the picker")
    }

    func testAnUnreachableFeedSaysSoAndNamesTheCause() {
        let app = launch(["-feedBaseOverride", "http://127.0.0.1:9/pendant/feed"])
        let history = app.tabBars.buttons["History"]
        XCTAssertTrue(history.waitForExistence(timeout: 30))
        history.tap()
        let trash = app.buttons["Recently deleted"]
        XCTAssertTrue(trash.waitForExistence(timeout: 10))
        trash.tap()
        let card = element(app, "feed-error")
        XCTAssertTrue(card.waitForExistence(timeout: 60), "an error card shows")
        XCTAssertTrue(card.staticTexts.matching(NSPredicate(format: "label CONTAINS 'No connection to '")).firstMatch.exists
                      || card.label.contains("No connection to "), "the error names the cause")
        XCTAssertTrue(card.buttons["Try again"].exists, "a Try again button")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Is Tailscale on?'")).firstMatch.exists)
        shot("24-feed-error")
    }
}

/// Screenshots of the shared picker in each place it appears: with an empty query and with a
/// typed query (LifeOS results, then the typed name as the last row). Only looks and types in the
/// search field: every sheet is cancelled.
final class PickerShotsUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = true
        app = XCUIApplication()
        app.launchArguments = ["-screenTourSamples"]
        app.launch()
    }

    private func shot(_ name: String) {
        let a = XCTAttachment(screenshot: app.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    private func settle(_ s: TimeInterval) { _ = XCTWaiter.wait(for: [XCTestExpectation(description: "s")], timeout: s) }

    private func note(_ name: String, _ why: String) {
        let a = XCTAttachment(string: why)
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    /// Empty, then typed ("an" plus letters no one has): both screenshots.
    private func pickerShots(_ empty: String, _ typed: String, placeholder: String) {
        settle(4)
        shot(empty)
        let field = app.textFields[placeholder].firstMatch
        guard field.waitForExistence(timeout: 5) else { note(typed, "no field \(placeholder)"); return }
        field.tap()
        field.typeText("anzzqxv")
        settle(4)
        shot(typed)
    }

    private func cancel() {
        let c = app.buttons["Cancel"].firstMatch
        if c.waitForExistence(timeout: 3) { c.tap() } else { app.swipeDown(velocity: .fast) }
        settle(1)
    }

    func testPickers() {
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 30))
        settle(4)

        // Who's this: "Someone else"
        let someone = app.buttons["Someone else"].firstMatch
        if someone.waitForExistence(timeout: 5) {
            someone.tap()
            settle(2)
            shot("34-whos-this-someone-else")
        } else { note("34-whos-this-someone-else", "no Who's this card") }

        // Voice Scoring: "No" opens the picker
        let scoring = app.buttons["Voice Scoring"].firstMatch
        if scoring.waitForExistence(timeout: 5) {
            scoring.tap()
            settle(4)
            // past the inert sample cards to a real one (Stranger on a sample sends nothing)
            var skips = 0
            while app.otherElements["voice-scoring-sample"].exists && skips < 4 {
                app.buttons["Stranger"].firstMatch.tap()
                settle(1)
                skips += 1
            }
            let no = app.buttons["No"].firstMatch
            if no.waitForExistence(timeout: 10) {
                no.tap()
                pickerShots("35-voice-scoring-no-picker-empty", "36-voice-scoring-no-picker-typed", placeholder: "Search for a name")
                cancel()
            } else { note("35-voice-scoring-no-picker-empty", "no No button") }
            let done = app.navigationBars["Voice Scoring"].buttons["Done"]
            if done.waitForExistence(timeout: 3) { done.tap() }
            settle(1)
        }

        // Reassign sheet from a conversation's speaker chip
        let rows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'conversation-'"))
        if rows.firstMatch.waitForExistence(timeout: 10) {
            rows.firstMatch.tap()
            let chip = app.buttons.matching(identifier: "speaker-chip").firstMatch
            if chip.waitForExistence(timeout: 20) {
                chip.tap()
                pickerShots("30-reassign-empty", "31-reassign-typed", placeholder: "Search for a name")
                cancel()
            } else { note("30-reassign-empty", "no speaker chip") }
            app.navigationBars.buttons.element(boundBy: 0).tap()
        }

        // Change LifeOS person
        let people = app.navigationBars.buttons["People"].firstMatch
        if people.waitForExistence(timeout: 5) {
            people.tap()
            let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'person-'")).firstMatch
            if row.waitForExistence(timeout: 20) {
                row.tap()
                let change = app.buttons["change-lifeos-person"]
                if change.waitForExistence(timeout: 10) {
                    change.tap()
                    pickerShots("32-change-person-empty", "33-change-person-typed", placeholder: "Search for a person")
                    cancel()
                }
            }
        }
    }
}
