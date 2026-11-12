import CoreBluetooth
import Foundation
import UIKit

/// Keeps every paired pendant connected at the same time (one PendantSession
/// each), with CoreBluetooth state restoration so iOS brings the app back.
/// Pendants are interchangeable: whichever the wearer wears streams live, the
/// others drain their memory and idle. Everything runs on the main queue.
@MainActor
final class PendantManager: NSObject, ObservableObject {
    static let shared = PendantManager()
    static let restoreID = "\(Bundle.main.bundleIdentifier ?? "com.example.pendant").central"   // CoreBluetooth state restoration

    // MARK: Published state for the UI

    /// Summary of the pendant that matters most right now (see `primary`).
    @Published var state = "starting" {
        didSet { if state != oldValue { log("state: \(oldValue) -> \(state)") } }
    }
    @Published var pairedCount = 0
    @Published var livePages = 0 {
        didSet { if livePages > oldValue { liveActivity.heard(pages: livePages - oldValue) } }
    }
    @Published var batchPages = 0 {
        didSet { if batchPages > oldValue { liveActivity.heard(pages: batchPages - oldValue) } }
    }
    @Published var lastDrainRate: Double?
    @Published var spoolBytes = 0
    @Published var uploadedBytes = 0
    @Published var logLines: [String] = []
    /// What the UI knows about each pendant, kept across launches.
    @Published var pendants: [String: PendantSnapshot] = PendantSnapshot.load() {
        didSet { PendantSnapshot.save(pendants) }
    }
    /// Hex ID of the pendant hearing audio now (or any connected one).
    @Published var activeDevice: String?
    /// Hex IDs of every connected pendant.
    @Published var connectedDevices: Set<String> = []
    @Published var liveEnabled: Bool {
        didSet {
            UserDefaults.standard.set(liveEnabled, forKey: "liveEnabled")
            sessions.values.forEach { $0.applyStreamingMode() }
        }
    }

    // MARK: Internals

    private var central: CBCentralManager!
    private var sessions: [UUID: PendantSession] = [:]
    /// Shared spool for upload bookkeeping and log rotation.
    let store = CaptureStore(lane: "shared")
    private(set) var uploader: Uploader!
    private let logger = FileLog()
    private var tick: Timer?
    /// Lock-screen Live Activity, Dynamic Island and home-screen widget.
    private lazy var liveActivity = LiveActivityController { [weak self] line in self?.log(line) }
    private var lastHealthReport: [String: Date] = [:]

