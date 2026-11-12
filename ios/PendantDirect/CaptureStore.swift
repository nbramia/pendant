import Foundation

/// Durable on-phone spool of raw `PendantAllMsg` payloads.
///
/// Each record is `[u32 LE length][payload]`, appended to a segment file in
/// Documents/spool/open/. `sync()` fsyncs the open segment; only after that
/// may the caller ack the pendant. Segments rotate by size or age and move to
/// Documents/spool/ready/ for upload. The server decodes the payloads with the
/// same protobuf schema the Python tools use, so the phone stays dumb.
final class CaptureStore {
    static let maxSegmentBytes = 4 * 1024 * 1024
    static let maxSegmentAge: TimeInterval = 10 * 60

    let root: URL
    let openDir: URL
    let readyDir: URL
    private var handle: FileHandle?
    private var currentURL: URL?
    private var currentBytes = 0
    private var openedAt = Date()
    private let fm = FileManager.default

    /// `lane` separates concurrently open segments: one per connected pendant.
    init(lane: String = "shared") {
        root = fm.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("spool")
        openDir = root.appendingPathComponent("open").appendingPathComponent(lane)
        readyDir = root.appendingPathComponent("ready")
        for dir in [openDir, readyDir] {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        // Anything this lane left open in a previous run is durable up to its last
        // fsync; seal it so it uploads. (Also sweeps files from before lanes existed.)
        let legacy = root.appendingPathComponent("open")
        for dir in [openDir, legacy] {
            for url in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.isRegularFileKey])) ?? []
            where url.pathExtension == "pmsg" {
                try? fm.moveItem(at: url, to: readyDir.appendingPathComponent(url.lastPathComponent))
            }
        }
    }

    /// Appends one message. Not durable until `sync()` returns true.
    func append(_ payload: [UInt8], device: String) throws {
        if handle == nil || currentBytes >= Self.maxSegmentBytes
            || Date().timeIntervalSince(openedAt) >= Self.maxSegmentAge {
            try rotate(device: device)
        }
        var len = UInt32(payload.count).littleEndian
        var record = Data(bytes: &len, count: 4)
        record.append(contentsOf: payload)
        try handle!.write(contentsOf: record)
        currentBytes += record.count
    }

    /// fsyncs the open segment. Returns false if the data may not be on disk.
    @discardableResult
    func sync() -> Bool {
        guard let h = handle else { return true }
        do {
            try h.synchronize()
            return true
        } catch {
            return false
        }
    }

    /// Seals the open segment (fsync + move to ready/).
    func seal() {
        guard let h = handle, let url = currentURL else { return }
        try? h.synchronize()
        try? h.close()
        handle = nil
        currentURL = nil
        if currentBytes == 0 {
            try? fm.removeItem(at: url)
        } else {
            try? fm.moveItem(at: url, to: readyDir.appendingPathComponent(url.lastPathComponent))
        }
        currentBytes = 0
    }

    private func rotate(device: String) throws {
        seal()
        let stamp = ISO8601DateFormatter.compact.string(from: Date())
        let url = openDir.appendingPathComponent("\(device)_\(stamp).pmsg")
        fm.createFile(atPath: url.path, contents: nil, attributes: [
            .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication,
        ])
        handle = try FileHandle(forWritingTo: url)
        currentURL = url
        openedAt = Date()
    }

    func readySegments() -> [URL] {
        ((try? fm.contentsOfDirectory(at: readyDir, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "pmsg" || $0.pathExtension == "log" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    func spoolBytes() -> Int {
        let urls = ((try? fm.contentsOfDirectory(at: readyDir, includingPropertiesForKeys: [.fileSizeKey])) ?? [])
        return urls.reduce(currentBytes) { sum, url in
            sum + ((try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
    }
}

extension ISO8601DateFormatter {
    static let compact: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withYear, .withMonth, .withDay, .withTime, .withTimeZone]
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()
}
