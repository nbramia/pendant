import XCTest
@testable import PendantCore

final class PlaybackTests: XCTestCase {
    let utc = TimeZone(identifier: "UTC")!
    // 2026-11-08 12:03:30 UTC
    let startMs = 1_794_139_410_000

    func testOffsetsWinOverTheStamp() {
        let t = Playback.seekTime(index: 1, stamp: "12:09", offsets: [[0.5, 3], [41.2, 50]], startMs: startMs, timeZone: utc)
        XCTAssertEqual(t, 41.2)
    }

    func testMinuteStampFallsBackToTimeSinceStart() {
        // 12:09:00 - 12:03:30 = 330 s
        XCTAssertEqual(Playback.seekTime(index: 2, stamp: "12:09", offsets: nil, startMs: startMs, timeZone: utc)!, 330, accuracy: 0.01)
        XCTAssertEqual(Playback.seekTime(index: 0, stamp: "12:09", offsets: [], startMs: startMs, timeZone: utc)!, 330, accuracy: 0.01)
    }

    func testStampBeforeStartClampsToZero() {
        XCTAssertEqual(Playback.seekTime(index: 0, stamp: "12:03", offsets: nil, startMs: startMs, timeZone: utc), 0)
    }

    func testNothingPlacesALine() {
        XCTAssertNil(Playback.seekTime(index: nil, stamp: nil, offsets: nil, startMs: startMs, timeZone: utc))
        XCTAssertNil(Playback.seekTime(index: 0, stamp: "bad", offsets: nil, startMs: startMs, timeZone: utc))
        XCTAssertNil(Playback.seekTime(index: 0, stamp: "12:09", offsets: nil, startMs: nil, timeZone: utc))
    }

    func testActiveLineIsTheLastOneStartedAndSkipsUnplacedLines() {
        let starts: [Double?] = [0, nil, 10, 25, nil]
        XCTAssertEqual(Playback.activeLine(starts: starts, at: 0), 0)
        XCTAssertEqual(Playback.activeLine(starts: starts, at: 9.9), 0)
        XCTAssertEqual(Playback.activeLine(starts: starts, at: 10), 2)
        XCTAssertEqual(Playback.activeLine(starts: starts, at: 1000), 3)
        XCTAssertNil(Playback.activeLine(starts: [5, 9], at: 1))
        XCTAssertNil(Playback.activeLine(starts: [], at: 3))
    }

    func testClockAndSpeeds() {
        XCTAssertEqual(Playback.clock(65.9), "1:05")
        XCTAssertEqual(Playback.clock(3723), "1:02:03")
        XCTAssertEqual(Playback.clock(.nan), "0:00")
        XCTAssertEqual(Playback.nextSpeed(after: 1), 1.5)
        XCTAssertEqual(Playback.nextSpeed(after: 2), 1)
        XCTAssertEqual(Playback.speedLabel(1.5), "1.5×")
        XCTAssertEqual(Playback.speedLabel(2), "2×")
        XCTAssertEqual(Playback.audioPath(device: "AB", startMs: 7), "conversations/AB/7/audio")
    }

    func testStampAfterMidnightIsTheNextDay() {
        // starts 23:59:10 UTC; a 00:01 line is 50 s + 60 s = 110 s in
        let ms = 1_794_182_350_000
        XCTAssertEqual(Playback.seekTime(index: 0, stamp: "00:01", offsets: nil, startMs: ms, timeZone: utc)!, 110, accuracy: 0.01)
        XCTAssertEqual(Playback.seekTime(index: 0, stamp: "23:59", offsets: nil, startMs: ms, timeZone: utc), 0)
    }

