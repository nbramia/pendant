import AVFoundation
import MediaPlayer
import SwiftUI

/// Plays one conversation's audio from the feed (an AAC/M4A transcode of the Ogg/Opus clip,
/// which AVPlayer can't decode). The token goes in a header on every range request AVPlayer makes.
@MainActor
final class ConversationPlayer: ObservableObject {
    enum State: Equatable { case idle, loading, ready, missing, failed }

    @Published private(set) var state: State = .idle
    @Published private(set) var isPlaying = false
    @Published private(set) var time: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var speed: Float = 1

    private var player: AVPlayer?
    private var observer: Any?
    private var endObserver: NSObjectProtocol?
    private var title = ""

    deinit {
        if let observer, let player { player.removeTimeObserver(observer) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
    }

    /// Why `prepare` gave up.
    enum PrepareError: Error {
        case missing, failed, timedOut
        var message: String {
            switch self {
            case .missing: "Audio no longer kept"
            case .failed: "Couldn't load the audio"
            case .timedOut: "The audio took too long to load"
            }
        }
    }

    private var source: (device: String, startMs: Int, knownDuration: Double)?
    private var loadTask: Task<Void, Never>?

    /// Loads the audio. A call while a load is running waits for that one; after a failure it loads again.
    func load(device: String, startMs: Int, title: String, knownDuration: Double) async {
        source = (device, startMs, knownDuration)
        self.title = title
        await startLoad()?.value
    }

    /// The running load, or a new one when there is none and the audio isn't loaded yet.
    private func startLoad() -> Task<Void, Never>? {
        if let loadTask { return loadTask }
        guard state == .idle || state == .failed, let s = source else { return nil }
        let task = Task {
            await self.fetch(device: s.device, startMs: s.startMs, knownDuration: s.knownDuration)
            self.loadTask = nil
        }
        loadTask = task
        return task
    }

    /// Waits until the audio can play from any position: loaded (loading again after a failure) and
    /// the player item `readyToPlay`. Throws after `timeout` seconds, or when the clip is gone or
    /// can't be loaded. A load still running at the timeout carries on for the next tap.
    func prepare(timeout: Double) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        if state != .ready {
            _ = startLoad()
            while loadTask != nil {
                guard Date() < deadline else { throw PrepareError.timedOut }
                try await Task.sleep(for: .milliseconds(100))
            }
        }
        switch state {
        case .ready: break
        case .missing: throw PrepareError.missing
        default: throw PrepareError.failed
        }
        guard let item = player?.currentItem else { throw PrepareError.failed }
        // A seek sent before the item is readyToPlay is dropped, which made line taps hit or miss.
        while item.status != .readyToPlay {
            if item.status == .failed {
                PendantManager.shared.log("player: item failed (\(item.error?.localizedDescription ?? "-"))")
                throw PrepareError.failed
            }
            guard Date() < deadline else { throw PrepareError.timedOut }
            try await Task.sleep(for: .milliseconds(100))
        }
    }