    private var deviceIDs: [String: String] {
        get { UserDefaults.standard.dictionary(forKey: "deviceIDs") as? [String: String] ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: "deviceIDs") }
    }

    func deviceID(for id: UUID) -> String? { deviceIDs[id.uuidString] }

    func setDeviceID(_ hex: String, for id: UUID) {
        if deviceIDs[id.uuidString] != hex { deviceIDs[id.uuidString] = hex }
        sessionsChanged()
    }

    /// Every pendant paired to this app.
    private var knownPeripheralIDs: [UUID] {
        get {
            var ids = (UserDefaults.standard.stringArray(forKey: "peripheralIDs") ?? []).compactMap(UUID.init)
            if ids.isEmpty, let legacy = UserDefaults.standard.string(forKey: "peripheralID").flatMap(UUID.init) {
                ids = [legacy]
            }
            return ids
        }
        set {
            UserDefaults.standard.set(newValue.map(\.uuidString), forKey: "peripheralIDs")
            pairedCount = newValue.count
        }
    }

    override init() {
        liveEnabled = UserDefaults.standard.object(forKey: "liveEnabled") as? Bool ?? true
        super.init()
        pairedCount = knownPeripheralIDs.count
        uploader = Uploader(store: store) { [weak self] line in
            Task { @MainActor in self?.log(line) }
        }
        uploader.onUploaded = { [weak self] _, size in
            Task { @MainActor in self?.uploadedBytes += size }
        }
        central = CBCentralManager(delegate: self, queue: .main, options: [
            CBCentralManagerOptionRestoreIdentifierKey: Self.restoreID,
        ])
        UIDevice.current.isBatteryMonitoringEnabled = true
        tick = Timer.scheduledTimer(withTimeInterval: 90, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.periodic() }
        }
    }

    // MARK: Logging

    func log(_ s: String) {
        let line = "\(Self.timeFmt.string(from: Date())) \(s)"
        logLines.append(line)
        if logLines.count > 300 { logLines.removeFirst(logLines.count - 300) }
        logger.write(line)
    }

    private static let timeFmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    // MARK: Session callbacks

    func updateSnapshot(_ session: PendantSession, _ update: (inout PendantSnapshot) -> Void) {
        guard let id = session.deviceID else { return }
        var snap = pendants[id] ?? PendantSnapshot(id: id)
        snap.lastSeen = Date()
        update(&snap)
        pendants[id] = snap
    }

    func refreshSpool() { spoolBytes = store.spoolBytes() }

    /// The pendant that matters most: the one that heard audio most recently,
    /// else one that's syncing, else any connected one.
    private var primary: PendantSession? {
        let ready = sessions.values.filter(\.isReady)
        if let hearing = ready.filter({ ($0.lastAudioAt.map { Date().timeIntervalSince($0) } ?? .infinity) < 120 })
            .max(by: { $0.lastAudioAt! < $1.lastAudioAt! }) { return hearing }
        if let syncing = ready.first(where: { if case .draining = $0.mode { true } else { false } }) { return syncing }
        return ready.first ?? sessions.values.first { $0.peripheral.state == .connected }
    }

    func sessionsChanged() {
        updateState()
        let snap = activeDevice.flatMap { pendants[$0] }
        liveActivity.refresh(appState: state, pendantName: activeDevice.map(PendantSnapshot.displayName),
                             battery: snap?.battery, connected: !connectedDevices.isEmpty)
    }

    private func updateState() {
        connectedDevices = Set(sessions.values.filter { $0.peripheral.state == .connected }.compactMap(\.deviceID))
        guard let p = primary else {
            activeDevice = nil
            if knownPeripheralIDs.isEmpty { state = "not paired: tap Add pendant" }
            else if !state.hasPrefix("scanning") { state = "waiting for a pendant" }
            return
        }
        activeDevice = p.deviceID
        if !p.isReady { state = "discovering"; return }
        switch p.mode {
        case .live:
            // Only claim to be listening when audio is actually arriving: a muted
            // pendant stays connected and streams nothing.
            let recent = p.lastAudioAt.map { Date().timeIntervalSince($0) < 120 } ?? false
            state = recent ? "live" : "quiet"
        case .draining: state = "draining"
        case .idle: state = "connected (flash only)"
        }
    }

    // MARK: Pairing

    /// Scan for a pendant that isn't paired yet and connect to it; iOS bonds on
    /// first encrypted access. (AccessorySetupKit's picker never listed the
    /// pendant, likely because its 128-bit UUID isn't in the primary advert.)
    private var scanning = false

    func addPendant() {
        guard central.state == .poweredOn else {
            log("can't scan: bluetooth state \(central.state.rawValue)")
            return
        }
        scanning = true
        state = "scanning: hold the new pendant near the phone"
        log("scanning for a new pendant")
        central.scanForPeripherals(withServices: [Limitless.service], options: nil)
    }

    fileprivate func discovered(_ p: CBPeripheral, rssi: Int) {
        guard scanning, !knownPeripheralIDs.contains(p.identifier) else { return }
        log("found \(p.name ?? "?") rssi \(rssi)")
        guard rssi > -75 else { return }
        scanning = false
        central.stopScan()
        knownPeripheralIDs.append(p.identifier)
        log("added pendant #\(knownPeripheralIDs.count)")
        session(for: p)
        central.connect(p, options: nil)
    }

    func forgetPendants() {
        for s in sessions.values { central.cancelPeripheralConnection(s.peripheral) }
        knownPeripheralIDs = []
        sessions.removeAll()
        sessionsChanged()
    }

    // MARK: Connection

    @discardableResult
    private func session(for p: CBPeripheral) -> PendantSession {
        if let s = sessions[p.identifier] {
            if s.peripheral !== p { s.rebind(p) }
            return s
        }
        let s = PendantSession(peripheral: p, manager: self)
        sessions[p.identifier] = s
        return s
    }

    /// Holds a pending connect to every known pendant that isn't connected.
    /// Pending connects never time out: iOS completes each one whenever that
    /// pendant comes into range, even with the app suspended.
    private func connectKnown() {
        guard central.state == .poweredOn else { return }
        for p in central.retrievePeripherals(withIdentifiers: knownPeripheralIDs) {
            let s = session(for: p)
            switch p.state {
            case .connected: s.rediscoverIfNeeded()
            case .disconnected: central.connect(p, options: nil)
            default: break
            }
        }
        sessionsChanged()
    }

    private func periodic() {
        sessions.values.forEach { $0.periodic() }
        sessionsChanged()  // re-evaluate "listening" vs "quiet"
        connectKnown()
        uploader.kick()
        logger.rotateIfNeeded(into: store.readyDir)
        refreshSpool()
        reportHealth()
    }

    /// Tells the server each connected pendant's battery and memory (it sends
    /// the Telegram alerts), at most every 5 minutes per pendant (60 s when low or nearly full). Fire-and-forget.
    private func reportHealth() {
        let now = Date()
        let items: [[String: Any]] = connectedDevices.compactMap { (id: String) -> [String: Any]? in
            guard let s = pendants[id] else { return nil }
            // Every 5 minutes per pendant; every 60 s while it is low or nearly full.
            let used = (s.freePages != nil && (s.totalPages ?? 0) > 0)
                ? Double((s.totalPages ?? 0) - (s.freePages ?? 0)) / Double(s.totalPages ?? 1) : 0
            let urgent = (s.battery ?? 100) <= 15 || used >= 0.85
            guard now.timeIntervalSince(lastHealthReport[id] ?? .distantPast) >= (urgent ? 60 : 300) else { return nil }
            return ["device": id,
                    "battery": s.battery.map { $0 as Any } ?? NSNull(),
                    "free_pages": s.freePages.map { $0 as Any } ?? NSNull(),
                    "total_pages": s.totalPages.map { $0 as Any } ?? NSNull()]
        }
        guard !items.isEmpty else { return }
        for i in items { if let id = i["device"] as? String { lastHealthReport[id] = now } }
        Task { try? await Feed.post("health", json: ["pendants": items]) }
    }

    func refreshNow() { periodic() }
}

