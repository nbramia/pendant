import XCTest
@testable import PendantCore

final class ReassignTests: XCTestCase {
    func testFindsTheSpeakerLabel() {
        XCTAssertEqual(Reassign.label(in: "**Alex:** we should fix it")?.tag, "Alex")
        XCTAssertEqual(Reassign.label(in: "**Alex:** we should fix it")?.rest, "we should fix it")
        XCTAssertEqual(Reassign.label(in: "**Speaker A:** hi")?.tag, "Speaker A")
        XCTAssertNil(Reassign.label(in: "no label here"))
        XCTAssertNil(Reassign.label(in: "**:** empty"))
        XCTAssertNil(Reassign.label(in: "**bold** text: not a label"))
    }

    func testPreviewSegmentsSplitNamesFromWords() {
        XCTAssertEqual(Reassign.previewSegments("**Noah:** hello there **Speaker A:** hi, ok?"),
                       [.name("Noah"), .words("hello there"), .name("Speaker A"), .words("hi, ok?")])
        XCTAssertEqual(Reassign.previewSegments("…ready. **Alex:** yes"),
                       [.words("…ready."), .name("Alex"), .words("yes")])
        XCTAssertEqual(Reassign.previewSegments("no labels, just words"), [.words("no labels, just words")])
        XCTAssertEqual(Reassign.previewSegments("a **bold** word: not a label"), [.words("a **bold** word: not a label")])
        XCTAssertEqual(Reassign.previewSegments("**Noah:**"), [.name("Noah")])
        XCTAssertEqual(Reassign.previewSegments(""), [])
    }

    func testHighlightRangesFindTermsInNamesToo() {
        let name = "Alex Stone"
        let r = Reassign.highlightRanges(in: name, terms: ["alex", "sto", ""])
        XCTAssertEqual(r.map { String(name[$0]) }, ["Alex", "Sto"])
        XCTAssertTrue(Reassign.highlightRanges(in: "hello", terms: ["alex"]).isEmpty)
    }

    func testParagraphIndexesCountOnlyStampedLines() {
        let lines = ["Also recorded in [[Note]]", "[10:00] **A:** one", "Added to tasks: x", "[9:05] two", "[bad] x", "[10:01] three"]
        XCTAssertEqual(Reassign.paragraphIndexes(lines: lines), [nil, 0, nil, 1, nil, 2])
    }

    func testRequestForALine() {
        let r = Reassign.request(deviceId: "F00D", startMs: 1000, scope: .line, tag: "Speaker B",
                                 paragraph: 3, name: " Alex ")
        XCTAssertEqual(r.path, "conversations/F00D/1000/speaker")
        XCTAssertEqual(r.body as NSDictionary, ["scope": "line", "tag": "Speaker B", "source": "user",
                                                "name": "Alex", "paragraph": 3] as NSDictionary)
    }

    func testRequestForTheConversationLeavesOutTheParagraph() {
        let r = Reassign.request(deviceId: "F00D", startMs: 1000, scope: .conversation, tag: "Alex",
                                 paragraph: 3, name: "Sam")
        XCTAssertEqual(r.body as NSDictionary, ["scope": "conversation", "tag": "Alex", "source": "user",
                                                "name": "Sam"] as NSDictionary)
    }
}

final class ReassignAllTests: XCTestCase {
    func testTheSheetOffersThreeScopesAndStartsOnTheConversation() {
        XCTAssertEqual(ReassignScope.allCases.map(\.title), ["This line", "This conversation", "All past conversations"])
        XCTAssertEqual(ReassignScope.default, .conversation)
        XCTAssertEqual(ReassignScope.allCases.map(\.rawValue), ["line", "conversation", "all"])
    }

    func testEverythingAvailableStartsOnTheConversation() {
        let c = ScopeChoice(lineOK: true, voiceOK: true)
        XCTAssertEqual(c.selected, .conversation)
        XCTAssertTrue(c.options.allSatisfy(\.enabled))
        XCTAssertEqual(c.options.map(\.scope), [.line, .conversation, .all])
        XCTAssertEqual(c.confirmTitle, "Change whole conversation")
    }

