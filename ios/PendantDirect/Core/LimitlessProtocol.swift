import CoreBluetooth
import Foundation

/// Limitless pendant BLE protocol: GATT UUIDs, the fragment wrapper, the
/// commands we send, and the parts of `PendantAllMsg` we read.
/// Field numbers come from proto/server.proto in an earlier prototype and
/// match BasedHardware/omi's limitless_connection.dart.
enum Limitless {
    static let service = CBUUID(string: "632DE001-604C-446B-A80F-7963E950F3FB")
    static let writeChar = CBUUID(string: "632DE002-604C-446B-A80F-7963E950F3FB")
    static let notifyChar = CBUUID(string: "632DE003-604C-446B-A80F-7963E950F3FB")

    // MARK: Fragment wrapper {1: index, 2: seq, 3: num_fragments, 4: payload}

    static func encodeFragments(index: UInt32, payload: [UInt8], maxChunk: Int) -> [Data] {
        let size = max(20, maxChunk)
        var chunks: [[UInt8]] = stride(from: 0, to: payload.count, by: size).map {
            Array(payload[$0..<min($0 + size, payload.count)])
        }
        if chunks.isEmpty { chunks = [[]] }
        return chunks.enumerated().map { seq, chunk in
            Data(Proto.varintField(1, UInt64(index))
                + Proto.varintField(2, UInt64(seq))
                + Proto.varintField(3, UInt64(chunks.count))
                + Proto.bytesField(4, chunk))
        }
    }

    struct Fragment {
        let index: UInt64
        let seq: UInt64
        let count: UInt64
        let payload: [UInt8]
    }

    static func decodeFragment(_ data: Data) -> Fragment? {
        guard let f = try? Proto.parse([UInt8](data)) else { return nil }
        return Fragment(index: f.uint(1) ?? 0, seq: f.uint(2) ?? 0,
                        count: f.uint(3) ?? 0, payload: f.bytes(4) ?? [])
    }

    /// Reassembles fragments per message index into full `PendantAllMsg` payloads.
    final class Reassembler {
        private var partial: [UInt64: (expected: UInt64, parts: [UInt64: [UInt8]])] = [:]

        func add(_ frag: Fragment) -> [UInt8]? {
            var entry = partial[frag.index] ?? (expected: frag.count, parts: [:])
            if entry.expected == 0 { entry.expected = frag.count }
            entry.parts[frag.seq] = frag.payload
            if entry.expected > 0, UInt64(entry.parts.count) >= entry.expected {
                partial[frag.index] = nil
                return (0..<entry.expected).flatMap { entry.parts[$0] ?? [] }
            }
            partial[frag.index] = entry
            return nil
        }

        func reset() { partial.removeAll() }
    }

    // MARK: Commands (ServerCommandMsg)

    private static var requestID: UInt64 = UInt64(Date().timeIntervalSince1970)
    /// Command field number by request ID, so responses can be matched in logs.
    static var sentCommands: [UInt64: Int] = [:]

    /// Wraps one oneof field with RequestData (field 30) like the official apps do.
    private static func command(_ field: Int, _ body: [UInt8]) -> [UInt8] {
        requestID += 1
        sentCommands[requestID] = field
        if sentCommands.count > 500 { sentCommands.removeAll() }
        let requestData = Proto.varintField(1, requestID) + Proto.varintField(2, 0)
        return Proto.bytesField(field, body) + Proto.bytesField(30, requestData)
    }

    static func setCurrentTime(_ date: Date = Date()) -> [UInt8] {
        command(6, Proto.varintField(1, UInt64(date.timeIntervalSince1970 * 1000)))
    }

    /// batch = drain stored flash pages; realtime = stream live audio.
    /// {false,false} leaves the pendant recording to flash only.
    static func downloadFlashPages(batch: Bool, realtime: Bool) -> [UInt8] {
        command(8, Proto.varintField(1, batch ? 1 : 0) + Proto.varintField(2, realtime ? 1 : 0))
    }

    /// Frees every flash page with index <= `upTo`. Only send after those pages are durable.
    static func deleteFlashPages(upTo index: Int64) -> [UInt8] {
        command(7, Proto.varintField(1, UInt64(max(0, index))))
    }

    static func getDeviceStatus() -> [UInt8] { command(21, []) }
    static func getDeviceInfo() -> [UInt8] { command(14, []) }

    // MARK: Inbound (PendantAllMsg)

    enum IngestType: UInt64 { case unspecified = 0, realtime = 1, batch = 2, command = 3 }

    struct StorageBuffer {
        let ingestType: IngestType
        let session: Int64
        let seq: Int64
        let index: Int64
        let flashPageBytes: Int
        /// The page carries sound (an audio chunk with Opus or encrypted Opus). A pendant
        /// that is switched off stays connected and keeps writing status-only pages.
        var hasAudio: Bool = false
        /// The page's own recording time (FlashPage.absolute_timestamp_ms), nil if absent.
        var pageTimeMs: Int64? = nil

