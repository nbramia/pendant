import CoreBluetooth
import Foundation

/// One connected pendant: its GATT link, write queue, live/drain mode, acks
/// and spool lane. The manager keeps one session per paired pendant and keeps
/// every pendant connected at once, so whichever one the wearer is wearing streams
/// while the others drain and idle.
@MainActor
final class PendantSession: NSObject {
    private(set) var peripheral: CBPeripheral
    private unowned let manager: PendantManager
    let store: CaptureStore

    private(set) var writeChar: CBCharacteristic?
    private var writeType: CBCharacteristicWriteType = .withResponse
    private let reassembler = Limitless.Reassembler()
    private var outgoingIndex: UInt32 = 1
    private var writeQueue: [Data] = []
    private var writeInFlight = false

    enum Mode { case idle, draining(target: Int64, started: Date, startPages: Int), live }
    private(set) var mode: Mode = .idle
    private var acks = AckTracker()
    private var pagesSinceAck = 0
    private var drainReceived = 0
    private var drainFirstIndex = Int64.max
    private var drainStored = 0
    /// When the snapshot's lastAudio was last written (rate-limits snapshot saves).
    private var snapshotAudioAt = Date.distantPast
    private var lastPageAt = Date()
    private(set) var lastAudioAt: Date?
    private var lastDrainEnded = Date.distantPast
    private var storageInstance: UInt64 = 0
    private var pendingStatus: Limitless.StorageState?
    private(set) var storage: Limitless.StorageState?
    private(set) var battery: Int64?

    init(peripheral: CBPeripheral, manager: PendantManager) {
        self.peripheral = peripheral
        self.manager = manager
        self.store = CaptureStore(lane: peripheral.identifier.uuidString)
        super.init()
        peripheral.delegate = self
    }

    /// Pendant hex ID, learned from GetDeviceInfo and remembered per peripheral.
    var deviceID: String? { manager.deviceID(for: peripheral.identifier) }
    var isReady: Bool { writeChar != nil && peripheral.state == .connected }
    var label: String { deviceID.map(PendantSnapshot.displayName) ?? "pendant \(peripheral.identifier.uuidString.prefix(4))" }

    private func log(_ s: String) { manager.log("[\(deviceID.map { PendantSnapshot.knownNames[$0] ?? String($0.suffix(4)) } ?? "?")] \(s)") }

    // MARK: Lifecycle

    /// iOS can hand back a new CBPeripheral object for the same pendant
    /// (e.g. after state restoration). Keep one session, and its spool lane.
    func rebind(_ p: CBPeripheral) {
        peripheral = p
        p.delegate = self
        writeChar = nil
    }

    func didConnect() {
        reassembler.reset()
        writeQueue.removeAll()
        writeInFlight = false
        writeChar = nil
        mode = .idle
        peripheral.discoverServices([Limitless.service])
    }

    func didDisconnect() {
        if case .draining = mode { finishDrain(reason: "disconnected") }
        store.seal()
        writeChar = nil
        mode = .idle
    }

    /// Restored by iOS with the link still up: rebuild GATT handles.
    func rediscoverIfNeeded() {
        if peripheral.state == .connected && writeChar == nil {
            log("already connected; rediscovering")
            peripheral.discoverServices([Limitless.service])
        }
    }

    private func didBecomeReady() {
        mode = .idle
        pendingStatus = nil
        acks.reset()
        pagesSinceAck = 0
        send(Limitless.setCurrentTime())
        send(Limitless.getDeviceInfo())
        send(Limitless.getDeviceStatus())
        manager.sessionsChanged()
    }

    // MARK: Writes (fragmented, serialized)

    private func send(_ command: [UInt8]) {
        let max = peripheral.maximumWriteValueLength(for: writeType) - 16
        writeQueue.append(contentsOf: Limitless.encodeFragments(index: outgoingIndex, payload: command, maxChunk: max))
        outgoingIndex &+= 1
        pumpWrites()
    }

    private func pumpWrites() {
        guard let ch = writeChar, peripheral.state == .connected else { return }
        while !writeQueue.isEmpty {
            if writeType == .withResponse {
                if writeInFlight { return }
                writeInFlight = true
                peripheral.writeValue(writeQueue.removeFirst(), for: ch, type: .withResponse)
                return
            }
            guard peripheral.canSendWriteWithoutResponse else { return }
            peripheral.writeValue(writeQueue.removeFirst(), for: ch, type: .withoutResponse)
        }
    }

    // MARK: Inbound

