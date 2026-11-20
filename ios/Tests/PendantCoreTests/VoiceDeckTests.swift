import XCTest
@testable import PendantCore

final class VoiceDeckTests: XCTestCase {
    private func card(_ id: Int, _ conf: Double, label: String? = nil, noise: Bool = false) -> VoiceCard {
        VoiceCard(id: id, currentLabel: label, confidence: conf, noiseLike: noise)
    }

    func testDecodesTheFeedCandidate() throws {
        let json = """
        {"speakers": [{"id": 7, "clip_id": 7, "when": 1000, "excerpt": "hi there you two", "current_label": "Alex",
          "confidence": 0.31, "similarity": 0.8, "margin": 0.15, "new_voice": false, "noise_like": false,
          "suggested": null, "device": "old", "duration_s": 3.5, "tag": "Alex", "labeled_by": "match"}]}
        """.data(using: .utf8)!
        struct Box: Decodable { let speakers: [VoiceCard] }
        let c = try JSONDecoder().decode(Box.self, from: json).speakers[0]
        XCTAssertEqual(c.id, 7)
        XCTAssertEqual(c.guess, "Alex")
        XCTAssertEqual(c.prompt, "Is this Alex?")
        XCTAssertEqual(c.confidenceHint, "Somewhat sure")
        XCTAssertEqual(VoiceCard(id: 1).prompt, "Who's this?")
        XCTAssertEqual(VoiceCard(id: 1, suggested: "Noah").prompt, "Is this Noah?")
        XCTAssertEqual(VoiceCard(id: 1, newVoice: true).confidenceHint, "Sounds like someone new")
    }

    func testOrderIsLeastConfidentFirstWithNoiseLast() {
        let d = VoiceDeck([card(1, 0.9), card(2, 0.0, noise: true), card(3, 0.2), card(4, 0.2)])
        XCTAssertEqual(d.cards.map(\.id), [3, 4, 1, 2])
    }

    func testSwipeDirections() {
        XCTAssertEqual(SwipeDirection.of(dx: 150, dy: 20), .right)
        XCTAssertEqual(SwipeDirection.of(dx: -150, dy: -40), .left)
        XCTAssertEqual(SwipeDirection.of(dx: 30, dy: -160), .up)
        XCTAssertNil(SwipeDirection.of(dx: 60, dy: -50))
        XCTAssertNil(SwipeDirection.of(dx: 0, dy: 200), "down does nothing")
    }

    func testRightConfirmsTheGuessAndAdvances() {
        var d = VoiceDeck([card(1, 0.1, label: "Alex"), card(2, 0.5)])
        XCTAssertEqual(d.swipe(.right), .submit(.confirm("Alex")))
        XCTAssertEqual(d.phase, .sending)
        XCTAssertEqual(d.swipe(.left), .none, "one answer at a time")
        d.finished(success: true)
        XCTAssertEqual(d.current?.id, 2)
        XCTAssertEqual(d.done, 1)
        XCTAssertEqual(d.phase, .card)
    }

    func testRightWithoutAGuessOpensThePicker() {
        var d = VoiceDeck([card(1, 0)])
        XCTAssertEqual(d.swipe(.right), .pick)
        XCTAssertEqual(d.phase, .picking)
        XCTAssertEqual(d.choose(.newPerson("  ")), .none, "an empty name is not an answer")
        XCTAssertEqual(d.choose(.newPerson("Mary")), .submit(.newPerson("Mary")))
    }

    func testLeftPicksAndCancelReturnsToTheCard() {
        var d = VoiceDeck([card(1, 0.4, label: "Alex")])
        XCTAssertEqual(d.swipe(.left), .pick)
        d.cancelPick()
        XCTAssertEqual(d.phase, .card)
        XCTAssertEqual(d.current?.id, 1)
        _ = d.swipe(.left)
        XCTAssertEqual(d.choose(.removeName), .submit(.removeName))
        d.finished(success: true)
        XCTAssertTrue(d.isEmpty)
    }

    func testUpSkipsAndFailureKeepsTheCard() {
        var d = VoiceDeck([card(1, 0), card(2, 0.3)])
        XCTAssertEqual(d.swipe(.up), .submit(.skip))
        d.finished(success: false)
        XCTAssertTrue(d.failed)
        XCTAssertEqual(d.current?.id, 1)
        XCTAssertEqual(d.swipe(.up), .submit(.skip))
        XCTAssertFalse(d.failed)
        d.finished(success: true)
        XCTAssertEqual(d.current?.id, 2)
    }

    /// Stranger and Two people are sent from the card in one tap; never while the picker is open or an answer is in flight.
    func testCardButtonsAnswerAtOnce() {
        var d = VoiceDeck([card(1, 0), card(2, 0.3)])
        XCTAssertEqual(d.answer(.twoPeople), .submit(.twoPeople))
        XCTAssertEqual(d.answer(.removeName), .none, "one answer at a time")
        d.finished(success: true)
        XCTAssertEqual(d.current?.id, 2)
        _ = d.swipe(.left)
        XCTAssertEqual(d.answer(.removeName), .none, "the picker answers through choose")
        d.cancelPick()
        XCTAssertEqual(d.answer(.removeName), .submit(.removeName))
        d.finished(success: true)
        XCTAssertTrue(d.isEmpty)
        XCTAssertEqual(d.answer(.twoPeople), .none)
    }

