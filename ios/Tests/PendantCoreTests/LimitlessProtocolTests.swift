import XCTest
@testable import PendantCore

final class LimitlessProtocolTests: XCTestCase {
    func testFragmentRoundTripAndReassembly() {
        let payload = (0..<1000).map { UInt8($0 % 251) }
        let frags = Limitless.encodeFragments(index: 7, payload: payload, maxChunk: 300)
        XCTAssertEqual(frags.count, 4)
        let r = Limitless.Reassembler()
        var out: [UInt8]?
        for d in frags.reversed() {  // order of arrival shouldn't matter
            let f = Limitless.decodeFragment(d)!
            XCTAssertEqual(f.index, 7)
            XCTAssertEqual(f.count, 4)
            out = r.add(f) ?? out
        }
        XCTAssertEqual(out, payload)
    }

    func testEmptyPayloadStillSendsOneFragment() {
        XCTAssertEqual(Limitless.encodeFragments(index: 1, payload: [], maxChunk: 100).count, 1)
    }

    /// Byte layout must match Omi's acknowledgeProcessedData:
    /// field 7 {1: upTo} then field 30 RequestData {1: id, 2: 0}.
    func testDeleteFlashPagesEncoding() throws {
        let f = try Proto.parse(Limitless.deleteFlashPages(upTo: 300))
        XCTAssertEqual(f.items.first?.0, 7)
        XCTAssertEqual(f.message(7)?.uint(1), 300)
        let rd = try XCTUnwrap(f.message(30))
        XCTAssertNotNil(rd.uint(1))
        XCTAssertEqual(rd.uint(2), 0)
    }

    func testDownloadFlashPagesEncoding() throws {
        let f = try Proto.parse(Limitless.downloadFlashPages(batch: true, realtime: false))
        XCTAssertEqual(f.message(8)?.uint(1), 1)
        XCTAssertEqual(f.message(8)?.uint(2), 0)
    }

    func testDecodeStorageBuffer() {
        let sb = Proto.varintField(1, 2) + Proto.varintField(5, 1234) + Proto.bytesField(6, [1, 2, 3])
        guard case .storage(let s)? = Limitless.decode(Proto.bytesField(2, sb)) else { return XCTFail() }
        XCTAssertEqual(s.ingestType, .batch)
        XCTAssertEqual(s.index, 1234)
        XCTAssertEqual(s.flashPageBytes, 3)
    }

    /// A switched-off pendant keeps writing pages with status chunks only; those aren't sound.
    func testStatusOnlyPageHasNoAudio() {
        let statusChunk = Proto.varintField(1, 120) + Proto.bytesField(6, Proto.varintField(5, 73))
        let page = Proto.varintField(1, 1_794_175_000_000) + Proto.bytesField(3, statusChunk) + Proto.bytesField(3, statusChunk)
        XCTAssertFalse(Limitless.flashPageHasAudio(page))
        let sb = Proto.varintField(1, 1) + Proto.varintField(5, 9) + Proto.bytesField(6, page)
        guard case .storage(let s)? = Limitless.decode(Proto.bytesField(2, sb)) else { return XCTFail() }
        XCTAssertFalse(s.hasAudio)
    }

    func testPageWithOpusOrEncryptedAudioHasAudio() {
        let status = Proto.bytesField(3, Proto.varintField(1, 5) + Proto.bytesField(9, Proto.varintField(1, 1)))
        let opus = Proto.bytesField(3, Proto.varintField(1, 20) + Proto.bytesField(2, Proto.bytesField(4, [0xFC, 0x01, 0x02])))
        let enc = Proto.bytesField(3, Proto.bytesField(2, Proto.bytesField(13, Proto.bytesField(1, [1, 2, 3]))))
        let flagsOnly = Proto.bytesField(3, Proto.bytesField(2, Proto.varintField(8, 1)))
        XCTAssertTrue(Limitless.flashPageHasAudio(status + opus))
        XCTAssertTrue(Limitless.flashPageHasAudio(enc))
        XCTAssertFalse(Limitless.flashPageHasAudio(flagsOnly))
        XCTAssertFalse(Limitless.flashPageHasAudio([0xFF, 0xFF]))
        let sb = Proto.varintField(1, 1) + Proto.varintField(5, 9) + Proto.bytesField(6, status + opus)
        guard case .storage(let s)? = Limitless.decode(Proto.bytesField(2, sb)) else { return XCTFail() }
        XCTAssertTrue(s.hasAudio)
    }

    /// Drained sound from earlier isn't "hearing now"; recent sound is, live or drained.
    func testRecentSoundUsesThePageTimestamp() {
        let now = Date(timeIntervalSince1970: 1_794_175_000)
        func page(_ ms: UInt64?) -> [UInt8] {
            (ms.map { Proto.varintField(1, $0) } ?? [])
                + Proto.bytesField(3, Proto.bytesField(2, Proto.bytesField(4, [0xFC, 1])))
        }
        func sb(_ type: UInt64, _ p: [UInt8]) -> Limitless.StorageBuffer {
            let m = Proto.varintField(1, type) + Proto.varintField(5, 3) + Proto.bytesField(6, p)
            guard case .storage(let s)? = Limitless.decode(Proto.bytesField(2, m)) else { fatalError() }
            return s
        }
        let nowMs = UInt64(now.timeIntervalSince1970 * 1000)
        XCTAssertTrue(sb(2, page(nowMs - 30_000)).isRecentSound(now: now))      // drained, 30 s old
        XCTAssertFalse(sb(2, page(nowMs - 3_600_000)).isRecentSound(now: now))  // drained, an hour old
        XCTAssertTrue(sb(1, page(nowMs)).isRecentSound(now: now))               // live
        XCTAssertTrue(sb(1, page(nil)).isRecentSound(now: now))                 // live, no stamp
        XCTAssertFalse(sb(2, page(nil)).isRecentSound(now: now))                // drained, no stamp
        let silent = Proto.varintField(1, nowMs) + Proto.bytesField(3, Proto.varintField(1, 5))
        XCTAssertFalse(sb(1, silent).isRecentSound(now: now))                   // status only
    }

    func testDecodeStatusWithWipedSentinel() {
        let state = Proto.varintField(1, 0) + Proto.varintField(2, UInt64(bitPattern: -1)) + Proto.varintField(4, 95232)
        let msg = Proto.bytesField(5, Proto.bytesField(5, state))
        guard case .status(let st?, _, _, _)? = Limitless.decode(msg) else { return XCTFail() }
        XCTAssertEqual(st.newest, -1)
        XCTAssertEqual(st.free, 95232)
    }

    func testDecodeResponseMatchesSentCommand() throws {
        let cmd = Limitless.deleteFlashPages(upTo: 5)
        let reqID = try XCTUnwrap(Proto.parse(cmd).message(30)?.uint(1))
        let resp = Proto.bytesField(6, Proto.varintField(1, reqID))
        guard case .response(let command, let id, _, let error)? = Limitless.decode(resp) else { return XCTFail() }
        XCTAssertEqual(command, 7)
        XCTAssertEqual(id, reqID)
        XCTAssertNil(error)
    }
}

final class PageZeroTests: XCTestCase {
    /// proto3 leaves index 0 off the wire; it must decode as page 0, not "missing".
    func testPageZeroDecodesAsZero() {
        let sb = Proto.varintField(1, 2) + Proto.bytesField(6, [9])
        guard case .storage(let s)? = Limitless.decode(Proto.bytesField(2, sb)) else { return XCTFail() }
        XCTAssertEqual(s.index, 0)
    }
}
