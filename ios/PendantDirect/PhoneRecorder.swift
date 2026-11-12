import AVFoundation

/// Records the phone's microphone while someone reads the voice-sample sentences: 16 kHz mono
/// 16-bit WAV (about 2 MB a minute), stopped by itself after 60 s. The file is deleted once the
/// upload has been answered.
@MainActor
final class PhoneRecorder {
    private var recorder: AVAudioRecorder?
    private var url: URL?

    /// Asks for microphone permission the first time; false when it is (or was) denied.
    static func requestPermission() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return true
        case .denied: return false
        default: return await AVAudioApplication.requestRecordPermission()
        }
    }

    /// Starts recording. False when the session or recorder couldn't start (the reading goes on
    /// with the pendant alone).
    func start() -> Bool {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("voice-sample-\(UUID().uuidString).wav")
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000, AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
        ]
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers])
            try session.setActive(true)
            let r = try AVAudioRecorder(url: file, settings: settings)
            guard r.record(forDuration: VoiceSampleScript.phoneMaxSeconds) else { return false }
            recorder = r
            url = file
            return true
        } catch {
            return false
        }
    }

    /// Stops and returns the recording, or nil when nothing was recorded.
    func stop() -> Data? {
        recorder?.stop()
        recorder = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        guard let url, let data = try? Data(contentsOf: url), data.count > 44 else { return nil }
        return data
    }

    /// Stops and throws the recording away (a cancelled or too-short reading).
    func discard() {
        recorder?.stop()
        recorder = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        if let url { try? FileManager.default.removeItem(at: url) }
        url = nil
    }

    /// Deletes the file after its upload was answered.
    func finish() {
        if let url { try? FileManager.default.removeItem(at: url) }
        url = nil
    }
}