    func testRefreshNeverBringsBackHandledCardsAndKeepsTheOneInFlight() {
        var d = VoiceDeck([card(1, 0), card(2, 0.3)])
        _ = d.swipe(.up)
        d.finished(success: true)
        _ = d.swipe(.left)                                  // card 2 is in the picker
        d.load([card(1, 0), card(5, 0.0), card(2, 0.3)])
        XCTAssertEqual(d.cards.map(\.id), [2, 5])
        _ = d.choose(.notSpeech)
        d.finished(success: true)
        d.load([card(2, 0.3), card(5, 0.0)])
        XCTAssertEqual(d.cards.map(\.id), [5])
    }

    func testRequests() {
        XCTAssertEqual(VoiceAnswer.confirm("Alex").request(cardID: 3).path, "speakers/3/label")
        XCTAssertEqual(VoiceAnswer.newPerson(" Mary ").request(cardID: 3).body, ["name": "Mary", "source": "user"])
        XCTAssertEqual(VoiceAnswer.removeName.request(cardID: 3).path, "speakers/3/stranger")
        XCTAssertEqual(VoiceAnswer.notSpeech.request(cardID: 3).path, "speakers/3/noise")
        XCTAssertEqual(VoiceAnswer.skip.request(cardID: 3).path, "speakers/3/skip")
        XCTAssertEqual(VoiceAnswer.skip.request(cardID: 3).body, [:])
        XCTAssertEqual(VoiceAnswer.twoPeople.request(cardID: 3).path, "speakers/3/mixed")
        XCTAssertEqual(VoiceAnswer.twoPeople.request(cardID: 3).body, [:])
        XCTAssertNil(VoiceAnswer.twoPeople.named)
    }

    func testPickerOrderIsMostRecognizedFirst() {
        let people = [PickerOrder.Person(name: "Noah", recognized: 40, count: 12),
                      PickerOrder.Person(name: "Alex", recognized: 55, count: 3),
                      PickerOrder.Person(name: "Ana", recognized: 55, count: 3),
                      PickerOrder.Person(name: "Sam", recognized: 2, count: 9)]
        XCTAssertEqual(PickerOrder.names(enrolled: people, allowed: ["Noah", "zed", "Bo", "alex"]),
                       ["Alex", "Ana", "Noah", "Sam", "Bo", "zed"])
        XCTAssertEqual(PickerOrder.names(enrolled: people, allowed: [], excluding: "alex"),
                       ["Ana", "Noah", "Sam"])
    }
}

final class ReachedVoiceTests: XCTestCase {
    /// A voice his sitting answer named: Yes confirms the name (teaches the voiceprint), No is "Not <name>"
    /// at once, with no picker.
    func testReachedCardConfirmsOrSaysNotThem() throws {
        let json = #"{"id": 7, "current_label": "Alex Rivera", "confidence": 0.6, "reached": true}"#
        let card = try JSONDecoder().decode(VoiceCard.self, from: Data(json.utf8))
        XCTAssertTrue(card.isReached)
        XCTAssertEqual(card.prompt, "Is this Alex Rivera?")
        XCTAssertEqual(card.confidenceHint, "From your answer for the whole conversation")
        var d = VoiceDeck([card])
        XCTAssertEqual(d.swipe(.left), .submit(.notThem))
        d.finished(success: true)
        XCTAssertTrue(d.isEmpty)
        var e = VoiceDeck([card])
        XCTAssertEqual(e.swipe(.right), .submit(.confirm("Alex Rivera")))
        XCTAssertEqual(VoiceAnswer.notThem.request(cardID: 7).path, "speakers/7/not")
        XCTAssertNil(VoiceAnswer.notThem.named)
        // an ordinary card still opens the picker on No
        var f = VoiceDeck([VoiceCard(id: 8, currentLabel: "Alex Rivera", confidence: 0.6)])
        XCTAssertEqual(f.swipe(.left), .pick)
        XCTAssertFalse(VoiceCard(id: 9, reached: true).isReached)      // nothing to confirm without a name
    }

    func testNoReadsNotFirstNameOnAReachedCard() {
        XCTAssertEqual(VoiceScoringActions(guess: "Alex Rivera", playing: false, onNo: {}, onYes: {}, onReplay: {},
                                           onStranger: {}, onTwoPeople: {}, reached: true).notThemTitle, "Not Alex")
        XCTAssertNil(VoiceScoringActions(guess: "Alex Rivera", playing: false, onNo: {}, onYes: {}, onReplay: {},
                                         onStranger: {}, onTwoPeople: {}).notThemTitle)
    }
}

final class VoiceDeckDropTests: XCTestCase {
    func testCardsNamedElsewhereGoAndNeverComeBack() {
        var d = VoiceDeck([VoiceCard(id: 1, confidence: 0.1), VoiceCard(id: 2, confidence: 0.2),
                           VoiceCard(id: 3, confidence: 0.3)])
        XCTAssertEqual(d.swipe(.up), .submit(.skip))
        d.drop([1, 3])                         // the current card stays while it is being answered
        XCTAssertEqual(d.cards.map(\.id), [1, 2])
        d.finished(success: true)
        XCTAssertEqual(d.cards.map(\.id), [2])
        d.drop([2])
        XCTAssertTrue(d.isEmpty)
        d.load([VoiceCard(id: 2), VoiceCard(id: 3), VoiceCard(id: 4)])
        XCTAssertEqual(d.cards.map(\.id), [4])
    }
}
