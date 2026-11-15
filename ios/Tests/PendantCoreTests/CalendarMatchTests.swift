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

    func testDecodesMaybePeopleAndAnOldServerWithout() throws {
        let json = """
        {"likely": null, "candidates": [], "event": null,
         "maybe": [{"name": "Alex Rivera", "label": "Alex Rivera", "entity_id": "e-lex", "source": "lifeos",
                    "reasons": ["texted on my way"], "detail": "texted on my way", "known": true, "voiceprint": true},
                   {"name": "Morgan Hale", "entity_id": "e-morgan"}]}
        """
        let h = try JSONDecoder().decode(CalendarHint.self, from: Data(json.utf8))
        XCTAssertEqual(h.maybe.map(\.name), ["Alex Rivera", "Morgan Hale"])
        XCTAssertEqual(h.maybe[0].prompt, "maybe: Alex Rivera?")
        XCTAssertEqual(h.maybe[0].source, "texted on my way")
        XCTAssertEqual(h.maybe[0].confirmTitle, "Yes, Alex Rivera")
        XCTAssertEqual(h.maybe[1].label, "Morgan Hale")
        XCTAssertEqual(h.maybe[1].source, "from LifeOS")
        let row = h.maybe[0].suggestion
        XCTAssertEqual(row.entityId, "e-lex")
        XCTAssertEqual(row.subtitle, "voice saved · texted on my way")
        let old = try JSONDecoder().decode(CalendarHint.self, from: Data(#"{"likely": null, "candidates": []}"#.utf8))
        XCTAssertEqual(old.maybe, [])
    }

    func testTheMaybeChipIsForAnUnnamedVoiceWithoutACalendarGuess() {
        let alex = MaybePerson(name: "Alex Rivera", entityId: "e-lex", reasons: ["texted on my way"])
        let morgan = MaybePerson(name: "Morgan Hale", entityId: "e-morgan", reasons: ["lives with you"])
        let h = CalendarHint(maybe: [alex, morgan])
        XCTAssertEqual(h.maybeChip(for: "Speaker B"), alex)
        XCTAssertEqual(h.maybeChip(for: "Someone"), alex)
        XCTAssertNil(h.maybeChip(for: "Riley Park"), "a named voice gets no chip")
        // the other maybe heads the picker with its reason; the chip's person isn't repeated
        XCTAssertEqual(h.pickerLead(for: "Speaker B").map(\.name), ["Morgan Hale"])
        XCTAssertEqual(h.pickerLead(for: "Speaker B").first?.subtitle, "lives with you")
        // a named voice: every maybe in the picker, none on its own name
        XCTAssertEqual(h.pickerLead(for: "Riley Park").map(\.name), ["Alex Rivera", "Morgan Hale"])
        XCTAssertEqual(h.pickerLead(for: "alex rivera").map(\.name), ["Morgan Hale"])
    }

    func testTheCalendarStaysAheadOfTheMaybes() {
        let alex = MaybePerson(name: "Alex Rivera", entityId: "e-lex")
        let riley = PersonSuggestion(name: "Riley Park", entityId: "e-riley", detail: "from Prep")
        let likely = LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep")
        let withLikely = CalendarHint(likely: likely, maybe: [alex])
        XCTAssertEqual(withLikely.likely(for: "Speaker B"), likely)
        XCTAssertNil(withLikely.maybeChip(for: "Speaker B"), "the calendar's one tap comes first")
        XCTAssertEqual(withLikely.pickerLead(for: "Speaker B").map(\.name), ["Alex Rivera"])
        // the calendar's guess is already the voice's name: it isn't offered, so the maybe is
        XCTAssertNil(withLikely.likely(for: "Morgan Hale"))
        let candidates = CalendarHint(candidates: [riley], maybe: [alex, MaybePerson(name: "Dana Brooks", entityId: "e-dana")])
        // calendar candidates and no single likely person: no maybe card; the maybes follow the candidates
        XCTAssertNil(candidates.maybeChip(for: "Speaker A"), "the calendar's candidates come before any maybe card")
        XCTAssertEqual(candidates.pickerLead(for: "Speaker A").map(\.name), ["Riley Park", "Alex Rivera", "Dana Brooks"])
        XCTAssertEqual(CalendarHint(maybe: [alex]).maybeChip(for: "Speaker A"), alex, "no calendar hint: the card shows")
        XCTAssertNil(CalendarHint.none.maybeChip(for: "Speaker A"))
        XCTAssertEqual(CalendarHint.none.pickerLead(for: "Speaker A"), [])
    }

    func testConfirmingAMaybeIsTheSameHandLabelAsPickingThem() {
        // the reassign sheet's one tap saves the maybe's label and LifeOS id with source "user"
        let alex = MaybePerson(name: "Alex Rivera", label: "Alex R", entityId: "e-lex")
        let req = Reassign.request(deviceId: "BEEF", startMs: 12, scope: .conversation, tag: "Speaker B",
                                   paragraph: 0, name: alex.label, entityId: alex.entityId)
        let picked = Reassign.request(deviceId: "BEEF", startMs: 12, scope: .conversation, tag: "Speaker B",
                                      paragraph: 0, name: alex.suggestion.label, entityId: alex.suggestion.entityId)
        XCTAssertEqual(req.path, picked.path)
        XCTAssertEqual(req.body as NSDictionary, picked.body as NSDictionary)
        XCTAssertEqual(req.body["source"] as? String, "user")
        XCTAssertEqual(req.body["entity_id"] as? String, "e-lex")
        XCTAssertEqual(req.body["name"] as? String, "Alex R")
    }
}
