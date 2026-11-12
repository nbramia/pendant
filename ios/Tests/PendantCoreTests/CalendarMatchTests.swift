import XCTest
@testable import PendantCore

final class CalendarMatchTests: XCTestCase {
    func testDecodesTheDayFeedCalendarAndLine() throws {
        let json = """
        {"id": "ev", "title": "Morgan-Noah week-ahead prep", "start_ms": 1, "end_ms": 2, "shared": false,
         "attendees": [{"name": "Noah Park", "email": "noah@work.example", "entity_id": "e-me", "wearer": true},
                       {"name": "Morgan Hale", "email": "morgan@work.example", "entity_id": "e-morgan", "wearer": false},
                       {"name": "brandon@vendor.example", "email": "brandon@vendor.example", "entity_id": null}]}
        """
        let m = try JSONDecoder().decode(CalendarMatch.self, from: Data(json.utf8))
        XCTAssertEqual(m.others, ["Morgan Hale", "brandon@vendor.example"])
        XCTAssertEqual(m.line, "Morgan-Noah week-ahead prep · Morgan Hale, brandon@vendor.example")
        XCTAssertEqual(m.attendees[1].entityId, "e-morgan")
        XCTAssertEqual(CalendarMatch(title: "Focus").line, "Focus")
    }

    func testMeetingLinks() {
        let m = MeetingLink(note: "Work/Team/Dana-Noah weekly 20261109", source: "granola")
        XCTAssertEqual(m.app, "Granola")
        XCTAssertEqual(m.name, "Dana-Noah weekly 20261109")
        XCTAssertEqual(m.title, "Also recorded in Granola: Dana-Noah weekly 20261109")
        XCTAssertEqual(m.lead, "Also recorded in Granola:")
        XCTAssertEqual(MeetingLink(note: "X", source: "wispr", shared: true).title, "Same meeting in Wispr: X")
        XCTAssertNil(m.url, "no Obsidian vault configured (PENDANT_OBSIDIAN_VAULT): no link")
    }

    func testPendantNamesSetting() {
        XCTAssertEqual(AppSettings.parseNames("a1b2c3d4e5f60718=new, 0F1E2D3C4B5A6978 = old,bad,=x,Y="),
                       ["A1B2C3D4E5F60718": "new", "0F1E2D3C4B5A6978": "old"])
        XCTAssertEqual(AppSettings.parseNames(""), [:])
        XCTAssertEqual(AppSettings.serverName, "the server")
        XCTAssertEqual(AppSettings.serverNameAtStart, "The server")
    }

    func testDecodesAHintAndAnOldServerWithout() throws {
        let json = """
        {"likely": {"name": "Morgan Hale", "label": "Morgan Hale", "entity_id": "e-morgan", "source": "calendar",
                    "event": "Morgan-Noah week-ahead prep", "known": true, "voiceprint": false},
         "candidates": [], "event": null}
        """
        let h = try JSONDecoder().decode(CalendarHint.self, from: Data(json.utf8))
        XCTAssertEqual(h.likely?.prompt, "Probably Morgan Hale")
        XCTAssertEqual(h.likely?.source, "from Morgan-Noah week-ahead prep")
        XCTAssertEqual(h.likely?.confirmTitle, "Yes, Morgan Hale")
        XCTAssertEqual(try JSONDecoder().decode(CalendarHint.self, from: Data("{}".utf8)), .none)
        let card = try JSONDecoder().decode(VoiceCard.self, from: Data(#"{"id": 3}"#.utf8))
        XCTAssertNil(card.likely)
        XCTAssertNil(card.likelyOther)
    }

    func testHintPathEncodesTheTag() {
        XCTAssertEqual(Suggestions.hintPath(deviceId: "BEEF", startMs: 12, tag: "Speaker A"),
                       "conversations/BEEF/12/speaker/Speaker%20A/suggest")
        XCTAssertEqual(Suggestions.hintPath(deviceId: "BEEF", startMs: 12, tag: "A/B?"),
                       "conversations/BEEF/12/speaker/A%2FB%3F/suggest")
    }

    func testCandidatesHeadThePickerOnce() {
        let morgan = PersonSuggestion(name: "Morgan Hale", entityId: "e-morgan", detail: "from Prep")
        let dana = PersonSuggestion(name: "Dana Brooks", entityId: "e-dana", detail: "from Prep")
        let rows = [PersonSuggestion(name: "Alex Rivera", entityId: "e-lex", known: true),
                    PersonSuggestion(name: "Morgan Hale", entityId: "e-morgan", circle: 1),
                    PersonSuggestion(name: "Mo", isNew: true)]
        XCTAssertEqual(Suggestions.leading(rows, with: [morgan, dana]).map(\.name),
                       ["Morgan Hale", "Dana Brooks", "Alex Rivera", "Mo"])
        XCTAssertEqual(Suggestions.leading(rows, with: [morgan, dana], query: "mo").map(\.name),
                       ["Morgan Hale", "Alex Rivera", "Mo"])
        XCTAssertEqual(Suggestions.leading(rows, with: [], query: "").map(\.name), rows.map(\.name))
    }

    func testLikelyIsOfferedUnlessTheCardAlreadyAsksAboutThem() {
        let morgan = LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep")
        XCTAssertEqual(VoiceCard(id: 1, likely: morgan).likelyOther, morgan)
        XCTAssertEqual(VoiceCard(id: 1, currentLabel: "Alex Rivera", likely: morgan).likelyOther, morgan)
        XCTAssertNil(VoiceCard(id: 1, currentLabel: "morgan hale", likely: morgan).likelyOther)
    }

    func testConfirmingTheLikelyPersonIsAHandLabelLinkedToThem() {
        let morgan = LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep")
        var deck = VoiceDeck([VoiceCard(id: 7, likely: morgan), VoiceCard(id: 8)])
        let step = deck.confirmLikely()
        XCTAssertEqual(step, .submit(.suggested("Morgan Hale", entityId: "e-morgan")))
        guard case .submit(let a) = step else { return XCTFail() }
        let req = a.request(cardID: 7)
        XCTAssertEqual(req.path, "speakers/7/label")
        XCTAssertEqual(req.body, ["name": "Morgan Hale", "source": "user", "entity_id": "e-morgan"])
        deck.finished(success: true)
        XCTAssertEqual(deck.confirmLikely(), .none)              // card 8 has no likely person: never automatic
        XCTAssertEqual(deck.current?.id, 8)
    }
}
