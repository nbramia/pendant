import XCTest
@testable import PendantCore

final class SuggestionsTests: XCTestCase {
    func testDecodesTheSuggestionFeed() throws {
        let json = """
        {"lifeos": true, "people": [
          {"name": "Alex Rivera", "label": "Alex", "entity_id": "e1", "circle": 0, "category": "family",
           "strength": 100.0, "voiceprint": true, "known": true, "new": false},
          {"name": "Alex Moore", "label": "Alex Moore", "entity_id": "e2", "circle": 5, "category": "personal",
           "voiceprint": false, "known": false, "new": false, "detail": "Acme"},
          {"name": "Zed", "label": "Zed", "entity_id": null, "circle": null, "category": null,
           "voiceprint": false, "known": true, "new": false},
          {"name": "Lex", "label": "Lex", "entity_id": null, "voiceprint": false, "known": false, "new": true}]}
        """
        let r = try JSONDecoder().decode(SuggestionResponse.self, from: Data(json.utf8))
        XCTAssertTrue(r.lifeos)
        XCTAssertEqual(r.people.map(\.name), ["Alex Rivera", "Alex Moore", "Zed", "Lex"])
        XCTAssertEqual(r.people.map(\.label), ["Alex", "Alex Moore", "Zed", "Lex"])
        XCTAssertEqual(r.people[1].entityId, "e2")
        XCTAssertNil(r.people[2].entityId)
        XCTAssertEqual(r.people[0].circleBadge, "Circle 0")
        XCTAssertNil(r.people[2].circleBadge)
        XCTAssertEqual(r.people[0].subtitle, "voice saved · family")
        XCTAssertEqual(r.people[1].subtitle, "Acme · personal")
        XCTAssertEqual(r.people[2].subtitle, "no voice yet")
        XCTAssertEqual(r.people.map(\.isNew), [false, false, false, true])
        XCTAssertEqual(Set(r.people.map(\.id)).count, 4)
    }

