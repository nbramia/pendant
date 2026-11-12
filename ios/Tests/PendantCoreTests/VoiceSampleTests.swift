import XCTest
@testable import PendantCore

final class VoiceSampleTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_794_000_000)

    func testEightBundledSentencesAboutAMinute() {
        let s = VoiceSampleScript.sentences
        XCTAssertEqual(s.count, 8)
        XCTAssertEqual(Set(s).count, 8)
        let words = s.joined(separator: " ").split(separator: " ").count
        XCTAssertTrue((100...170).contains(words), "\(words) words is about a minute at a normal pace")
    }

    func testNamesFollowTheServersRules() {
        XCTAssertEqual(VoiceSampleFlow.clean("  Riley  "), "Riley")
        XCTAssertEqual(VoiceSampleFlow.clean("Mary   Ann"), "Mary Ann")
        XCTAssertEqual(VoiceSampleFlow.clean("O'Neil-Smith"), "O'Neil-Smith")
        XCTAssertNil(VoiceSampleFlow.clean(""))
        XCTAssertNil(VoiceSampleFlow.clean("   "))
        XCTAssertNil(VoiceSampleFlow.clean("3PO"))
        XCTAssertNil(VoiceSampleFlow.clean("Bob!"))
        XCTAssertNil(VoiceSampleFlow.clean(String(repeating: "a", count: 41)))
    }

    func testReadingNeedsANameAndAConnectedPendant() {
        var f = VoiceSampleFlow()
        XCTAssertFalse(f.start(now: t0, device: "E1"), "no name yet")
        f.choose(name: "123")
        XCTAssertEqual(f.phase, .name, "an invalid name is not a choice")
        f.choose(name: "Riley")
        XCTAssertFalse(f.start(now: t0, device: nil), "no pendant to listen")
        XCTAssertFalse(f.start(now: t0, device: ""))
        XCTAssertTrue(f.start(now: t0, device: "A1B2C3D4E5F60718"))
        XCTAssertEqual(f.phase, .reading(start: t0))
        XCTAssertEqual(f.elapsed(now: t0.addingTimeInterval(12.5)), 12.5, accuracy: 0.001)
    }

    func testDoneSendsTheWallClockWindowAndThePendant() {
        var f = VoiceSampleFlow()
        f.choose(name: "Riley")
        f.start(now: t0, device: "A1B2C3D4E5F60718")
        let req = f.done(now: t0.addingTimeInterval(58.4))
        XCTAssertEqual(req, EnrollRequest(name: "Riley", device: "A1B2C3D4E5F60718",
                                          startMs: 1_794_000_000_000, endMs: 1_794_000_058_400))
        XCTAssertEqual(f.phase, .sending)
        let body = req!.body
        XCTAssertEqual(body["name"] as? String, "Riley")
        XCTAssertEqual(body["device"] as? String, "A1B2C3D4E5F60718")
        XCTAssertEqual(body["start_ms"] as? Int, 1_794_000_000_000)
        XCTAssertEqual(body["end_ms"] as? Int, 1_794_000_058_400)
        XCTAssertEqual(EnrollRequest.path, "enroll")
    }

    func testTooShortIsNotSentAndCanBeRetried() {
        var f = VoiceSampleFlow()
        f.choose(name: "Riley")
        f.start(now: t0, device: "E1")
        XCTAssertNil(f.done(now: t0.addingTimeInterval(12)))
        XCTAssertEqual(f.phase, .tooShort(seconds: 12))
        XCTAssertTrue(f.canStart)
        XCTAssertTrue(f.start(now: t0.addingTimeInterval(60), device: "E1"))
    }

    func testAddedAfterWaiting() {
        var f = VoiceSampleFlow()
        f.choose(name: "Riley")
        f.start(now: t0, device: "E1")
        _ = f.done(now: t0.addingTimeInterval(55))
        f.sent(EnrollStatus(id: 4, name: "Riley", status: "waiting"))
        XCTAssertEqual(f.phase, .waiting(id: 4))
        XCTAssertFalse(f.isFinished)
        f.apply(EnrollStatus(id: 4, name: "Riley", status: "added", samples: 6))
        XCTAssertEqual(f.phase, .added(samples: 6))
        XCTAssertTrue(f.isFinished)
    }

    func testRejectedKeepsTheNameForAnotherTry() {
        var f = VoiceSampleFlow()
        f.choose(name: "Riley")
        f.start(now: t0, device: "E1")
        _ = f.done(now: t0.addingTimeInterval(55))
        f.sent(EnrollStatus(id: 4, status: "waiting"))
        f.apply(EnrollStatus(id: 4, status: "rejected", reason: "more than one voice"))
        XCTAssertEqual(f.phase, .rejected(reason: "more than one voice"))
        f.tryAgain()
        XCTAssertEqual(f.phase, .ready)
        XCTAssertEqual(f.name, "Riley")
        XCTAssertNil(f.request)
    }

    func testAnImmediateVerdictFromThePost() {
        var f = VoiceSampleFlow()
        f.choose(name: "Someone")
        f.start(now: t0, device: "E1")
        _ = f.done(now: t0.addingTimeInterval(55))
        f.sent(EnrollStatus(id: 9, status: "rejected", reason: "not allowed"))
        XCTAssertEqual(f.phase, .rejected(reason: "not allowed"))
    }

    func testAFailedSendKeepsTheSameWindow() {
        var f = VoiceSampleFlow()
        f.choose(name: "Riley")
        f.start(now: t0, device: "E1")
        let first = f.done(now: t0.addingTimeInterval(55))
        f.sendFailed()
        XCTAssertEqual(f.phase, .sendFailed)
        XCTAssertEqual(f.retrySend(), first, "the window is resent as it was, not re-measured")
        XCTAssertEqual(f.phase, .sending)
        XCTAssertNil(f.retrySend(), "nothing to resend while sending")
    }

    func testDecodesTheServersAnswers() throws {
        func status(_ json: String) throws -> EnrollStatus {
            try JSONDecoder().decode(EnrollStatus.self, from: json.data(using: .utf8)!)
        }
        XCTAssertEqual(try status(#"{"id": 3, "name": "Riley", "status": "waiting"}"#).outcome, .waiting)
        XCTAssertEqual(try status(#"{"id": 3, "name": "Riley", "status": "added", "samples": 7}"#).outcome, .added(samples: 7))
        XCTAssertEqual(try status(#"{"id": 3, "name": "Riley", "status": "rejected", "reason": "too little speech"}"#).outcome,
                       .rejected(reason: "too little speech"))
        XCTAssertEqual(try status(#"{"id": 3, "status": "something new"}"#).outcome, .waiting, "an unknown state keeps waiting")
    }

    func testEveryServerReasonHasAdvice() {
        for r in ["too little speech", "more than one voice", "no audio from that pendant", "not allowed",
                  "could not be checked", "overlaps another voice sample"] {
            XCTAssertNotEqual(EnrollOutcome.advice(for: r), r)
        }
        XCTAssertEqual(EnrollOutcome.advice(for: "novel"), "novel")
    }
}

final class PhoneSampleTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_794_000_000)
    private func reading(phone: Bool = true) -> VoiceSampleFlow {
        var f = VoiceSampleFlow()
        f.choose(name: "Riley")
        f.start(now: t0, device: "A1B2C3D4E5F60718", phoneRecording: phone)
        return f
    }

    func testPhoneRecordsOnlyWhenAllowed() {
        XCTAssertEqual(reading().phone, .recording)
        XCTAssertEqual(reading(phone: false).phone, .none, "permission denied: pendant only")
    }

    func testDoneStartsTheUploadAndTooShortDiscardsIt() {
        var f = reading()
        XCTAssertNotNil(f.done(now: t0.addingTimeInterval(58)))
        XCTAssertEqual(f.phone, .uploading)
        var g = reading()
        XCTAssertNil(g.done(now: t0.addingTimeInterval(10)))
        XCTAssertEqual(g.phone, .none)
    }

    func testPathAndDecodedVerdict() throws {
        XCTAssertEqual(VoiceSampleScript.phonePath(id: 12), "enroll/12/phone")
        let json = #"{"id":12,"name":"Riley","status":"waiting","phone":{"status":"added","samples":6}}"#
        let s = try JSONDecoder().decode(EnrollStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.phone, EnrollStatus.Phone(status: "added", samples: 6))
        let old = try JSONDecoder().decode(EnrollStatus.self, from: Data(#"{"id":1,"status":"waiting"}"#.utf8))
        XCTAssertNil(old.phone)
    }

    func testPhoneResultShowsAtOnceWhilePendantCopyPends() {
        var f = reading()
        f.done(now: t0.addingTimeInterval(58))
        f.sent(EnrollStatus(id: 3, status: "waiting"))
        XCTAssertEqual(f.phase, .waiting(id: 3))
        f.phoneSent(EnrollStatus(id: 3, status: "waiting", phone: .init(status: "added", samples: 6)))
        XCTAssertEqual(f.phone, .added(samples: 6))
        XCTAssertEqual(f.phoneLine, "Phone recording: 6 samples added")
        XCTAssertEqual(f.pendantLine, "Pendant copy: pending, added when the pendant syncs")
        XCTAssertTrue(f.anyAdded)
        f.apply(EnrollStatus(id: 3, status: "added", samples: 5, phone: .init(status: "added", samples: 6)))
        XCTAssertEqual(f.pendantLine, "Pendant copy: 5 samples added")
        XCTAssertEqual(f.phase, .added(samples: 5))
    }

    func testRejectedPhoneCopyAndFailedUpload() {
        var f = reading()
        f.done(now: t0.addingTimeInterval(58))
        f.sent(EnrollStatus(id: 3, status: "waiting"))
        f.phoneSent(EnrollStatus(id: 3, status: "waiting", phone: .init(status: "rejected", reason: "more than one voice")))
        XCTAssertEqual(f.phone, .rejected(reason: "more than one voice"))
        XCTAssertTrue(f.phoneLine!.contains("not added"))
        XCTAssertFalse(f.anyAdded)
        f.phoneFailed()
        XCTAssertEqual(f.phone, .failed)
        f.retryPhone()
        XCTAssertEqual(f.phone, .uploading)
        f.phoneFailed()
        XCTAssertEqual(f.phoneLine, "Phone recording didn't upload.")
        f.tryAgain()
        XCTAssertEqual(f.phone, .none)
    }

    func testPhoneAddedSurvivesARejectedPendantCopy() {
        var f = reading()
        f.done(now: t0.addingTimeInterval(58))
        f.sent(EnrollStatus(id: 3, status: "waiting"))
        f.phoneSent(EnrollStatus(id: 3, status: "waiting", phone: .init(status: "added", samples: 6)))
        f.apply(EnrollStatus(id: 3, status: "rejected", reason: "no audio from that pendant", phone: .init(status: "added", samples: 6)))
        XCTAssertTrue(f.anyAdded)
        XCTAssertTrue(f.pendantLine!.contains("not added"))
    }
}