    func testASharedTagDisablesTheVoiceScopesWithTheirReasonAndStartsOnTheLine() {
        var c = ScopeChoice(lineOK: true, voiceOK: false)
        XCTAssertEqual(c.selected, .line)
        XCTAssertTrue(c.option(.line).enabled)
        XCTAssertNil(c.option(.line).reason)
        XCTAssertEqual(c.option(.conversation).reason, ScopeChoice.sharedVoiceReason)
        XCTAssertEqual(c.option(.all).reason, ScopeChoice.sharedVoiceReason)
        XCTAssertEqual(c.confirmTitle, "Change this line")
        // tapping a disabled option selects nothing, and nothing switches by itself
        XCTAssertFalse(c.select(.conversation))
        XCTAssertFalse(c.select(.all))
        XCTAssertEqual(c.selected, .line)
    }

    func testALineThatCantBeAddressedDisablesOnlyTheLine() {
        var c = ScopeChoice(lineOK: false, voiceOK: true)
        XCTAssertEqual(c.selected, .conversation)
        XCTAssertEqual(c.option(.line).reason, ScopeChoice.lineReason)
        XCTAssertFalse(c.select(.line))
        XCTAssertEqual(c.selected, .conversation)
        XCTAssertTrue(c.select(.all))
        XCTAssertEqual(c.confirmTitle, "Change all past conversations…")
    }

    func testTheChosenScopeSticks() {
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        XCTAssertTrue(c.select(.line))
        XCTAssertEqual(c.selected, .line)
        XCTAssertTrue(c.select(.line))                // picking it again keeps it
        XCTAssertEqual(c.selected, .line)
        XCTAssertTrue(c.select(.all))
        XCTAssertEqual(c.selected, .all)
        XCTAssertTrue(c.select(.conversation))
        XCTAssertEqual(c.selected, .conversation)
    }

    func testNothingAvailableSelectsNothing() {
        var c = ScopeChoice(lineOK: false, voiceOK: false)
        XCTAssertNil(c.selected)
        XCTAssertFalse(c.options.contains(where: \.enabled))
        for s in ReassignScope.allCases { XCTAssertFalse(c.select(s)) }
        XCTAssertNil(c.selected)
    }

    func testConfirmTitlesNameTheScope() {
        XCTAssertEqual(ReassignScope.allCases.map(\.confirmTitle),
                       ["Change this line", "Change whole conversation", "Change all past conversations…"])
    }

    func testRequestForAllLeavesOutTheParagraph() {
        let r = Reassign.request(deviceId: "F00D", startMs: 1000, scope: .all, tag: "Alex", paragraph: 3, name: "Noah")
        XCTAssertEqual(r.body as NSDictionary, ["scope": "all", "tag": "Alex", "source": "user",
                                                "name": "Noah"] as NSDictionary)
    }

    func testTheRequestUrlKeepsTheQueryOutOfThePath() throws {
        let base = URL(string: "https://noah-linux.example.ts.net/pendant/feed")!
        let path = Reassign.previewPath(deviceId: "F00D", startMs: 1000, tag: "Speaker B", name: " Ann & Bo+ ")
        let url = try XCTUnwrap(FeedURL.make(base: base, path: path))
        let c = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(c.path, "/pendant/feed/conversations/F00D/1000/speaker/preview")
        XCTAssertFalse(url.absoluteString.contains("%3F"))
        XCTAssertEqual(c.queryItems?.first { $0.name == "tag" }?.value, "Speaker B")
        XCTAssertEqual(c.queryItems?.first { $0.name == "name" }?.value, "Ann & Bo+")
        XCTAssertEqual(c.queryItems?.first { $0.name == "source" }?.value, "user")
        XCTAssertEqual(c.queryItems?.count, 3)
        // what URLSession sends: the path and the query as separate parts
        XCTAssertEqual(url.query, "tag=Speaker%20B&name=Ann%20%26%20Bo%2B&source=user")
    }

    func testAnEscapedFullNameIsNotEscapedTwice() throws {
        let base = URL(string: "https://h/pendant/feed")!
        let person = try XCTUnwrap(FeedURL.make(base: base, path: "people/Alex%20Rivera?days=7"))
        XCTAssertEqual(person.absoluteString, "https://h/pendant/feed/people/Alex%20Rivera?days=7")
        let link = try XCTUnwrap(FeedURL.make(base: base, path: Suggestions.linkPath("Alex Rivera")))
        XCTAssertEqual(link.path, "/pendant/feed/people/Alex Rivera/link")
        XCTAssertEqual(try XCTUnwrap(FeedURL.make(base: base, path: "people/Alex Rivera")).absoluteString,
                       "https://h/pendant/feed/people/Alex%20Rivera")
    }