    func testAnOldServerRowStillDecodes() throws {
        let p = try JSONDecoder().decode(PersonSuggestion.self, from: Data(#"{"name": "Alex", "voiceprint": false, "known": true}"#.utf8))
        XCTAssertEqual(p.label, "Alex")
        XCTAssertFalse(p.isNew)
    }

    func testPathEncodesTheQuery() {
        XCTAssertEqual(Suggestions.path(""), "people/suggest?q=")
        XCTAssertEqual(Suggestions.path(" Lex "), "people/suggest?q=Lex")
        XCTAssertEqual(Suggestions.path("O'Mara & Jo"), "people/suggest?q=O%27Mara%20%26%20Jo")
    }

    func testOfflineFallbackListsKnownNamesThenTheTypedName() {
        let r = Suggestions.offline(["Noah", "Alex", "Riley"], matching: "ri")
        XCTAssertEqual(r.map(\.name), ["Riley", "ri"])
        XCTAssertEqual(r.map(\.isNew), [false, true])
        XCTAssertTrue(r.allSatisfy { $0.entityId == nil })
        XCTAssertEqual(Suggestions.offline(["A", "B"], matching: "").count, 2)
        XCTAssertEqual(Suggestions.offline(["Alex"], matching: "alex").map(\.isNew), [false])
        XCTAssertEqual(Suggestions.offline([], matching: "1234").count, 0)
    }

    func testRelinkPathAndLinkLine() {
        XCTAssertEqual(Suggestions.linkPath("Alex"), "people/Alex/link")
        XCTAssertEqual(Suggestions.linkPath("Alex Rivera"), "people/Alex%20Rivera/link")
        XCTAssertEqual(Suggestions.linkLine(entityId: "e1", name: "Alex Rivera", circle: 0), "LifeOS: Alex Rivera · circle 0")
        XCTAssertEqual(Suggestions.linkLine(entityId: "e1", name: nil, circle: nil), "LifeOS: linked")
        XCTAssertEqual(Suggestions.linkLine(entityId: nil, name: nil, circle: nil), "Not linked")
        let r = try? JSONDecoder().decode(PersonLinkResult.self, from: Data(#"{"name":"Alex","entity_id":"e1","lifeos_name":"Alex Rivera","lifeos_circle":0,"lifeos_category":"family"}"#.utf8))
        XCTAssertEqual(r?.lifeosName, "Alex Rivera")
    }

    func testPickedPersonLinksThroughEveryAnswer() {
        let r = Reassign.request(deviceId: "F00D", startMs: 1000, scope: .conversation, tag: "Alex", paragraph: nil,
                                 name: "Alex", entityId: "e1")
        XCTAssertEqual(r.body["entity_id"] as? String, "e1")
        XCTAssertEqual(r.body["source"] as? String, "user")
        XCTAssertNil(Reassign.request(deviceId: "F00D", startMs: 1000, scope: .line, tag: "T", paragraph: 1,
                                      name: "Alex").body["entity_id"])

        let a = VoiceAnswer.suggested(" Alex ", entityId: "e1").request(cardID: 3)
        XCTAssertEqual(a.path, "speakers/3/label")
        XCTAssertEqual(a.body, ["name": "Alex", "source": "user", "entity_id": "e1"])

        var f = VoiceSampleFlow()
        f.choose(name: "Alex", entityId: "e1")
        f.start(now: Date(timeIntervalSince1970: 1_794_000_000), device: "E1")
        let req = f.done(now: Date(timeIntervalSince1970: 1_794_000_060))
        XCTAssertEqual(req?.body["entity_id"] as? String, "e1")
        XCTAssertEqual(req?.body["source"] as? String, "user")
        var typed = VoiceSampleFlow()
        typed.choose(name: "Alex")
        typed.start(now: Date(timeIntervalSince1970: 1_794_000_000), device: "E1")
        XCTAssertNil(typed.done(now: Date(timeIntervalSince1970: 1_794_000_060))?.body["entity_id"])
    }

    func testDeckAcceptsASuggestedPerson() {
        var d = VoiceDeck([VoiceCard(id: 1, excerpt: "hi", confidence: 0.1)])
        XCTAssertEqual(d.swipe(.left), .pick)
        XCTAssertEqual(d.choose(.suggested("Alex", entityId: "e1")), .submit(.suggested("Alex", entityId: "e1")))
    }
}

final class SubmitPicksTests: XCTestCase {
    private let rows = [PersonSuggestion(name: "Alex Rivera", entityId: "e1", known: true),
                        PersonSuggestion(name: "Lex", isNew: true)]

    func testSubmitPicksOnlyAnExactFirstRow() {
        XCTAssertEqual(Suggestions.exactFirst(rows, query: "alex rivera")?.entityId, "e1")
        XCTAssertEqual(Suggestions.exactFirst(rows, query: " Alex Rivera ")?.entityId, "e1")
        XCTAssertNil(Suggestions.exactFirst(rows, query: "lex"), "a partial match is not chosen")
        XCTAssertNil(Suggestions.exactFirst(rows, query: ""))
        XCTAssertNil(Suggestions.exactFirst([PersonSuggestion(name: "Lex", isNew: true)], query: "Lex"),
                     "the new-name row is never chosen by submitting")
        XCTAssertNil(Suggestions.exactFirst([], query: "x"))
    }

    func testTheRelinkPickerOpensOnTheSpeakersNameAndRelinksInOneTap() {
        // nothing typed: the LifeOS matches for the speaker's name
        XCTAssertEqual(Suggestions.path(Suggestions.relinkQuery("", name: "Alex Rivera")), "people/suggest?q=Alex%20Rivera")
        XCTAssertEqual(Suggestions.relinkQuery("  ", name: "Alex"), "Alex")
        // a search replaces it
        XCTAssertEqual(Suggestions.relinkQuery("Riley", name: "Alex"), "Riley")
        // the one tap on a match: the relink action for that name
        XCTAssertEqual(Suggestions.linkPath("Alex Rivera"), "people/Alex%20Rivera/link")
    }

    func testAnAnswerAfterAFailedLoadListsItsMatches() {
        // the relink picker: LifeOS people only, opened on the speaker's name
        var state = SuggestionState()
        state.loadFailed()
        XCTAssertTrue(state.failed)
        XCTAssertEqual(state.shown(query: "Alex", fallback: ["Alex"], linkOnly: true), [],
                       "after a failure only known names are listed, and none of them is a LifeOS person")
        let alex = PersonSuggestion(name: "Alex Rivera", entityId: "e-lex", circle: 1)
        let other = PersonSuggestion(name: "Alex Moreno", entityId: "e-alexm", circle: 3)
        state.received(SuggestionResponse(people: [alex, other, PersonSuggestion(name: "Alex", known: true)]))
        XCTAssertFalse(state.failed)
        XCTAssertEqual(state.shown(query: "Alex", fallback: ["Alex"], linkOnly: true), [alex, other],
                       "the later answer's LifeOS matches show")
        XCTAssertFalse(state.lifeosDown)
        state.received(SuggestionResponse(people: [], lifeos: false))
        XCTAssertTrue(state.lifeosDown)
    }
}