    private func fetch(device: String, startMs: Int, knownDuration: Double) async {
        guard let url = Feed.url(Playback.audioPath(device: device, startMs: startMs)) else { state = .failed; return }
        state = .loading
        // Ask for one byte first: 404 means the clip was purged; a 206 means it is there (and transcoded).
        var probe = URLRequest(url: url)
        probe.setValue("Bearer \(Uploader.token)", forHTTPHeaderField: "Authorization")
        probe.setValue("bytes=0-0", forHTTPHeaderField: "Range")
        probe.timeoutInterval = 120
        do {
            let (_, resp) = try await URLSession.shared.data(for: probe)
            switch (resp as? HTTPURLResponse)?.statusCode {
            case 200, 206: break
            case 404: state = .missing; return
            default: throw URLError(.badServerResponse)
            }
        } catch {
            PendantManager.shared.log("player: probe failed (\(error.localizedDescription))")
            state = .failed
            return
        }
        // a reload after a failure replaces the old player
        if let observer, let player { player.removeTimeObserver(observer) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        observer = nil
        endObserver = nil
        let asset = AVURLAsset(url: url, options: ["AVURLAssetHTTPHeaderFieldsKey": ["Authorization": "Bearer \(Uploader.token)"]])
        let item = AVPlayerItem(asset: asset)
        let p = AVPlayer(playerItem: item)
        p.automaticallyWaitsToMinimizeStalling = true
        player = p
        duration = knownDuration
        observer = p.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] t in
            MainActor.assumeIsolated {
                guard let self, t.isNumeric else { return }
                self.time = t.seconds
                self.updateNowPlaying()
            }
        }
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.isPlaying = false; self?.updateNowPlaying() }
        }
        if let d = try? await asset.load(.duration), d.isNumeric, d.seconds > 0 { duration = d.seconds }
        state = .ready
        PendantManager.shared.log("player: ready \(device)-\(startMs), \(Int(duration)) s")
    }

    /// Starts playing at the current position; at the very end it starts over, unless `rewindAtEnd` is false.
    func play(rewindAtEnd: Bool = true) {
        guard let player, state == .ready else { return }
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            PendantManager.shared.log("player: audio session failed (\(error.localizedDescription))")
        }
        if rewindAtEnd, duration > 0, time >= duration - 0.5 { seek(to: 0) }
        player.rate = speed
        isPlaying = true
        enableRemoteControls()
        updateNowPlaying()
        PendantManager.shared.log("player: play at \(Int(time)) s, \(Playback.speedLabel(speed))")
    }

    func pause() {
        player?.pause()
        isPlaying = false
        updateNowPlaying()
    }

    func toggle() { isPlaying ? pause() : play() }

    func seek(to seconds: Double) {
        let t = min(max(0, seconds), duration > 0 ? duration : seconds)
        time = t
        player?.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
        updateNowPlaying()
    }

    /// Moves to exactly `seconds` and returns once the player is there (false if a newer seek replaced it).
    func seekExactly(to seconds: Double) async -> Bool {
        let t = min(max(0, seconds), duration > 0 ? duration : seconds)
        time = t
        guard let player else { return false }
        let done = await player.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
        time = t
        updateNowPlaying()
        return done
    }

    func cycleSpeed() {
        speed = Playback.nextSpeed(after: speed)
        if isPlaying { player?.rate = speed }
        updateNowPlaying()
    }

    /// Leaves the screen: stop and release the audio session so other audio can resume.
    func stop() {
        pause()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        let center = MPRemoteCommandCenter.shared()
        for c in [center.playCommand, center.pauseCommand, center.changePlaybackPositionCommand] { c.removeTarget(nil) }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    private func enableRemoteControls() {
        let c = MPRemoteCommandCenter.shared()
        for cmd in [c.playCommand, c.pauseCommand, c.changePlaybackPositionCommand] { cmd.removeTarget(nil) }
        c.playCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.play() }; return .success }
        c.pauseCommand.addTarget { [weak self] _ in MainActor.assumeIsolated { self?.pause() }; return .success }
        c.changePlaybackPositionCommand.addTarget { [weak self] e in
            guard let e = e as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            MainActor.assumeIsolated { self?.seek(to: e.positionTime) }
            return .success
        }
    }

    private func updateNowPlaying() {
        guard state == .ready else { return }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [
            MPMediaItemPropertyTitle: title,
            MPMediaItemPropertyPlaybackDuration: duration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: time,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? Double(speed) : 0,
        ]
    }
}

/// Play/pause, scrubber with elapsed/total time, speed and follow toggles; or why there is no audio.
struct ConversationPlayerBar: View {
    @ObservedObject var player: ConversationPlayer
    @Binding var follow: Bool
    @State private var scrubbing = false
    @State private var scrubTime: Double = 0

    var body: some View {
        switch player.state {
        case .idle, .loading:
            Card { Label("Loading audio…", systemImage: "waveform").font(TypeScale.callout).foregroundStyle(Theme.textSecondary) }
        case .missing:
            Card { Label("Audio no longer kept", systemImage: "waveform.slash").font(TypeScale.callout).foregroundStyle(Theme.textSecondary) }
        case .failed:
            Card { Label("Can't load the audio right now", systemImage: "wifi.exclamationmark")
                .font(TypeScale.callout).foregroundStyle(Theme.textSecondary) }
        case .ready:
            PlayerCard(isPlaying: player.isPlaying, time: scrubbing ? scrubTime : player.time, duration: player.duration,
                       speed: Playback.speedLabel(player.speed), follow: follow,
                       onToggle: { player.toggle() }, onSpeed: { player.cycleSpeed() }, onFollow: { follow.toggle() }) {
                Slider(value: Binding(get: { scrubbing ? scrubTime : player.time },
                                      set: { scrubTime = $0 }),
                       in: 0...max(player.duration, 1)) { editing in
                    scrubbing = editing
                    if !editing { player.seek(to: scrubTime) }
                }
                .tint(Theme.teal)
            }
        }
    }
}