    func testPlainPathsAndBasesWithATrailingSlash() throws {
        let u = try XCTUnwrap(FeedURL.make(base: URL(string: "https://h/pendant/feed/")!, path: "day/2026-11-08"))
        XCTAssertEqual(u.absoluteString, "https://h/pendant/feed/day/2026-11-08")
        XCTAssertNil(u.query)
    }

    func testAllRequestCarriesThePreviewIdsAndDigest() throws {
        let p = try JSONDecoder().decode(ReassignPreview.self, from: Data(json.utf8))
        let r = Reassign.request(deviceId: "F00D", startMs: 1000, scope: .all, tag: "Alex", paragraph: nil,
                                 name: "Noah", preview: p)
        XCTAssertEqual(r.body as NSDictionary, ["scope": "all", "tag": "Alex", "source": "user", "name": "Noah",
                                                "ids": [4, 9], "digest": "0123456789abcdef"] as NSDictionary)
        let other = Reassign.request(deviceId: "F00D", startMs: 1000, scope: .conversation, tag: "Alex",
                                     paragraph: nil, name: "Noah", preview: p)
        XCTAssertNil(other.body["ids"])
    }

    func testPreviewPathEscapesTheQuery() {
        XCTAssertEqual(Reassign.previewPath(deviceId: "F00D", startMs: 1000, tag: "Speaker B", name: " Ann & Bo+ "),
                       "conversations/F00D/1000/speaker/preview?tag=Speaker%20B&name=Ann%20%26%20Bo%2B&source=user")
    }

    private let json = """
    {"conversations": 3, "days": 2, "words": 41, "skipped": 1, "name": "Noah", "ids": [4, 9],
     "digest": "0123456789abcdef", "sample": [
      {"date": "2026-11-06", "time": "9:00", "title": "Old", "device": "F00D", "device_id": "F00D", "start_ms": 5,
       "label": "Speaker A", "similarity": 0.71, "excerpt": "hi", "words": 4, "paragraphs": [0]},
      {"date": "2026-11-07", "time": "10:00", "title": "Lunch", "device": "F00D", "device_id": "F00D", "start_ms": 7,
       "label": "Speaker A", "similarity": 0.62, "excerpt": "one two", "words": 20, "paragraphs": [0, 2]},
      {"date": "2026-11-07", "time": "11:00", "title": "Walk", "device": "F00D", "device_id": "F00D", "start_ms": 9,
       "label": "Alex", "similarity": 0.93, "excerpt": "salt", "words": 17, "paragraphs": [1]}]}
    """

    func testPreviewDecodesAndSummarizes() throws {
        let p = try JSONDecoder().decode(ReassignPreview.self, from: Data(json.utf8))
        XCTAssertEqual(p.summary, "41 words in 3 conversations over 2 days")
        XCTAssertEqual(p.confirmTitle, "Relabel 41 words over 2 days")
        XCTAssertEqual(p.skipped, 1)
        XCTAssertFalse(p.isEmpty)
        XCTAssertEqual(p.sample[1].paragraphs, [0, 2])
        XCTAssertEqual(p.sample[1].id, "F00D|7")
    }

    func testSingularCountsAndEmpty() {
        let one = ReassignPreview(conversations: 1, days: 1, words: 1, sample: [])
        XCTAssertEqual(one.confirmTitle, "Relabel 1 word over 1 day")
        XCTAssertEqual(one.summary, "1 word in 1 conversation over 1 day")
        XCTAssertTrue(ReassignPreview(conversations: 0, days: 0, words: 0, sample: []).isEmpty)
    }

    func testSampleGroupsByDayNewestFirstBestMatchFirst() throws {
        let p = try JSONDecoder().decode(ReassignPreview.self, from: Data(json.utf8))
        let g = p.sampleByDay
        XCTAssertEqual(g.map(\.date), ["2026-11-07", "2026-11-06"])
        XCTAssertEqual(g[0].items.map(\.title), ["Walk", "Lunch"])
    }

    func testAVoiceRenameCoversTheConversationWhenAllIsChosen() {
        var p = PendingLabels()
        p.record(scope: .all, tag: "Alex", paragraph: 2, name: "Noah")
        XCTAssertEqual(p.label(index: 0, tag: "Alex"), "Noah")
        XCTAssertEqual(p.label(index: 2, tag: "Alex"), "Noah")
    }
}

