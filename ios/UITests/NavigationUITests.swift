import XCTest

/// Runs on the phone against the live feed: tapping a person opens them, and Back
/// returns to People (regression for pushes landing late in a mixed stack).
final class NavigationUITests: XCTestCase {
    override func setUp() { continueAfterFailure = false }

    func testPeopleOpensPersonAndBackReturns() {
        let app = XCUIApplication()
        app.launch()
        let people = app.buttons["People"]
        XCTAssertTrue(people.waitForExistence(timeout: 20))
        people.tap()
        XCTAssertTrue(app.navigationBars["People"].waitForExistence(timeout: 10))

        let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'person-'")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 30), "no people listed")
        let name = String(row.identifier.dropFirst("person-".count))
        row.tap()
        XCTAssertTrue(app.navigationBars[name].waitForExistence(timeout: 5), "tapping \(name) did not open them")

        app.navigationBars[name].buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["People"].waitForExistence(timeout: 5), "Back did not return to People")
        XCTAssertFalse(app.navigationBars[name].exists)
    }
}