        /// Sound recorded within `window` seconds of `now`. Drained pages hold sound from
        /// earlier, so only a page whose own timestamp is recent counts as hearing now; a
        /// live page without a timestamp counts as now.
        func isRecentSound(now: Date, window: TimeInterval = 120) -> Bool {
            guard hasAudio else { return false }
            guard let ms = pageTimeMs else { return ingestType == .realtime }
            return abs(now.timeIntervalSince1970 - Double(ms) / 1000) < window
        }
    }

    /// FlashPage.absolute_timestamp_ms (field 1); nil when absent or unparseable.
    static func flashPageTimeMs(_ page: [UInt8]) -> Int64? {
        guard let fields = try? Proto.parse(page), let t = fields.int(1), t > 0 else { return nil }
        return t
    }

    /// Whether a serialized FlashPage holds any audio: some Chunk (field 3) whose
    /// audio_data (field 2) has codec_beamforming_data (4), codec_manual_beamforming_data (5)
    /// or encrypted_codec_beamforming_data (13). Status-only chunks don't count.
    static func flashPageHasAudio(_ page: [UInt8]) -> Bool {
        guard let fields = try? Proto.parse(page) else { return false }
        for (f, v) in fields.items where f == 3 {
            guard case .bytes(let raw) = v, let chunk = try? Proto.parse(raw),
                  let audio = chunk.message(2) else { continue }
            if !(audio.bytes(4) ?? []).isEmpty || !(audio.bytes(5) ?? []).isEmpty || audio.bytes(13) != nil {
                return true
            }
        }
        return false
    }

    struct StorageState {
        let oldest: Int64
        let newest: Int64
        let session: Int64
        let free: Int64
        let total: Int64
    }

    struct DeviceInfo {
        let deviceID: UInt64
        let serial: String
        let firmware: String
        let batteryPercent: Int64
        let storageInstance: UInt64
        let oldest: Int64
        let newest: Int64
    }

    enum Message {
        case storage(StorageBuffer)
        case status(StorageState?, batterySoc: Int64?, syncMode: UInt64?, recordingState: UInt64?)
        case info(DeviceInfo)
        case timeSet(invalidRTCResets: UInt64?)
        case battery(soc: Int64?)
        /// ResponseData with no other content: a command's ack or error.
        case response(command: Int?, requestID: UInt64, errorCategory: UInt64?, error: String?)
        case other(field: Int)
    }

    static func decode(_ payload: [UInt8]) -> Message? {
        guard let msg = try? Proto.parse(payload) else { return nil }
        if let sb = msg.message(2) {
            return .storage(StorageBuffer(
                ingestType: IngestType(rawValue: sb.uint(1) ?? 0) ?? .unspecified,
                session: sb.int(2) ?? 0, seq: sb.int(4) ?? 0, index: sb.int(5) ?? 0,  // proto3 omits 0
                flashPageBytes: sb.bytes(6)?.count ?? 0,
                hasAudio: sb.bytes(6).map(flashPageHasAudio) ?? false,
                pageTimeMs: sb.bytes(6).flatMap(flashPageTimeMs)))
        }
        if let ds = msg.message(5) {
            let st = ds.message(5).map {
                StorageState(oldest: $0.int(1) ?? 0, newest: $0.int(2) ?? 0, session: $0.int(3) ?? 0,
                             free: $0.int(4) ?? 0, total: $0.int(5) ?? 0)
            }
            return .status(st, batterySoc: ds.message(9)?.int(5), syncMode: ds.uint(6),
                           recordingState: ds.message(7)?.uint(1))
        }
        if let di = msg.message(1) {
            return .info(DeviceInfo(
                deviceID: di.uint(1) ?? 0,
                serial: String(decoding: di.bytes(2) ?? [], as: UTF8.self),
                firmware: String(decoding: di.bytes(3) ?? [], as: UTF8.self),
                batteryPercent: di.int(12) ?? -1, storageInstance: di.uint(5) ?? 0, oldest: di.int(10) ?? -1, newest: di.int(11) ?? -1))
        }
        if let t = msg.message(3) { return .timeSet(invalidRTCResets: t.uint(2)) }
        if let b = msg.message(7) { return .battery(soc: b.int(5)) }
        if let r = msg.message(6), msg.items.count == 1 {
            let id = r.uint(1) ?? 0
            let err = r.message(2)
            return .response(command: sentCommands[id], requestID: id, errorCategory: err?.uint(1),
                             error: err?.bytes(2).map { String(decoding: $0, as: UTF8.self) })
        }
        return .other(field: msg.items.first?.0 ?? 0)
    }
}