final class PendingLabelsTests: XCTestCase {
    func testALineShowsTheNewNameUntilTheServerAgrees() {
        var p = PendingLabels()
        p.record(scope: .line, tag: "Speaker B", paragraph: 1, name: " Alex ")
        XCTAssertEqual(p.label(index: 1, tag: "Speaker B"), "Alex")
        XCTAssertEqual(p.label(index: 0, tag: "Speaker B"), "Speaker B")
        p.reconcile(serverTags: [0: "Speaker B", 1: "Speaker B"])       // reload before the processor applied it
        XCTAssertEqual(p.label(index: 1, tag: "Speaker B"), "Alex")
        p.reconcile(serverTags: [0: "Speaker B", 1: "alex"])          // server copy now agrees (case aside)
        XCTAssertTrue(p.isEmpty)
        XCTAssertEqual(p.label(index: 1, tag: "Alex"), "Alex")
    }

    func testAVoiceRenameCoversItsLinesButNotOverriddenOnes() {
        var p = PendingLabels()
        p.record(scope: .conversation, tag: "Alex", paragraph: nil, name: "Noah")
        XCTAssertEqual(p.label(index: 0, tag: "Alex"), "Noah")
        XCTAssertEqual(p.label(index: 1, tag: "Alex", overrides: [1]), "Alex")
        XCTAssertEqual(p.label(index: 2, tag: "Someone"), "Someone")
        p.reconcile(serverTags: [0: "Alex", 1: "Alex"], overrides: [1])
        XCTAssertFalse(p.isEmpty)                                       // line 0 still shows the old name
        p.reconcile(serverTags: [0: "Noah", 1: "Alex"], overrides: [1])
        XCTAssertTrue(p.isEmpty)
    }

    func testSomeoneKeepsItsSpelling() {
        var p = PendingLabels()
        p.record(scope: .line, tag: "A", paragraph: 0, name: "someone")
        XCTAssertEqual(p.label(index: 0, tag: "A"), "Someone")
    }
}