// MARK: - CBCentralManagerDelegate

extension PendantManager: CBCentralManagerDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        MainActor.assumeIsolated {
            log("bluetooth state \(central.state.rawValue)")
            if central.state == .poweredOn { connectKnown() }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        MainActor.assumeIsolated {
            let restored = dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral] ?? []
            for p in restored {
                session(for: p)
                if !knownPeripheralIDs.contains(p.identifier) { knownPeripheralIDs.append(p.identifier) }
            }
            log("restored by iOS (\(restored.count) pendant(s))")
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                                    advertisementData: [String: Any], rssi RSSI: NSNumber) {
        MainActor.assumeIsolated { discovered(peripheral, rssi: RSSI.intValue) }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        MainActor.assumeIsolated {
            let s = session(for: peripheral)
            log("connected to \(s.label); discovering")
            s.didConnect()
            sessionsChanged()
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral,
                                    error: Error?) {
        MainActor.assumeIsolated {
            log("connect failed: \(error?.localizedDescription ?? "?")")
            if knownPeripheralIDs.contains(peripheral.identifier) { central.connect(peripheral, options: nil) }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral,
                                    error: Error?) {
        MainActor.assumeIsolated {
            let s = sessions[peripheral.identifier]
            log("\(s?.label ?? "pendant") disconnected: \(error?.localizedDescription ?? "clean")")
            s?.didDisconnect()
            // Pending connect: resumes automatically when it's back in range.
            if knownPeripheralIDs.contains(peripheral.identifier) { central.connect(peripheral, options: nil) }
            sessionsChanged()
        }
    }
}

/// Appends log lines to Documents/logs/current.log and periodically moves the
/// file into the upload spool so the server sees the logs too.
final class FileLog {
    private let dir: URL
    private let url: URL
    private var openedAt = Date()

    init() {
        dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("logs")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        url = dir.appendingPathComponent("current.log")
    }

    func write(_ line: String) {
        let data = Data((line + "\n").utf8)
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile()
            h.write(data)
            try? h.close()
        } else {
            try? data.write(to: url)
        }
    }

    func rotateIfNeeded(into readyDir: URL) {
        guard Date().timeIntervalSince(openedAt) > 3600,
              FileManager.default.fileExists(atPath: url.path) else { return }
        let stamp = ISO8601DateFormatter.compact.string(from: Date())
        try? FileManager.default.moveItem(at: url, to: readyDir.appendingPathComponent("log_\(stamp).log"))
        openedAt = Date()
    }
}
