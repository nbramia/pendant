import XCTest
@testable import PendantCore

final class AnsweredVoicesTests: XCTestCase {
    private func cards(_ ids: Int...) -> [VoiceCard] { ids.map { VoiceCard(id: $0) } }

    func testAnAnswerOnOneScreenLeavesTheOtherScreensList() {
        var a = AnsweredVoices()
        // Voice Scoring: a name for 2 that also covered 4 elsewhere in the conversation, a skip of 5
        a.add([2, 4], on: "2026-11-17")
        a.add([5], on: "2026-11-17")
        // the Who's this card's list, fetched later (or a stale copy fetched before the answers)
        XCTAssertEqual(a.unanswered(cards(1, 2, 3, 4, 5), on: "2026-11-17").map(\.id), [1, 3])
        XCTAssertTrue(a.contains(4, on: "2026-11-17"))
    }

    func testANewDayStartsEmpty() {
        var a = AnsweredVoices()
        a.add([5], on: "2026-11-17")
        // the server offers a skipped voice again the next day: it shows again
        XCTAssertEqual(a.unanswered(cards(5, 6), on: "2026-11-18").map(\.id), [5, 6])
        XCTAssertFalse(a.contains(5, on: "2026-11-18"))
        a.add([6], on: "2026-11-18")
        XCTAssertEqual(a.ids, [6])
        XCTAssertEqual(a.unanswered(cards(5, 6), on: "2026-11-18").map(\.id), [5])
    }

    func testRoundTripsAndNamesTheLocalDay() throws {
        var a = AnsweredVoices()
        a.add([7, 8], on: "2026-11-17")
        let back = try JSONDecoder().decode(AnsweredVoices.self, from: JSONEncoder().encode(a))
        XCTAssertEqual(back, a)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "America/New_York")!
        // 03:30 UTC on the 18th is still the evening of the 17th in New York
        let evening = Date(timeIntervalSince1970: 1_794_972_600)
        XCTAssertEqual(AnsweredVoices.day(of: evening, calendar: cal), "2026-11-17")
        XCTAssertEqual(AnsweredVoices.day(of: evening, calendar: AnsweredVoices.serverCalendar), "2026-11-17")
    }
}