final class ReassignUndoTests: XCTestCase {
    func testTheReceiptDecodesAndOlderServersHaveNoUndo() throws {
        let now = Date(timeIntervalSince1970: 1_000)
        let r = try JSONDecoder().decode(ReassignReceipt.self, from: Data(#"{"ok":true,"change_id":"c-7","undo_until":87400.5}"#.utf8))
        XCTAssertEqual(r, ReassignReceipt(changeId: "c-7", undoUntil: 87400.5))
        XCTAssertEqual(r.undoId(now: now), "c-7")
        let old = try JSONDecoder().decode(ReassignReceipt.self, from: Data(#"{"ok":true}"#.utf8))
        XCTAssertNil(old.undoId(now: now))
        XCTAssertNil(ReassignReceipt(changeId: "", undoUntil: nil).undoId(now: now))
        XCTAssertNil(ReassignReceipt(changeId: "c-7", undoUntil: 999).undoId(now: now))   // window closed
        XCTAssertEqual(ReassignReceipt.tourSample(now: now).undoId(now: now), ReassignReceipt.tourSampleId)
    }

    func testUndoPath() {
        XCTAssertEqual(Reassign.undoPath(changeId: "abc-123"), "reassign/abc-123/undo")
        XCTAssertEqual(Reassign.undoPath(changeId: "a/b c"), "reassign/a%2Fb%20c/undo")
    }

    func testFailuresSayWhyAndOnlyTransientOnesRetry() {
        XCTAssertFalse(UndoFailure(status: 409).canRetry)
        XCTAssertEqual(UndoFailure(status: 409).message, "Edited since, so it can't be undone.")
        XCTAssertFalse(UndoFailure(status: 410).canRetry)
        XCTAssertFalse(UndoFailure(status: 404).canRetry)
        XCTAssertEqual(UndoFailure(status: 503).message, "Busy — retry")
        XCTAssertTrue(UndoFailure(status: 503).canRetry)
        XCTAssertTrue(UndoFailure(status: nil).canRetry)
    }

    private func offer(_ name: String, row: Int? = 0, at now: Date) -> UndoOffer {
        var p = PendingLabels()
        return UndoOffer(name: name, changeId: "c-\(name)", scope: .conversation, row: row,
                         change: p.record(scope: .conversation, tag: "Speaker A", paragraph: row, name: name), now: now)
    }

    func testTheRowDismissesItselfOnlyWhileIdle() {
        var o = offer(" Alex ", at: Date())
        XCTAssertEqual(o.title, "Changed to Alex")
        XCTAssertTrue(o.dismissesItself)
        XCTAssertTrue(o.begin())
        XCTAssertFalse(o.dismissesItself)
        XCTAssertFalse(o.begin())                     // a second tap while undoing does nothing
        o.fail(status: 503)
        XCTAssertFalse(o.dismissesItself)
        XCTAssertTrue(o.begin())                      // retry
        o.fail(status: 409)
        XCTAssertFalse(o.begin())                     // final: no retry
        XCTAssertFalse(o.dismissesItself)
        XCTAssertEqual(offer("someone", at: Date()).title, "Changed to Someone")
    }

    func testEveryRecentChangeKeepsItsOwnRow() {
        let t0 = Date(timeIntervalSince1970: 1_000)
        var list = UndoOffers()
        list.add(offer("Alex", row: 0, at: t0))
        list.add(offer("Sam", row: 1, at: t0.addingTimeInterval(4)))
        XCTAssertEqual(list.all.map(\.name), ["Alex", "Sam"])
        XCTAssertEqual(list.at(0).map(\.name), ["Alex"])
        XCTAssertEqual(list.at(1).map(\.name), ["Sam"])
        XCTAssertEqual(list.nextExpiry, t0.addingTimeInterval(UndoOffer.shownFor))
    }

    func testEachRowExpiresOnItsOwnTimer() {
        let t0 = Date(timeIntervalSince1970: 1_000)
        var list = UndoOffers()
        list.add(offer("Alex", at: t0))
        list.add(offer("Sam", at: t0.addingTimeInterval(4)))
        list.expire(now: t0.addingTimeInterval(9.9))
        XCTAssertEqual(list.all.count, 2)
        list.expire(now: t0.addingTimeInterval(10))
        XCTAssertEqual(list.all.map(\.name), ["Sam"])
        XCTAssertEqual(list.nextExpiry, t0.addingTimeInterval(14))
        list.expire(now: t0.addingTimeInterval(14))
        XCTAssertTrue(list.isEmpty)
        XCTAssertNil(list.nextExpiry)
    }

    func testAnUndoingOrFailedRowStaysPastItsTime() {
        let t0 = Date(timeIntervalSince1970: 1_000)
        var list = UndoOffers()
        let a = offer("Alex", at: t0), b = offer("Sam", at: t0)
        list.add(a)
        list.add(b)
        XCTAssertNotNil(list.begin(a.id))
        XCTAssertNil(list.begin(a.id))                // already undoing
        XCTAssertNotNil(list.begin(b.id))
        list.fail(b.id, status: 409)
        XCTAssertNil(list.nextExpiry)                 // neither goes away by itself now
        list.expire(now: t0.addingTimeInterval(60))
        XCTAssertEqual(list.all.count, 2)
        XCTAssertEqual(list.all[1].phase, .failed(UndoFailure(status: 409)))
        XCTAssertNil(list.begin(b.id))                // a final failure can't be retried
    }

    func testUndoingOneRowLeavesTheOthersAlone() {
        let t0 = Date(timeIntervalSince1970: 1_000)
        var list = UndoOffers()
        let a = offer("Alex", at: t0), b = offer("Sam", at: t0)
        list.add(a)
        list.add(b)
        XCTAssertNotNil(list.begin(a.id))
        list.remove(a.id)
        XCTAssertEqual(list.all.map(\.name), ["Sam"])
        XCTAssertEqual(list.all[0].phase, .offered)
        XCTAssertNotNil(list.begin(b.id))
    }

    func testUndoTakesBackOnlyItsOwnLabels() {
        var p = PendingLabels()
        let voice = p.record(scope: .conversation, tag: "Speaker A", paragraph: 0, name: "Alex")
        let line = p.record(scope: .line, tag: "Speaker B", paragraph: 2, name: "Sam")
        XCTAssertEqual(p.label(index: 0, tag: "Speaker A"), "Alex")
        XCTAssertEqual(p.label(index: 2, tag: "Speaker B"), "Sam")
        p.revert(voice)                                // the older change: the newer line keeps its name
        XCTAssertEqual(p.label(index: 0, tag: "Speaker A"), "Speaker A")
        XCTAssertEqual(p.label(index: 2, tag: "Speaker B"), "Sam")
        p.revert(line)
        XCTAssertEqual(p.label(index: 2, tag: "Speaker B"), "Speaker B")
        XCTAssertTrue(p.isEmpty)
    }

    func testRevertingALineGoesBackToItsEarlierNameUnlessChangedSince() {
        var p = PendingLabels()
        let first = p.record(scope: .line, tag: "A", paragraph: 1, name: "Sam")
        let second = p.record(scope: .line, tag: "A", paragraph: 1, name: "Alex")
        p.revert(first)                                // superseded: the newer name stays
        XCTAssertEqual(p.label(index: 1, tag: "A"), "Alex")
        p.revert(second)
        XCTAssertEqual(p.label(index: 1, tag: "A"), "Sam")
    }

    func testTheLikelyConfirmNamesTheSelectedScope() {
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        XCTAssertEqual(c.likelyTitle("Morgan Hale"), "Yes, Morgan Hale · this conversation")
        c.select(.line)
        XCTAssertEqual(c.likelyTitle("Morgan Hale"), "Yes, Morgan Hale · this line")
        c.select(.all)
        XCTAssertEqual(c.likelyTitle("Morgan Hale"), "Yes, Morgan Hale · all past conversations…")
        XCTAssertEqual(ScopeChoice(lineOK: true, voiceOK: false).likelyTitle("Morgan"), "Yes, Morgan · this line")
        XCTAssertNil(ScopeChoice(lineOK: false, voiceOK: false).likelyTitle("Morgan"))
    }
}

final class RemoveNameTests: XCTestCase {
    func testRemoveRequestsCarryNoNameAndNeverApplyToAll() throws {
        let line = try XCTUnwrap(Reassign.removeRequest(deviceId: "F00D", startMs: 1000, scope: .line, tag: "Alex",
                                                        paragraph: 2))
        XCTAssertEqual(line.path, "conversations/F00D/1000/speaker")
        XCTAssertEqual(line.body as NSDictionary, ["scope": "line", "tag": "Alex", "remove": true,
                                                   "paragraph": 2] as NSDictionary)
        let conv = try XCTUnwrap(Reassign.removeRequest(deviceId: "F00D", startMs: 1000, scope: .conversation,
                                                        tag: "Alex", paragraph: 2))
        XCTAssertEqual(conv.body as NSDictionary, ["scope": "conversation", "tag": "Alex", "remove": true] as NSDictionary)
        XCTAssertNil(Reassign.removeRequest(deviceId: "F00D", startMs: 1000, scope: .all, tag: "Alex", paragraph: nil))
    }

    func testPickingRemoveNameNamesTheScopeAndTakesAllAway() {
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        c.setRemoving(true)
        XCTAssertTrue(c.removing)
        XCTAssertEqual(c.selected, .conversation)
        XCTAssertEqual(c.confirmTitle, "Remove name · this conversation")
        XCTAssertEqual(c.option(.all).reason, ScopeChoice.removeAllReason)
        XCTAssertFalse(c.select(.all))
        XCTAssertTrue(c.select(.line))
        XCTAssertEqual(c.confirmTitle, "Remove name · this line")
        // back to a person: all past conversations is offered again
        c.setRemoving(false)
        XCTAssertTrue(c.option(.all).enabled)
        XCTAssertEqual(c.confirmTitle, "Change this line")
    }

    func testRemovingWhileAllIsSelectedSelectsNothingRatherThanSwitching() {
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        c.select(.all)
        c.setRemoving(true)
        XCTAssertNil(c.selected)
        XCTAssertEqual(c.confirmTitle, "Choose this line or this conversation")
        XCTAssertTrue(c.select(.conversation))
        XCTAssertEqual(c.confirmTitle, "Remove name · this conversation")
    }

    func testPickingAPersonAfterRemoveNameBringsBackAllPastConversations() {
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        c.select(.all)
        c.setRemoving(true)
        XCTAssertNil(c.selected)
        c.setRemoving(false)                          // a person picked instead: his own choice comes back
        XCTAssertEqual(c.selected, .all)
        XCTAssertEqual(c.confirmTitle, "Change all past conversations…")
        // a scope he chose meanwhile wins
        c.setRemoving(true)
        c.select(.line)
        c.setRemoving(false)
        XCTAssertEqual(c.selected, .line)
        XCTAssertEqual(c.confirmTitle, "Change this line")
    }

    func testRemovingKeepsTheOtherReasons() {
        var c = ScopeChoice(lineOK: true, voiceOK: false)
        c.setRemoving(true)
        XCTAssertEqual(c.option(.conversation).reason, ScopeChoice.sharedVoiceReason)
        XCTAssertEqual(c.option(.all).reason, ScopeChoice.sharedVoiceReason)
        XCTAssertEqual(c.confirmTitle, "Remove name · this line")
        var none = ScopeChoice(lineOK: false, voiceOK: false)
        none.setRemoving(true)
        XCTAssertEqual(none.confirmTitle, "Nothing here can be changed")
    }

    func testRemoveTitles() {
        XCTAssertEqual(ReassignScope.allCases.map(\.removeTitle),
                       ["Remove name · this line", "Remove name · this conversation", nil])
        XCTAssertEqual(Reassign.removeCaption, "Back to an unnamed speaker")
    }

    func testFreshTagSkipsUsedTags() {
        XCTAssertEqual(Reassign.freshTag(avoiding: []), "Speaker A")
        XCTAssertEqual(Reassign.freshTag(avoiding: ["Alex", "speaker a", "Speaker B"]), "Speaker C")
        let all = Set((65...90).map { "Speaker \(String(UnicodeScalar($0)!))" })
        XCTAssertEqual(Reassign.freshTag(avoiding: all), "Speaker AA")
        XCTAssertEqual(Reassign.freshTag(avoiding: ["Speaker A"], keeping: "Speaker D"), "Speaker D")
        XCTAssertEqual(Reassign.freshTag(avoiding: ["Speaker D"], keeping: "Speaker D"), "Speaker A")
        XCTAssertEqual(Reassign.freshTag(avoiding: [], keeping: "Alex"), "Speaker A")
        XCTAssertTrue(Reassign.isUnnamed("Speaker AB"))
        XCTAssertFalse(Reassign.isUnnamed("Speaker 1"))
        XCTAssertFalse(Reassign.isUnnamed("Someone"))
    }

    func testTheReceiptCarriesTheNewLabelAndTheRowSaysNameRemoved() throws {
        let r = try JSONDecoder().decode(ReassignReceipt.self, from: Data(
            #"{"scope":"line","name":"Speaker C","removed":true,"change_id":"c-9","undo_until":9e9}"#.utf8))
        XCTAssertEqual(r.name, "Speaker C")
        XCTAssertNil(ReassignReceipt.tourSample().name)
        var p = PendingLabels()
        let change = p.record(scope: .line, tag: "Alex", paragraph: 1, name: "Speaker C")
        XCTAssertEqual(p.label(index: 1, tag: "Alex"), "Speaker C")
        let o = UndoOffer(name: "Speaker C", changeId: "c-9", scope: .line, row: 1, change: change, removed: true)
        XCTAssertEqual(o.title, "Name removed")
        XCTAssertTrue(o.removed)
        p.revert(o.change)
        XCTAssertEqual(p.label(index: 1, tag: "Alex"), "Alex")
        XCTAssertEqual(UndoOffer(name: "Sam", changeId: "c", scope: .line, row: 1, change: change).title, "Changed to Sam")
    }

    func testANameSavesOnTheFirstTapExceptForAllPastConversationsAndRemoveName() {
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        XCTAssertEqual(c.selected, .conversation)
        XCTAssertFalse(c.confirmsPick, "the default scope: one tap on a name saves it")
        // what that one tap sends: the conversation-wide hand label, linked to the person
        let req = Reassign.request(deviceId: "BEEF", startMs: 12, scope: c.selected!, tag: "Speaker B", paragraph: 3,
                                   name: "Alex Rivera", entityId: "e-lex")
        XCTAssertEqual(req.path, "conversations/BEEF/12/speaker")
        XCTAssertEqual(req.body["scope"] as? String, "conversation")
        XCTAssertEqual(req.body["name"] as? String, "Alex Rivera")
        XCTAssertEqual(req.body["entity_id"] as? String, "e-lex")
        XCTAssertEqual(req.body["source"] as? String, "user")
        c.select(.line)
        XCTAssertFalse(c.confirmsPick, "this line saves on the tap too")
        c.select(.all)
        XCTAssertTrue(c.confirmsPick, "all past conversations waits for the confirm (and its preview)")
        c.setRemoving(true)
        XCTAssertTrue(c.confirmsPick)
        c.setRemoving(false)
        XCTAssertEqual(c.selected, .all)
        XCTAssertTrue(c.confirmsPick, "a name after Remove name goes back to all past conversations, still confirmed")
        c.select(.conversation)
        c.setRemoving(true)
        XCTAssertTrue(c.confirmsPick, "Remove name always asks")
        c.setRemoving(false)
        XCTAssertFalse(c.confirmsPick)
        // a voice with only the line available opens on the line: one tap
        XCTAssertFalse(ScopeChoice(lineOK: true, voiceOK: false).confirmsPick)
        // nothing can be changed: no tap saves anything
        XCTAssertTrue(ScopeChoice(lineOK: false, voiceOK: false).confirmsPick)
    }
}

final class SittingScopeTests: XCTestCase {
    func testTheScopeRowSaysHowMuchThisConversationCovers() throws {
        let json = #"{"parts":4,"span_s":3940,"start_ms":1,"end_ms":2,"named":4,"named_parts":3,"before":3,"after":0}"#
        let s = try JSONDecoder().decode(SittingScope.self, from: Data(json.utf8))
        XCTAssertEqual(s.scopeTitle, "This conversation (1 hr, 4 parts)")
        XCTAssertEqual(SittingScope(parts: 1, spanS: 300).scopeTitle, "This conversation")
        XCTAssertEqual(SittingScope(parts: 2, spanS: nil).scopeTitle, "This conversation (2 parts)")
        var c = ScopeChoice(lineOK: true, voiceOK: true)
        XCTAssertEqual(c.title(.conversation), "This conversation")          // until the server says
        c.sitting = s
        XCTAssertEqual(c.options.map { c.title($0.scope) },
                       ["This line", "This conversation (1 hr, 4 parts)", "All past conversations"])
        XCTAssertEqual(c.selected, .conversation)                             // still one tap
        XCTAssertFalse(c.confirmsPick)
        c.setRemoving(true)                                                   // Remove name stays in this piece
        XCTAssertEqual(c.title(.conversation), "This conversation")
    }

    func testSpans() {
        XCTAssertEqual(SittingScope.span(20), "1 min")
        XCTAssertEqual(SittingScope.span(45 * 60), "45 min")
        XCTAssertEqual(SittingScope.span(66 * 60), "1 hr")
        XCTAssertEqual(SittingScope.span(92 * 60), "1 hr 30 min")
        XCTAssertEqual(SittingScope.span(2 * 3600 + 50 * 60), "2 hr 45 min")
    }

    func testTheConfirmationSaysWhatElseWasNamed() throws {
        XCTAssertEqual(SittingScope(parts: 4, spanS: 3900, before: 3).alsoNamed, "Also named in 3 earlier parts")
        XCTAssertEqual(SittingScope(parts: 2, spanS: 900, after: 1).alsoNamed, "Also named in 1 later part")
        XCTAssertEqual(SittingScope(parts: 4, spanS: 3900, before: 2, after: 1).alsoNamed,
                       "Also named in 2 earlier parts and 1 later part")
        XCTAssertNil(SittingScope(parts: 3, spanS: 3900).alsoNamed)          // there, but nothing to rename
        let json = #"{"scope":"conversation","name":"Alex","change_id":"c-1","undo_until":9e9,"sitting":{"parts":4,"span_s":3900,"named":4,"named_parts":3,"before":3,"after":0,"ids":[7,8,9,10]}}"#
        let r = try JSONDecoder().decode(ReassignReceipt.self, from: Data(json.utf8))
        XCTAssertEqual(r.sitting?.alsoNamed, "Also named in 3 earlier parts")
        XCTAssertEqual(r.otherIds, [7, 8, 9, 10])
        XCTAssertEqual(try JSONDecoder().decode(ReassignReceipt.self, from: Data(#"{"change_id":"c"}"#.utf8)).otherIds, [])
        var p = PendingLabels()
        let o = UndoOffer(name: "Alex", changeId: "c-1", scope: .conversation, row: 0,
                          change: p.record(scope: .conversation, tag: "Speaker A", paragraph: 0, name: "Alex"),
                          detail: r.sitting?.alsoNamed)
        XCTAssertEqual(o.title, "Changed to Alex")
        XCTAssertEqual(o.detail, "Also named in 3 earlier parts")
        XCTAssertEqual(ReassignReceipt.tourSample(sitting: .tourSample).sitting?.alsoNamed, "Also named in 3 earlier parts")
    }

    func testCoveragePath() {
        XCTAssertEqual(SittingScope.path(deviceId: "F00D", startMs: 1000, tag: "Speaker B"),
                       "conversations/F00D/1000/speaker/Speaker%20B/sitting")
    }
}