    func testLinesSharingAMinuteSpreadByWords() {
        // starts 12:03:30; three lines stamped 12:09 (words 10, 30, 20) share 330...390 s
        let lines = [Playback.Line(index: 0, stamp: "12:09", words: 10), .init(index: 1, stamp: "12:09", words: 30),
                     .init(index: 2, stamp: "12:09", words: 20), .init(index: 3, stamp: "12:10", words: 5)]
        let s = Playback.lineStarts(lines, offsets: nil, startMs: startMs, timeZone: utc)
        XCTAssertEqual(s[0]!, 330, accuracy: 0.01)
        XCTAssertEqual(s[1]!, 340, accuracy: 0.01)
        XCTAssertEqual(s[2]!, 370, accuracy: 0.01)
        XCTAssertEqual(s[3]!, 390, accuracy: 0.01)
        XCTAssertEqual(Playback.activeLine(starts: s, at: 345), 1)
    }

    func testSpreadInTheFirstMinuteStartsAtZeroAndOffsetsAreLeftAlone() {
        let lines = [Playback.Line(index: 0, stamp: "12:03", words: 1), .init(index: 1, stamp: "12:03", words: 1)]
        let s = Playback.lineStarts(lines, offsets: nil, startMs: startMs, timeZone: utc)
        XCTAssertEqual(s[0]!, 0, accuracy: 0.01)
        XCTAssertEqual(s[1]!, 15, accuracy: 0.01)   // 0...30 s remain in that minute
        let o = Playback.lineStarts(lines, offsets: [[4, 5], [9, 12]], startMs: startMs, timeZone: utc)
        XCTAssertEqual(o, [4, 9])
    }
}

final class LineTapTests: XCTestCase {
    func testATapLoadsThenPlaysAtTheLine() {
        var t = LineTap()
        XCTAssertEqual(t.state, .idle)
        XCTAssertTrue(t.tap(3))
        XCTAssertEqual(t.state, .loading(3))
        XCTAssertTrue(t.isLoading(3))
        XCTAssertEqual(t.target, 3)
        XCTAssertTrue(t.ready(at: 3))
        XCTAssertEqual(t.state, .playing(3))
        XCTAssertNil(t.target)
    }

    func testTappingTheSameLineWhileLoadingIsANoOp() {
        var t = LineTap()
        XCTAssertTrue(t.tap(3))
        XCTAssertFalse(t.tap(3))
        XCTAssertFalse(t.tap(3))
        XCTAssertEqual(t.state, .loading(3))
    }

    func testTappingAnotherLineWhileLoadingMovesTheTargetWithoutASecondLoad() {
        var t = LineTap()
        XCTAssertTrue(t.tap(3))
        XCTAssertFalse(t.tap(5))          // no second load
        XCTAssertEqual(t.state, .loading(5))
        XCTAssertFalse(t.isLoading(3))
        XCTAssertFalse(t.ready(at: 3))    // positioned at the old line: seek again
        XCTAssertEqual(t.state, .loading(5))
        XCTAssertTrue(t.ready(at: 5))
        XCTAssertEqual(t.state, .playing(5))
    }

    func testAFailureShowsOnTheLineAndATapRetries() {
        var t = LineTap()
        _ = t.tap(2)
        t.fail("Couldn't load the audio")
        XCTAssertEqual(t.state, .failed(2, "Couldn't load the audio"))
        XCTAssertEqual(t.error(on: 2), "Couldn't load the audio")
        XCTAssertNil(t.error(on: 1))
        XCTAssertTrue(t.tap(2))           // retry starts a new load
        XCTAssertEqual(t.state, .loading(2))
        XCTAssertNil(t.error(on: 2))
        XCTAssertTrue(t.ready(at: 2))
    }

    func testLateAnswersChangeNothing() {
        var t = LineTap()
        t.fail("x")                       // nothing loading
        XCTAssertEqual(t.state, .idle)
        XCTAssertFalse(t.ready(at: 0))
        _ = t.tap(1)
        XCTAssertTrue(t.ready(at: 1))
        t.fail("late")                    // already playing
        XCTAssertEqual(t.state, .playing(1))
        XCTAssertTrue(t.tap(4))           // a tap while playing starts a new load
    }
}
