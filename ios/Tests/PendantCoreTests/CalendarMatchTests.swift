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
        let step = deck.confirmGuess()
        XCTAssertEqual(step, .submit(.suggested("Morgan Hale", entityId: "e-morgan")))
        guard case .submit(let a) = step else { return XCTFail() }
        let req = a.request(cardID: 7)
        XCTAssertEqual(req.path, "speakers/7/label")
        XCTAssertEqual(req.body, ["name": "Morgan Hale", "source": "user", "entity_id": "e-morgan"])
        deck.finished(success: true)
        XCTAssertEqual(deck.confirmGuess(), .none)               // card 8 has no likely person: never automatic
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

    func testTheCardGuessIsTheCalendarsPersonElseTheFirstMaybe() {
        let alex = MaybePerson(name: "Alex Rivera", entityId: "e-lex", reasons: ["texted on my way", "home all evening"])
        let morgan = LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep")
        // the calendar's one likely person comes first, with its event as the reason
        let cal = CalendarHint(likely: morgan, maybe: [alex]).cardGuess(asking: nil)
        XCTAssertEqual(cal, CardGuess(name: "Morgan Hale", entityId: "e-morgan", reason: "from Prep", fromCalendar: true))
        XCTAssertEqual(cal?.title, "Morgan Hale?")
        // the calendar offers nobody: LifeOS's first maybe, its reasons under the name
        let maybe = CalendarHint(maybe: [alex]).cardGuess(asking: nil)
        XCTAssertEqual(maybe?.name, "Alex Rivera")
        XCTAssertEqual(maybe?.reason, "texted on my way · home all evening")
        XCTAssertEqual(maybe?.fromCalendar, false)
        // calendar candidates but no single person: no maybe guess (the picker lists them)
        let riley = PersonSuggestion(name: "Riley Park", entityId: "e-riley", detail: "from Prep")
        XCTAssertNil(CalendarHint(candidates: [riley], maybe: [alex]).cardGuess(asking: nil))
        // a card already asking about a name gets no maybe, and no calendar guess of that same name
        XCTAssertNil(CalendarHint(maybe: [alex]).cardGuess(asking: "Riley Park"))
        XCTAssertNil(CalendarHint(likely: morgan).cardGuess(asking: "morgan hale"))
        XCTAssertEqual(CalendarHint(likely: morgan).cardGuess(asking: "Riley Park")?.name, "Morgan Hale")
        // no hints: the card is as it was
        XCTAssertNil(CalendarHint.none.cardGuess(asking: nil))
    }

    func testTheCardGuessSendsAHandLabelWithTheLifeOSId() {
        let g = CardGuess(name: "Alex Rivera", label: "Alex", entityId: "e-lex", reason: "texted on my way",
                          fromCalendar: false)
        let r = g.answer.request(cardID: 42)
        XCTAssertEqual(r.path, "speakers/42/label")
        XCTAssertEqual(r.body, ["name": "Alex", "source": "user", "entity_id": "e-lex"])
    }

    func testCardsDecodeTheirHints() throws {
        let json = """
        {"likely": null, "candidates": [], "maybe": [
          {"name": "Alex Rivera", "label": "Alex Rivera", "entity_id": "e-lex", "reasons": ["texted on my way"],
           "source": "lifeos", "known": false, "voiceprint": false, "detail": "texted on my way"}]}
        """
        let h = try JSONDecoder().decode(CalendarHint.self, from: Data(json.utf8))
        XCTAssertEqual(h.cardGuess(asking: nil)?.answer, .suggested("Alex Rivera", entityId: "e-lex"))
    }

    private struct Card: HintedCard, Equatable {
        let id: Int
        let conversation: CardConversation?
        var hint: CalendarHint
    }

    func testNamingAVoiceTakesThatPersonOffTheOtherCardsOfItsConversation() {
        let alex = MaybePerson(name: "Alex Rivera", entityId: "e-lex", reasons: ["texted on my way"])
        let morgan = MaybePerson(name: "Morgan Hale", entityId: "e-morgan", reasons: ["lives with you"])
        let here = CardConversation(device: "0F1E2D3C4B5A6978", startMs: 1000, tag: "Speaker A")
        let first = Card(id: 1, conversation: here, hint: CalendarHint(maybe: [alex, morgan]))
        let second = Card(id: 2, conversation: CardConversation(device: "0f1e2d3c4b5a6978", startMs: 1000, tag: "Speaker B"),
                          hint: CalendarHint(maybe: [alex, morgan]))
        let elsewhere = Card(id: 3, conversation: CardConversation(device: "0F1E2D3C4B5A6978", startMs: 9000),
                             hint: CalendarHint(maybe: [alex]))
        XCTAssertEqual(second.hint.cardGuess(asking: nil)?.name, "Alex Rivera")
        // one tap on the first card's guess names Alex: the second card falls to its next guess
        let g = first.hint.cardGuess(asking: nil)!
        guard case let .suggested(name, entityId) = g.answer else { return XCTFail("not a hand label") }
        let after = CardHints.named([first, second, elsewhere], answered: first, name: name, entityId: entityId)
        XCTAssertEqual(after[1].hint.cardGuess(asking: nil)?.name, "Morgan Hale")
        XCTAssertEqual(after[0], first, "the answered card is left as it was")
        XCTAssertEqual(after[2], elsewhere, "another conversation keeps its guess")
        // the calendar's likely person, named by hand without an id: the card falls to no guess
        let likely = LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep")
        let cal = Card(id: 4, conversation: here, hint: CalendarHint(likely: likely))
        let byName = CardHints.named([first, cal], answered: first, name: "morgan hale", entityId: nil)
        XCTAssertNil(byName[1].hint.cardGuess(asking: nil))
        // a different person who shares the name keeps their place when the answer had an id
        let other = CardHints.named([first, cal], answered: first, name: "Morgan Hale", entityId: "e-other")
        XCTAssertEqual(other[1].hint.cardGuess(asking: nil)?.name, "Morgan Hale")
        // a card without its conversation changes nothing
        let loose = Card(id: 5, conversation: nil, hint: .none)
        XCTAssertEqual(CardHints.named([loose, second], answered: loose, name: "Alex Rivera", entityId: "e-lex")[1], second)
    }

    func testCardsDecodeTheirConversation() throws {
        let c = try JSONDecoder().decode(CardConversation.self,
                                         from: Data(#"{"device": "0F1E2D3C4B5A6978", "start_ms": 1000, "tag": "Speaker B"}"#.utf8))
        XCTAssertEqual(c, CardConversation(device: "0F1E2D3C4B5A6978", startMs: 1000, tag: "Speaker B"))
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

    func testVoiceScoringCardsDecodeTheirHintsAndConversation() throws {
        let json = """
        {"speakers": [{"id": 7, "current_label": null, "confidence": 0.2,
          "conversation": {"device": "0F1E2D3C4B5A6978", "start_ms": 1000, "tag": "Speaker B"},
          "likely": null, "candidates": [],
          "hints": {"likely": null, "candidates": [], "maybe": [
            {"name": "Alex Rivera", "label": "Alex Rivera", "entity_id": "e-lex", "reasons": ["texted on my way"]}]}}]}
        """
        struct Box: Decodable { let speakers: [VoiceCard] }
        let c = try JSONDecoder().decode(Box.self, from: Data(json.utf8)).speakers[0]
        XCTAssertEqual(c.conversation, CardConversation(device: "0F1E2D3C4B5A6978", startMs: 1000, tag: "Speaker B"))
        XCTAssertEqual(c.oneTap?.title, "Alex Rivera?")
        XCTAssertEqual(c.oneTap?.reason, "texted on my way")
        // an older server without hints: the calendar fields alone, and no guess without them
        let old = try JSONDecoder().decode(VoiceCard.self, from: Data(#"{"id": 3}"#.utf8))
        XCTAssertNil(old.oneTap)
        XCTAssertEqual(VoiceCard(id: 1, likely: LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep"))
            .oneTap?.name, "Morgan Hale")
    }

    func testTheVoiceScoringGuessSendsAHandLabelAdvancesAndLeavesTheOtherCardsOfItsConversation() {
        let alex = MaybePerson(name: "Alex Rivera", entityId: "e-lex", reasons: ["texted on my way"])
        let morgan = MaybePerson(name: "Morgan Hale", entityId: "e-morgan", reasons: ["lives with you"])
        let here = CardConversation(device: "0F1E2D3C4B5A6978", startMs: 1000)
        let hint = CalendarHint(maybe: [alex, morgan])
        var deck = VoiceDeck([VoiceCard(id: 7, confidence: 0.1, hints: hint, conversation: here),
                              VoiceCard(id: 8, confidence: 0.2, hints: hint, conversation: here),
                              VoiceCard(id: 9, confidence: 0.3, hints: hint,
                                        conversation: CardConversation(device: "0F1E2D3C4B5A6978", startMs: 9000))])
        let step = deck.confirmGuess()
        XCTAssertEqual(step, .submit(.suggested("Alex Rivera", entityId: "e-lex")))
        XCTAssertEqual(deck.phase, .sending, "no sheet: the answer goes straight out")
        guard case .submit(let a) = step else { return XCTFail() }
        let req = a.request(cardID: 7)
        XCTAssertEqual(req.path, "speakers/7/label")
        XCTAssertEqual(req.body, ["name": "Alex Rivera", "source": "user", "entity_id": "e-lex"])
        XCTAssertEqual(deck.confirmGuess(), .none, "one answer at a time")
        deck.finished(success: true)
        XCTAssertEqual(deck.current?.id, 8, "the guess advances")
        XCTAssertEqual(deck.current?.oneTap?.name, "Morgan Hale", "the same conversation stops offering Alex")
        XCTAssertEqual(deck.cards.last?.oneTap?.name, "Alex Rivera", "another conversation keeps its guess")
        // a failed send keeps the card and its guess
        _ = deck.confirmGuess()
        deck.finished(success: false)
        XCTAssertEqual(deck.current?.id, 8)
        XCTAssertEqual(deck.current?.oneTap?.name, "Morgan Hale")
        // a name given another way (swipe right on "Is this Morgan Hale?") prunes by name too
        var byName = VoiceDeck([VoiceCard(id: 1, currentLabel: "Morgan Hale", confidence: 0.1, conversation: here),
                                VoiceCard(id: 2, confidence: 0.2, hints: CalendarHint(maybe: [morgan]), conversation: here)])
        XCTAssertEqual(byName.swipe(.right), .submit(.confirm("Morgan Hale")))
        byName.finished(success: true)
        XCTAssertNil(byName.current?.oneTap)
        // skipping names nobody: the other cards keep their guess
        var skipped = VoiceDeck([VoiceCard(id: 1, confidence: 0.1, hints: hint, conversation: here),
                                 VoiceCard(id: 2, confidence: 0.2, hints: hint, conversation: here)])
        _ = skipped.swipe(.up)
        skipped.finished(success: true)
        XCTAssertEqual(skipped.current?.oneTap?.name, "Alex Rivera")
    }

    func testAVoiceScoringCardWithoutAGuessIsUnchanged() {
        var deck = VoiceDeck([VoiceCard(id: 1, currentLabel: "Noah Park", confidence: 0.3)])
        XCTAssertNil(deck.current?.oneTap)
        XCTAssertEqual(deck.confirmGuess(), .none)
        XCTAssertEqual(deck.phase, .card)
        XCTAssertEqual(deck.swipe(.right), .submit(.confirm("Noah Park")), "Yes still confirms the name it asks about")
    }

    func testSomeoneElseOffersAtMostThreeLikelyPeopleNotOnTheCard() {
        let riley = PersonSuggestion(name: "Riley Park", entityId: "e-riley", detail: "from Prep")
        let dana = PersonSuggestion(name: "Dana Brooks", entityId: "e-dana", detail: "from Prep")
        let alex = MaybePerson(name: "Alex Rivera", entityId: "e-lex", reasons: ["texted on my way"])
        let morgan = MaybePerson(name: "Morgan Hale", entityId: "e-morgan", reasons: ["lives with you"])
        let full = CalendarHint(candidates: [riley, dana], maybe: [alex, morgan])
        // calendar candidates first, then the maybes; three at most
        XCTAssertEqual(full.someoneElse(asking: nil, onCard: []).map(\.name), ["Riley Park", "Dana Brooks", "Alex Rivera"])
        // a name already on the card's buttons, or the name the card asks about, is not repeated
        XCTAssertEqual(full.someoneElse(asking: nil, onCard: ["riley park"]).map(\.name),
                       ["Dana Brooks", "Alex Rivera", "Morgan Hale"])
        XCTAssertEqual(full.someoneElse(asking: "Dana Brooks", onCard: []).map(\.name),
                       ["Riley Park", "Alex Rivera", "Morgan Hale"])
        // the card's guess button is not repeated either
        let maybes = CalendarHint(maybe: [alex, morgan])
        XCTAssertEqual(maybes.cardGuess(asking: nil)?.name, "Alex Rivera")
        XCTAssertEqual(maybes.someoneElse(asking: nil, onCard: []).map(\.name), ["Morgan Hale"])
        let likely = CalendarHint(likely: LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Prep"),
                                  maybe: [MaybePerson(name: "Morgan Hale", entityId: "e-morgan"), alex])
        XCTAssertEqual(likely.someoneElse(asking: nil, onCard: []).map(\.name), ["Alex Rivera"])
        // a person named for another voice of the conversation is gone
        XCTAssertEqual(full.dropping(name: "Alex Rivera", entityId: "e-lex").someoneElse(asking: nil, onCard: []).map(\.name),
                       ["Riley Park", "Dana Brooks", "Morgan Hale"])
        // nothing to suggest: no buttons
        XCTAssertEqual(CalendarHint.none.someoneElse(asking: nil, onCard: ["Noah Park"]), [])
    }

    func testSomeoneElsesButtonSavesAHandLabel() {
        let riley = PersonSuggestion(name: "Riley Park", label: "Riley", entityId: "e-riley")
        let r = VoiceAnswer.picked(riley).request(cardID: 12)
        XCTAssertEqual(r.path, "speakers/12/label")
        XCTAssertEqual(r.body, ["name": "Riley", "source": "user", "entity_id": "e-riley"])
        // a known voice with no LifeOS link: the plain hand label
        XCTAssertEqual(VoiceAnswer.picked(PersonSuggestion(name: "Noah Park", known: true)).request(cardID: 12).body,
                       ["name": "Noah Park", "source": "user"])
    }
}