    private func handle(_ payload: [UInt8]) {
        guard let msg = Limitless.decode(payload) else { log("undecodable message (\(payload.count) B)"); return }
        switch msg {
        case .storage(let sb):
            handleStorage(sb, raw: payload)
        case .status(let st, let soc, let syncMode, let rec):
            if let st { storage = st }
            if let soc { battery = soc }
            manager.updateSnapshot(self) { s in
                if let soc { s.battery = Int(soc) }
                if let st { s.freePages = Int(st.free); s.totalPages = Int(st.total) }
            }
            log("status: oldest=\(st?.oldest ?? -1) newest=\(st?.newest ?? -1) free=\(st?.free ?? -1)"
                + " sync=\(syncMode ?? 99) rec=\(rec ?? 99) batt=\(soc ?? -1)%")
            if let st { onStatus(st) }
        case .info(let info):
            let hex = String(format: "%016llX", info.deviceID)
            manager.setDeviceID(hex, for: peripheral.identifier)
            storageInstance = info.storageInstance
            if info.batteryPercent >= 0 { battery = info.batteryPercent }
            manager.updateSnapshot(self) { s in
                s.firmware = info.firmware
                if info.batteryPercent >= 0 { s.battery = Int(info.batteryPercent) }
            }
            log("info: id=\(hex) fw=\(info.firmware)")
            if let st = pendingStatus {
                pendingStatus = nil
                onStatus(st)
            }
        case .timeSet(let resets):
            log("time set ack (invalid RTC resets: \(resets ?? 0))")
        case .battery(let soc):
            if let soc { battery = soc; manager.updateSnapshot(self) { $0.battery = Int(soc) } }
        case .response(let command, let id, let category, let error):
            if (category ?? 0) != 0 || error != nil {
                log("command \(command.map(String.init) ?? "?") (req \(id)) failed: \(category ?? 0) \(error ?? "")")
            } else if command == 7 {
                log("ack accepted (req \(id))")
            }
        case .other(let field):
            log("message field \(field) (\(payload.count) B)")
        }
    }

    /// Our own last ack for this pendant's storage instance (a wipe starts a new
    /// instance). The pendant's reported `oldest` can lag our acks.
    private var watermarkKey: String { "ackWatermark.\(deviceID ?? "unknown")" }
    private var localWatermark: Int64 {
        get {
            let parts = (UserDefaults.standard.string(forKey: watermarkKey) ?? "").split(separator: ":")
            guard parts.count == 2, UInt64(parts[0]) == storageInstance, let v = Int64(parts[1]) else { return -1 }
            return v
        }
        set { UserDefaults.standard.set("\(storageInstance):\(newValue)", forKey: watermarkKey) }
    }

    private func onStatus(_ st: Limitless.StorageState) {
        guard deviceID != nil else {
            pendingStatus = st
            send(Limitless.getDeviceInfo())
            return
        }
        acks.applyStatus(oldest: st.oldest, newest: st.newest, localWatermark: localWatermark)
        if case .draining = mode { return }
        // The pendant pushes status every few seconds; drain at most once a
        // minute unless a real backlog has built up (e.g. after being away).
        let backlog = st.newest - 1 - acks.ackedThrough
        let rested = Date().timeIntervalSince(lastDrainEnded) > 60
        if acks.needsDrain(newest: st.newest) && (rested || backlog > 50) {
            startDrain(target: st.newest - 1)
        } else if case .idle = mode {
            applyStreamingMode()
        }
    }

    private func handleStorage(_ sb: Limitless.StorageBuffer, raw: [UInt8]) {
        guard sb.ingestType == .batch || sb.ingestType == .realtime else { return }
        lastPageAt = Date()
        if sb.ingestType == .batch { drainReceived += 1; drainFirstIndex = min(drainFirstIndex, sb.index) }
        if acks.shouldStore(sb.index) {
            do {
                try store.append(raw, device: deviceID ?? "unknown")
            } catch {
                log("spool write failed: \(error.localizedDescription)")
                return
            }
            acks.didStore(sb.index, batch: sb.ingestType == .batch)
            if sb.ingestType == .batch {
                drainStored += 1
                manager.batchPages += 1
                pagesSinceAck += 1
                if pagesSinceAck >= 25 { syncAndAck() }
            } else {
                manager.livePages += 1
            }
            noteSound(sb)
        } else if sb.ingestType == .batch {
            acks.didSeeStoredBatchPage(sb.index)
        }
        if case .draining(let target, _, _) = mode, sb.ingestType == .batch, sb.index >= target {
            finishDrain(reason: "reached target")
        }
    }

