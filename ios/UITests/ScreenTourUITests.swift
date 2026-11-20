import XCTest
import UIKit

/// The screen tour: runs on the phone against the live feed, opens every main screen and
/// sheet, and attaches a screenshot of each to the test result (`ios/scripts/screens.sh`
/// exports them). It only looks: every sheet is closed with Cancel, Close or Done, and
/// nothing real is saved, deleted or relabelled. With `-screenTourSamples`,
/// Today shows inert sample Who's this cards (the calendar's guess, LifeOS's maybe, then no guess:
/// the tour taps each guess button and one of "Someone else"'s names), Voice Scoring inert sample cards
/// ahead of the real ones (the tour taps each guess button), and Today lists an inert sample meeting with a calendar match and an inert
/// sample chat whose unnamed voice gets LifeOS "maybe" names. The tour relabels, undoes and plays
/// lines only on those samples: Feed sends nothing for them.
final class ScreenTourUITests: XCTestCase {
    private var app: XCUIApplication!
    private var shot = 0
    private var debugShot = 0

    override func setUp() {
        continueAfterFailure = true
        app = XCUIApplication()
        app.launchArguments = ["-screenTourSamples"]
        app.launch()
    }

    func testScreenTour() {
        let today = app.tabBars.buttons["Today"]
        XCTAssertTrue(today.waitForExistence(timeout: 20))
        settle(4)   // let the feed and the Who's this card load

        // Today
        snap("today-top")
        let whos = app.otherElements["whos-this"]
        if whos.waitForExistence(timeout: 5) {
            snap("whos-this", element: whos)
        } else {
            note("whos-this", "No voices to tag: the Who's this card isn't showing.")
        }
        // the inert sample cards: the calendar's guess, LifeOS's maybe, then no guess; one tap on the
        // guess button moves to the next card with no sheet in between
        let sample = app.otherElements["whos-this-sample"]
        if sample.waitForExistence(timeout: 5) {
            for name in ["whos-this-guess-calendar", "whos-this-guess-maybe"] {
                let guess = sample.buttons["guess-confirm"]
                var swipes = 0
                while guess.exists && !guess.isHittable && swipes < 6 { app.swipeUp(velocity: .slow); swipes += 1 }
                settle(1)
                snap(name)
                guard guess.exists else { note(name, "No guess button on the sample card."); break }
                let before = guess.label
                guess.tap()
                settle(1)
                if app.sheets.count > 0 || app.navigationBars.count > 1 { note(name, "A sheet opened after the guess.") }
                if guess.exists && guess.label == before { note(name, "The sample card didn't advance.") }
            }
            if sample.exists {
                snap("whos-this-no-guess")
                if sample.buttons["guess-confirm"].exists { note("whos-this-no-guess", "The last card shows a guess.") }
                // "Someone else" on that card: up to three one-tap names (the calendar's candidates, then a
                // maybe); one tap answers the card
                let someoneElse = sample.buttons["Someone else"]
                if someoneElse.exists {
                    someoneElse.tap()
                    settle(1)
                    let quick = sample.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'someone-else-'"))
                    var swipes = 0
                    while quick.firstMatch.exists && !quick.firstMatch.isHittable && swipes < 4 {
                        app.swipeUp(velocity: .slow); swipes += 1
                    }
                    snap("whos-this-someone-else")
                    if quick.count == 0 || quick.count > 3 {
                        note("whos-this-someone-else", "Expected one to three name buttons, saw \(quick.count).")
                    }
                    if quick.firstMatch.exists && quick.firstMatch.isHittable {
                        quick.firstMatch.tap()
                        settle(1)
                        if app.sheets.count > 0 { note("whos-this-someone-else", "A sheet opened after the name.") }
                        if sample.exists { note("whos-this-someone-else", "The sample card didn't advance.") }
                    }
                } else {
                    note("whos-this-someone-else", "No Someone else button on the sample card.")
                }
            }
        } else {
            note("whos-this-guess-calendar", "The sample Who's this card isn't showing.")
        }
        app.swipeUp(velocity: .slow)
        settle(1)
        snap("today-scrolled")
        app.swipeDown(velocity: .fast)
        app.swipeDown(velocity: .fast)

        // Short clips folded into one row, collapsed and expanded: on Today, else the newest History
        // day that has some (the tour only looks: it opens and closes the fold, nothing is deleted)
        if !foldShots() {
            app.tabBars.buttons["History"].tap()
            let days = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'day-'"))
            var found = false
            if days.firstMatch.waitForExistence(timeout: 10) {
                for i in 0..<min(days.count, 4) where !found {
                    days.element(boundBy: i).tap()
                    found = foldShots()
                    if !found { home("History") }
                }
            }
            if !found { note("short-clips", "No short clips folded on Today or in recent History days.") }
            home("History")
        }
        home("Today")

        // A conversation with its player, and the reassign sheet
        if openConversation() {
            let play = app.buttons["Play"]
            _ = play.waitForExistence(timeout: 20)
            settle(1)
            snap("conversation")
            let chip = app.buttons.matching(identifier: "speaker-chip").firstMatch
            if chip.waitForExistence(timeout: 5) {
                chip.tap()
                settle(2)
                snap("reassign-sheet")
                cancelSheet()
            } else {
                note("reassign-sheet", "This conversation has no speaker names to tap.")
            }
            home("Today")
            home("History")
        } else {
            note("conversation", "No conversation with audio found today or in History.")
        }

        // The calendar line and the "Probably …" confirm, on the tour's inert sample meeting (it only
        // looks: the sheet is cancelled, and Feed and the sheet send nothing for sample conversations)
        home("Today")
        let sampleMeeting = app.buttons["tour-sample-23:58"]
        if openSample(sampleMeeting) {
            settle(2)
            snap("conversation-calendar")
            let chips = app.buttons.matching(identifier: "speaker-chip")
            let undoRows = app.descendants(matching: .any).matching(identifier: "undo-change")
            if chips.firstMatch.waitForExistence(timeout: 5) {
                // The "Probably …" card with its one-tap "Yes, <name> · this conversation".
                chips.firstMatch.tap()
                _ = app.descendants(matching: .any)["likely-person"].waitForExistence(timeout: 5)
                settle(2)
                snap("reassign-likely")
                // "This conversation" says how much it covers (the sample is an hour in four parts)
                let scope = app.buttons["scope-conversation"]
                if !(scope.exists && scope.label.contains("4 parts")) {
                    note("reassign-likely", "The scope row doesn't show the sitting's parts.")
                }
                // A name from the picker, with the default scope: one tap saves it and closes the sheet, with
                // no confirm button. It changes only the inert sample (Feed sends nothing for it), and its
                // Undo row appears. The first known voice in the picker (a short scroll brings it into view).
                let someone = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'suggestion-tagged-'"))
                    .firstMatch
                var scrolls = 0
                while !(someone.exists && someone.isHittable) && scrolls < 3 {
                    app.swipeUp(velocity: .slow)
                    scrolls += 1
                }
                if someone.exists && someone.isHittable {
                    if app.buttons["reassign-confirm"].exists { note("reassign-one-tap", "A confirm button shows for the default scope.") }
                    snap("reassign-one-tap")
                    someone.tap()
                    if !undoRows.firstMatch.waitForExistence(timeout: 5) { note("undo-row", "No Undo row after the first sample change.") }
                    else if !app.staticTexts["undo-change-detail"].exists {
                        note("undo-row", "The Undo row doesn't say what else was named.")
                    }
                    if app.buttons["reassign-confirm"].exists { note("reassign-one-tap", "The sheet stayed open after the tap.") }
                } else {
                    note("reassign-one-tap", "No known voice in the picker.")
                    cancelSheet()
                }
                // "All past conversations" is broad: a name tap only picks, and the confirm button is needed.
                // The sheet is cancelled.
                if chips.firstMatch.waitForExistence(timeout: 5) && chips.firstMatch.isHittable {
                    chips.firstMatch.tap()
                    let all = app.buttons["scope-all"]
                    if all.waitForExistence(timeout: 5) {
                        all.tap()
                        var scrolls = 0
                        while !(someone.exists && someone.isHittable) && scrolls < 3 { app.swipeUp(velocity: .slow); scrolls += 1 }
                        if someone.exists && someone.isHittable { someone.tap() }
                        settle(1)
                        snap("reassign-confirm")
                        if !app.buttons["reassign-confirm"].exists {
                            note("reassign-confirm", "No confirm button with All past conversations.")
                        }
                    } else {
                        note("reassign-confirm", "No All past conversations option.")
                    }
                    cancelSheet()
                }
            } else {
                note("reassign-likely", "The sample meeting showed no speaker name to tap.")
            }
            // A second inert change right after, with the card's one tap on the second voice (only
            // "This line" applies to it): two Undo rows, each with its own timer.
            settle(1)
            if chips.count > 1 {
                chips.element(boundBy: 1).tap()
                let yes = app.buttons["likely-confirm"]
                if yes.waitForExistence(timeout: 5) {
                    yes.tap()
                    _ = XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "count >= 2"),
                                                                       object: undoRows)], timeout: 5)
                    settle(1)
                    snap("undo-row")
                    if undoRows.count < 2 { note("undo-row", "Expected two Undo rows, saw \(undoRows.count).") }
                } else {
                    note("undo-row", "No one-tap confirm on the second sample voice.")
                    cancelSheet()
                }
            } else {
                note("undo-row", "The sample meeting showed no second voice.")
            }
            // Undo them (inert too), so the names go back
            let undoButtons = app.buttons.matching(identifier: "undo-change-button")
            var undone = 0
            while undoButtons.firstMatch.exists && undoButtons.firstMatch.isHittable && undone < 4 {
                undoButtons.firstMatch.tap()
                settle(1)
                undone += 1
            }
            // Remove name on the first sample voice: the picker's destructive row, the confirm that names
            // the scope ("All past conversations" can't apply), and its "Name removed · Undo" row. Inert:
            // Feed sends nothing for the sample, and the change is undone right after.
            if chips.firstMatch.waitForExistence(timeout: 5) && chips.firstMatch.isHittable {
                chips.firstMatch.tap()
                let remove = app.buttons["remove-name"]
                if remove.waitForExistence(timeout: 5) {
                    app.swipeUp(velocity: .slow)        // the row sits under the search field, just below the fold
                    settle(1)
                    snap("reassign-remove-name")
                    remove.tap()
                    settle(1)
                    snap("reassign-remove-confirm")
                    // tapping "All past conversations" scrolls it into view and changes nothing: it shows why not
                    let all = app.buttons["scope-all"]
                    if all.exists {
                        all.tap()
                        settle(1)
                        snap("reassign-remove-scopes")
                        if !app.descendants(matching: .any)["scope-reason-all"].exists {
                            note("reassign-remove-scopes", "No reason under All past conversations while removing.")
                        }
                    } else {
                        note("reassign-remove-scopes", "All past conversations is scrolled out of view; not shown.")
                    }
                    let confirm = app.buttons["reassign-confirm"]
                    if confirm.exists && confirm.isEnabled {
                        confirm.tap()
                        let removedRow = app.descendants(matching: .any).matching(identifier: "undo-change")
                            .matching(NSPredicate(format: "label CONTAINS 'Name removed'")).firstMatch
                        if undoRows.firstMatch.waitForExistence(timeout: 5) {
                            settle(1)
                            snap("undo-row-removed")
                            if !removedRow.exists && !app.staticTexts["Name removed"].exists {
                                note("undo-row-removed", "The Undo row doesn't say \"Name removed\".")
                            }
                        } else {
                            note("undo-row-removed", "No Undo row after Remove name.")
                            if app.buttons["reassign-confirm"].exists { cancelSheet() }
                        }
                        undone = 0
                        while undoButtons.firstMatch.exists && undoButtons.firstMatch.isHittable && undone < 2 {
                            undoButtons.firstMatch.tap()
                            settle(1)
                            undone += 1
                        }
                    } else {
                        note("reassign-remove-confirm", "No confirm button after picking Remove name.")
                        cancelSheet()
                    }
                } else {
                    note("reassign-remove-name", "No Remove name row in the picker.")
                    cancelSheet()
                }
            } else {
                note("reassign-remove-name", "The sample meeting showed no speaker name to tap.")
            }
            // The second voice shares its name with another, so only "This line" can apply: the
            // other scopes show their reason. The sheet is cancelled.
            if chips.count > 1 {
                chips.element(boundBy: 1).tap()
                let reason = app.descendants(matching: .any)["scope-reason-conversation"]
                if reason.waitForExistence(timeout: 5) {
                    let other = app.buttons["scope-conversation"]
                    if other.exists && other.isHittable { other.tap() }   // a disabled scope: stays on "This line"
                    settle(1)
                    snap("reassign-scope-disabled")
                } else {
                    note("reassign-scope-disabled", "No disabled scope reason on the sample's shared voice.")
                }
                cancelSheet()
            } else {
                note("reassign-scope-disabled", "The sample meeting showed no second voice.")
            }
            // A line's time stamp: the sample has no audio, so the line stays loading for the shot.
            let stamp = app.buttons.matching(identifier: "line-stamp").firstMatch
            if stamp.waitForExistence(timeout: 5) && stamp.isHittable {
                stamp.tap()
                if app.descendants(matching: .any)["line-loading"].waitForExistence(timeout: 2) {
                    snap("line-loading")
                } else {
                    note("line-loading", "The tapped line showed no loading state.")
                }
            } else {
                note("line-loading", "The sample meeting showed no line time stamp to tap.")
            }
        } else {
            note("conversation-calendar", "The sample meeting row wasn't found on Today or in recent History days.")
        }
        home("Today")

        // LifeOS's "maybe: <name>?" on the tour's inert sample chat (no calendar event, one unnamed voice):
        // the one-tap confirm, then the picker led by the other maybe with its reasons. Cancelled.
        let sampleChat = app.buttons["tour-sample-23:55"]
        if openSample(sampleChat) {
            let chip = app.buttons.matching(identifier: "speaker-chip").firstMatch
            if chip.waitForExistence(timeout: 5) {
                chip.tap()
                if app.descendants(matching: .any)["maybe-person"].waitForExistence(timeout: 5) {
                    settle(2)
                    snap("reassign-maybe")
                    let other = app.buttons["suggestion-lifeos-Morgan Hale"]
                    var scrolls = 0
                    while !(other.exists && other.isHittable) && scrolls < 3 { app.swipeUp(velocity: .slow); scrolls += 1 }
                    settle(1)
                    snap("reassign-maybe-picker")
                    if !other.exists { note("reassign-maybe-picker", "The second maybe isn't in the picker.") }
                } else {
                    note("reassign-maybe", "No maybe confirm on the sample chat's unnamed voice.")
                }
                cancelSheet()
            } else {
                note("reassign-maybe", "The sample chat showed no speaker name to tap.")
            }
        } else {
            note("reassign-maybe", "The sample chat row wasn't found on Today or in recent History days.")
        }
        home("Today")
        home("History")
        home("Today")

        // Voice Scoring, and Voice sample from its toolbar
        home("Today")
        let scoring = app.buttons["Voice Scoring"].firstMatch
        if scoring.waitForExistence(timeout: 5) {
            scoring.tap()
            settle(4)
            // the inert sample cards ahead of the real ones: the calendar's guess, LifeOS's maybe, then no
            // guess; one tap on the guess button moves on with no sheet
            let scoringSample = app.otherElements["voice-scoring-sample"]
            if scoringSample.waitForExistence(timeout: 5) {
                for name in ["voice-scoring-guess-calendar", "voice-scoring-guess-maybe"] {
                    let guess = scoringSample.buttons["guess-confirm"]
                    snap(name)
                    guard guess.exists && guess.isHittable else { note(name, "No guess button on the sample card."); break }
                    let before = guess.label
                    guess.tap()
                    settle(1)
                    let picker = app.navigationBars.matching(NSPredicate(format: "identifier == %@ OR identifier BEGINSWITH 'Not '",
                                                                         "Who's this?"))
                    if picker.count > 0 { note(name, "The picker sheet opened after the guess.") }
                    if guess.exists && guess.label == before { note(name, "The sample card didn't advance.") }
                }
                if scoringSample.exists {
                    snap("voice-scoring-no-guess")
                    if scoringSample.buttons["guess-confirm"].exists { note("voice-scoring-no-guess", "The last card shows a guess.") }
                    // past the no-guess card to the confirm of a voice a "this conversation" answer named
                    let stranger = scoringSample.buttons["Stranger"].firstMatch
                    if stranger.exists { stranger.tap(); settle(1) }
                    if scoringSample.exists {
                        snap("voice-scoring-confirm-reached")
                        if !scoringSample.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Not '")).firstMatch.exists
                            && !scoringSample.buttons.matching(NSPredicate(format: "label BEGINSWITH 'No, it isn'")).firstMatch.exists {
                            note("voice-scoring-confirm-reached", "No \"Not <name>\" button on the confirm card.")
                        }
                    }
                }
            } else {
                note("voice-scoring-guess-calendar", "The sample Voice Scoring card isn't showing.")
            }
            snap("voice-scoring")
            let sample = app.buttons["Record a voice sample"].firstMatch
            if sample.waitForExistence(timeout: 5) {
                sample.tap()
                settle(2)
                snap("voice-sample")
                app.navigationBars["Voice sample"].buttons["Close"].tap()
                settle(1)
            }
            app.navigationBars["Voice Scoring"].buttons["Done"].tap()
            settle(1)
        }

        // People, a person, Change LifeOS person
        let people = app.navigationBars.buttons["People"].firstMatch
        if people.waitForExistence(timeout: 5) {
            people.tap()
            XCTAssertTrue(app.navigationBars["People"].waitForExistence(timeout: 10))
            settle(3)
            snap("people")
            let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'person-'")).firstMatch
            if row.waitForExistence(timeout: 20) {
                let name = String(row.identifier.dropFirst("person-".count))
                row.tap()
                _ = app.navigationBars[name].waitForExistence(timeout: 5)
                settle(2)
                snap("person")
                let change = app.buttons["change-lifeos-person"]
                if change.waitForExistence(timeout: 5) {
                    change.tap()
                    // it opens on the LifeOS matches for the person's name
                    let match = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'suggestion-'")).firstMatch
                    if !match.waitForExistence(timeout: 10) { note("change-lifeos-person", "No LifeOS match listed on open.") }
                    settle(1)
                    snap("change-lifeos-person")
                    cancelSheet()
                }
            }
            home("Today")
        }

        // History, Recently deleted
        app.tabBars.buttons["History"].tap()
        settle(3)
        snap("history")
        let trash = app.buttons["recently-deleted"].firstMatch
        if trash.waitForExistence(timeout: 5) {
            trash.tap()
            settle(3)
            snap("recently-deleted")
            home("History")
        }

        // Settings (the Pendants tab), and Search
        app.tabBars.buttons["Pendants"].tap()
        settle(2)
        snap("settings-pendants")
        // the last row must scroll fully clear of the tab bar
        for _ in 0..<4 { app.swipeUp(velocity: .fast) }
        settle(2)
        snap("settings-pendants-bottom")
        app.tabBars.buttons["Search"].tap()
        settle(2)
        snap("search")
        app.tabBars.buttons["Today"].tap()
    }

    // MARK: helpers

    /// Scrolls to the fold row, shots it collapsed then expanded, and folds it again. False if the day has none.
    private func foldShots() -> Bool {
        let fold = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier == 'short-clips-row' OR label CONTAINS 'hidden · Show'")).firstMatch
        settle(2)
        // Start from the top, then look down the day and, failing that, back up: the row sits at
        // the newest hidden clip's slot, which may be above wherever the list happens to be.
        for _ in 0..<6 { app.swipeDown(velocity: .fast) }
        settle(1)
        var tries = 0
        while !(fold.exists && fold.isHittable) && tries < 14 { app.swipeUp(velocity: .slow); tries += 1 }
        while !(fold.exists && fold.isHittable) && tries < 28 { app.swipeDown(velocity: .slow); tries += 1 }
        guard fold.exists && fold.isHittable else {
            debugShot += 1
            note("fold-debug-\(debugShot)", "exists=\(fold.exists) tries=\(tries)\n" + String(app.debugDescription.suffix(5000)))
            return false
        }
        app.swipeUp(velocity: .slow)   // bring it clear of the tab bar
        settle(1)
        // a long day can scroll it out of view: find it again rather than tap nothing (a failed tap ends the tour)
        var back = 0
        while !(fold.exists && fold.isHittable) && back < 4 { app.swipeDown(velocity: .slow); back += 1 }
        snap("short-clips-collapsed")
        guard fold.exists && fold.isHittable else {
            note("short-clips-expanded", "The fold row scrolled out of reach after the collapsed shot; not expanded.")
            return true
        }
        fold.tap()
        settle(1)
        snap("short-clips-expanded")
        // Swipe a shown sample clip to show its actions, then cancel. Only the tour's inert
        // samples: a full swipe on a real clip would delete it.
        let sample = app.staticTexts["Sample clip"].firstMatch
        if sample.exists && sample.isHittable {
            let start = sample.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5))
            start.press(forDuration: 0.05, thenDragTo: sample.coordinate(withNormalizedOffset: CGVector(dx: 0.55, dy: 0.5)))
            settle(1)
            snap("short-clips-swipe")
            start.press(forDuration: 0.05, thenDragTo: sample.coordinate(withNormalizedOffset: CGVector(dx: 1.4, dy: 0.5)))
            settle(1)
        } else {
            note("short-clips-swipe", "No sample clip shown, so the swipe step was skipped (never swipes real clips).")
        }
        if fold.exists && fold.isHittable { fold.tap(); settle(1) }
        return true
    }

    /// Taps a tour sample row: on Today, or (Today has no conversations yet, so no samples) on the newest
    /// History day that has them. False if it isn't found.
    private func openSample(_ row: XCUIElement) -> Bool {
        func reach() -> Bool {
            _ = row.waitForExistence(timeout: 10)
            var tries = 0
            while !(row.exists && row.isHittable) && tries < 8 { app.swipeUp(velocity: .slow); tries += 1 }
            tries = 0
            while !(row.exists && row.isHittable) && tries < 8 { app.swipeDown(velocity: .slow); tries += 1 }
            return row.exists && row.isHittable
        }
        var found = reach()
        if !found {
            app.tabBars.buttons["History"].tap()
            let days = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'day-'"))
            if days.firstMatch.waitForExistence(timeout: 10) {
                for i in 0..<min(days.count, 4) where !found {
                    days.element(boundBy: i).tap()
                    found = reach()
                    if !found { home("History") }
                }
            }
        }
        if found { row.tap() }
        return found
    }

    /// Opens the newest conversation on Today, else the newest in History.
    private func openConversation() -> Bool {
        let rows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'conversation-'"))
        if rows.firstMatch.waitForExistence(timeout: 5) {
            rows.firstMatch.tap()
            return true
        }
        app.tabBars.buttons["History"].tap()
        let days = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'day-'"))
        guard days.firstMatch.waitForExistence(timeout: 10) else { return false }
        let count = min(days.count, 4)
        for i in 0..<count {
            days.element(boundBy: i).tap()
            if rows.firstMatch.waitForExistence(timeout: 8) {
                rows.firstMatch.tap()
                return true
            }
            home("History")
        }
        return false
    }

    /// Back to a tab's first screen (tapping the selected tab pops its stack).
    private func home(_ tab: String) {
        let b = app.tabBars.buttons[tab]
        guard b.waitForExistence(timeout: 5) else { return }
        b.tap()
        settle(1)
        b.tap()
        settle(1)
    }

    private func cancelSheet() {
        let cancel = app.buttons["Cancel"].firstMatch
        if cancel.waitForExistence(timeout: 3) { cancel.tap() } else { app.swipeDown(velocity: .fast) }
        settle(1)
    }

    private func settle(_ seconds: TimeInterval) {
        _ = XCTWaiter.wait(for: [XCTestExpectation(description: "settle")], timeout: seconds)
    }

    private func snap(_ name: String, element: XCUIElement? = nil) {
        shot += 1
        let a: XCTAttachment
        if let element, let crop = crop(element) { a = XCTAttachment(image: crop) }
        else { a = XCTAttachment(screenshot: app.screenshot()) }
        a.name = String(format: "%02d-%@", shot, name)
        a.lifetime = .keepAlways
        add(a)
    }

    /// The element cut out of a full-screen shot, once its frame has stopped moving (a card that is
    /// still animating in gave XCUIElement.screenshot() a wrong, black frame). Nil if it never settles
    /// or the frame isn't on screen; the caller then attaches the whole screen.
    private func crop(_ element: XCUIElement) -> UIImage? {
        guard element.exists else { return nil }
        var last = element.frame
        for _ in 0..<10 {
            settle(0.5)
            let f = element.frame
            if f == last { break }
            last = f
        }
        let full = app.screenshot().image
        let screen = CGRect(origin: .zero, size: full.size)
        guard last.width > 100, last.height > 100, screen.contains(last), let cg = full.cgImage else { return nil }
        let s = full.scale
        guard let cut = cg.cropping(to: CGRect(x: last.minX * s, y: last.minY * s,
                                               width: last.width * s, height: last.height * s)) else { return nil }
        return UIImage(cgImage: cut, scale: s, orientation: .up)
    }

    private func note(_ name: String, _ text: String) {
        let a = XCTAttachment(string: text)
        a.name = "note-\(name)"
        a.lifetime = .keepAlways
        add(a)
    }
}