    /// "Listening" means sound recorded in the last two minutes: a switched-off pendant
    /// keeps sending status-only pages, and a drain delivers sound from earlier.
    private func noteSound(_ sb: Limitless.StorageBuffer) {
        let now = Date()
        guard sb.isRecentSound(now: now) else { return }
        let first = lastAudioAt.map { now.timeIntervalSince($0) >= 120 } ?? true
        lastAudioAt = now
        if now.timeIntervalSince(snapshotAudioAt) > 30 {
            snapshotAudioAt = now
            manager.updateSnapshot(self) { $0.lastAudio = now }
        }
        if first { manager.sessionsChanged() }
    }

    /// fsync, then ack through the highest batch page stored (see AckTracker).
    private func syncAndAck() {
        pagesSinceAck = 0
        guard store.sync() else {
            log("fsync failed; not acking")
            return
        }
        if let upTo = acks.commit() {
            send(Limitless.deleteFlashPages(upTo: upTo))
            localWatermark = upTo
            log("ack sent through page \(upTo)")
        }
        manager.refreshSpool()
    }

    // MARK: Modes

    private func startDrain(target: Int64) {
        mode = .draining(target: target, started: Date(), startPages: drainStored)
        lastPageAt = Date()
        drainReceived = 0
        drainFirstIndex = .max
        log("drain start: acked=\(acks.ackedThrough) target=\(target)")
        send(Limitless.downloadFlashPages(batch: true, realtime: false))
        manager.sessionsChanged()
    }

    private func finishDrain(reason: String) {
        guard case .draining(_, let started, let startPages) = mode else { return }
        syncAndAck()
        let pages = drainStored - startPages
        let secs = Date().timeIntervalSince(started)
        if pages > 0 && secs > 0 { manager.lastDrainRate = Double(pages) / secs }
        log(String(format: "drain done (%@): received %d from page %lld, stored %d new, %.0fs",
                   reason, drainReceived, drainFirstIndex == .max ? -1 : drainFirstIndex, pages, secs))
        mode = .idle
        lastDrainEnded = Date()
        store.seal()
        manager.uploader.kick()
        applyStreamingMode()
    }

    func applyStreamingMode() {
        guard writeChar != nil else { return }
        if case .draining = mode { return }
        send(Limitless.downloadFlashPages(batch: false, realtime: manager.liveEnabled))
        mode = manager.liveEnabled ? .live : .idle
        manager.sessionsChanged()
    }

    func periodic() {
        guard isReady else { return }
        if case .draining = mode, Date().timeIntervalSince(lastPageAt) > 30 {
            finishDrain(reason: "stalled 30s")
        }
        syncAndAck()
        if case .live = mode {
            store.seal()
            // The pendant stops streaming after a while without a refresh (per Omi).
            send(Limitless.downloadFlashPages(batch: false, realtime: true))
        }
        send(Limitless.getDeviceStatus())
    }
}

// MARK: - CBPeripheralDelegate

extension PendantSession: CBPeripheralDelegate {
    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        MainActor.assumeIsolated {
            guard let svc = peripheral.services?.first(where: { $0.uuid == Limitless.service }) else {
                log("pendant service missing: \(error?.localizedDescription ?? "")")
                return
            }
            peripheral.discoverCharacteristics([Limitless.writeChar, Limitless.notifyChar], for: svc)
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService,
                                error: Error?) {
        MainActor.assumeIsolated {
            for ch in service.characteristics ?? [] {
                if ch.uuid == Limitless.writeChar {
                    writeChar = ch
                    writeType = ch.properties.contains(.write) ? .withResponse : .withoutResponse
                } else if ch.uuid == Limitless.notifyChar {
                    // Touching the encrypted characteristic makes iOS pair if needed.
                    peripheral.setNotifyValue(true, for: ch)
                }
            }
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                                error: Error?) {
        MainActor.assumeIsolated {
            if let error {
                log("notify subscribe failed: \(error.localizedDescription)")
                return
            }
            if characteristic.uuid == Limitless.notifyChar && characteristic.isNotifying {
                log("subscribed; handshake")
                didBecomeReady()
            }
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic,
                                error: Error?) {
        MainActor.assumeIsolated {
            guard let data = characteristic.value, let frag = Limitless.decodeFragment(data) else { return }
            if let full = reassembler.add(frag) { handle(full) }
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic,
                                error: Error?) {
        MainActor.assumeIsolated {
            writeInFlight = false
            if let error { log("write failed: \(error.localizedDescription)") }
            pumpWrites()
        }
    }

    nonisolated func peripheralIsReady(toSendWriteWithoutResponse peripheral: CBPeripheral) {
        MainActor.assumeIsolated { pumpWrites() }
    }
}
