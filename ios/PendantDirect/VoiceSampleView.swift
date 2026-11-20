import SwiftUI

/// "Record a voice sample": pick or type a name, read eight sentences near the pendant, tap Done.
/// The pendant and the phone's microphone both record. The app sends the name, the pendant and
/// the start and end times to the server, which waits for the pendant's audio, checks it is
/// one voice with enough speech, and adds it to the person's voiceprint (samples tagged
/// `pendant`). The phone's own recording is uploaded when Done is tapped and checked at once
/// (samples tagged `phone`), so that result shows right away. If microphone permission is
/// denied the pendant alone records. The people picker is the shared LifeOS-first one.
struct VoiceSampleView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var pm = PendantManager.shared
    @State private var flow = VoiceSampleFlow()
    @State private var roster: SpeakerRoster?
    @State private var typed = ""
    @State private var submit = 0
    @State private var recorder = PhoneRecorder()
    /// The phone's recording while the pendant window is being sent (kept for "Send again").
    @State private var phoneAudio: Data?
    @State private var micDenied = false
    @State private var sendProblem: FeedProblem?
    @State private var replaceProblem: String?
    @State private var now = Date()
    @FocusState private var typing: Bool

    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    /// The pendant that heard audio most recently, else any connected one.
    private var device: String? { pm.activeDevice ?? pm.connectedDevices.sorted().first }

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.night.ignoresSafeArea()
                content
            }
            .navigationTitle("Voice sample")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Close") { dismiss() } }
                if ScreenTour.samples, case .name = flow.phase {   // the tour's inert "different voice" result
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Sample result") { flow = .tourReplaceSample(name: "Riley Park") }
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
        .tint(Theme.teal)
        .task { roster = try? await Feed.fetch("speakers", as: SpeakerRoster.self) }
        .onReceive(tick) { now = $0 }
        .onChange(of: flow.phase) { _, phase in
            if case .reading = phase { UIApplication.shared.isIdleTimerDisabled = true }
            else { UIApplication.shared.isIdleTimerDisabled = false }
        }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
            if case .reading = flow.phase { recorder.discard() }     // closed mid-reading: nothing is kept
        }
        .task(id: flow.phase) { await pollIfWaiting() }
    }

    @ViewBuilder private var content: some View {
        switch flow.phase {
        case .name:
            namePicker
        case .ready, .tooShort:
            reader(reading: false)
        case .reading:
            reader(reading: true)
        case .sending:
            status(icon: nil, title: "Sending…", detail: "")
        case .sendFailed:
            VStack(spacing: 14) {
                status(icon: "wifi.exclamationmark", title: sendProblem?.message ?? "Couldn't reach \(AppSettings.serverName)",
                       detail: "Your reading is kept; send it again when you're connected.")
                Button("Send again") { Task { await send(flow.retrySend()) } }
                    .buttonStyle(.primary(.large))
            }
        case .waiting:
            VStack(spacing: 14) {
                if flow.anyAdded {
                    status(icon: "checkmark.seal.fill", title: "\(flow.name)'s voice was added", detail: resultDetail)
                } else {
                    status(icon: nil, title: flow.phone == .uploading ? "Checking your recording…" : "Waiting for the pendant's audio",
                           detail: resultDetail.isEmpty
                               ? "This can take about five minutes, longer if the pendant is out of the phone's range. You can close this; the voice is added once the audio arrives."
                               : resultDetail)
                }
                replacePrompt
                phoneRetry
                Button("Close") { dismiss() }.buttonStyle(flow.anyAdded ? .primary(.large) : .secondary(.large))
            }
        case .added:
            VStack(spacing: 14) {
                status(icon: "checkmark.seal.fill", title: "\(flow.name)'s voice was added",
                       detail: resultDetail + "\nTheir voice is recognised from the next conversation.")
                Button("Done") { dismiss() }.buttonStyle(.primary(.large))
            }
        case .rejected:
            VStack(spacing: 14) {
                if flow.anyAdded {
                    status(icon: "checkmark.seal.fill", title: "\(flow.name)'s voice was added", detail: resultDetail)
                    Button("Done") { dismiss() }.buttonStyle(.primary(.large))
                } else {
                    status(icon: "exclamationmark.triangle.fill", title: "Not added", detail: resultDetail)
                    replacePrompt
                    Button("Try again") { flow.tryAgain() }
                        .buttonStyle(flow.replaceQuestion == nil ? .primary(.large) : .secondary(.large))
                }
            }
        }
    }

    /// The phone's result and the pendant's, one line each.
    private var resultDetail: String {
        let replaced = flow.replaced ? "What was saved for \(flow.name) before was replaced by this reading (and backed up)." : nil
        return [flow.phoneLine, flow.pendantLine, replaced].compactMap { $0 }.joined(separator: "\n")
    }

    /// "This reading doesn't sound like the 19 samples saved for Riley…" with a one-tap Replace.
    @ViewBuilder private var replacePrompt: some View {
        if let question = flow.replaceQuestion {
            VStack(spacing: 10) {
                Text(question).font(TypeScale.subheadline).foregroundStyle(Theme.textPrimary)
                    .multilineTextAlignment(.center)
                Button { Task { await replaceSaved() } } label: {
                    if flow.replacing { ProgressView() }
                    else { Label("Replace \(flow.name)'s saved voice", systemImage: "arrow.triangle.2.circlepath") }
                }
                .buttonStyle(.primary(.large))
                .disabled(flow.replacing)
                if let replaceProblem {
                    Text(replaceProblem).font(TypeScale.caption).foregroundStyle(Theme.warning)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal)
        }
    }

    @ViewBuilder private var phoneRetry: some View {
        if flow.phone == .failed, phoneAudio != nil, case .waiting(let id) = flow.phase {
            Button("Send the phone recording again") { Task { await uploadPhone(id: id) } }
                .buttonStyle(.secondary(.medium))
        }
    }

    // MARK: name

    private var namePicker: some View {
        List {
            Section {
                NameField(text: $typed, placeholder: "Search for a name")
                    .focused($typing)
                    .onSubmit { submit += 1 }
            } header: { SectionLabel("Search for a person") }
            .surfaceRows()
            Section {
                SuggestionRows(query: typed, fallback: known, submitTick: submit) { s in flow.choose(name: s.label, entityId: s.entityId) }
            } header: { SectionLabel("Known voices, then people from LifeOS") } footer: {
                SectionLabel("Reading the sentences teaches the voice, so only record someone who agrees to it.")
            }
            .surfaceRows()
        }
        .scrollContentBackground(.hidden)
    }

    private var known: [String] {
        var seen = Set<String>(), out: [String] = []
        for n in (roster?.enrolled.map(\.name) ?? []) + (roster?.allowed ?? []) where seen.insert(n.lowercased()).inserted {
            out.append(n)
        }
        return out
    }

    // MARK: reading

    private func reader(reading: Bool) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Label(flow.name, systemImage: "person.wave.2").font(TypeScale.headline)
                        .foregroundStyle(Theme.textPrimary)
                    Spacer()
                    if !reading { Button("Change") { flow.changeName() }.buttonStyle(.tertiary(.small)) }
                }
                if case .tooShort(let s) = flow.phase {
                    Label("That was only \(s) s. Read all eight sentences, then tap Done.", systemImage: "hourglass")
                        .font(TypeScale.subheadline).foregroundStyle(Theme.warning)
                }
                Text(reading ? "Read the sentences below out loud at a normal pace, about an arm's length from the pendant."
                             : "Hold the pendant about an arm's length away and tap Start. The pendant and this phone's microphone both listen.")
                    .font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary)
                if let d = device {
                    Label(flow.phone == .recording ? "Listening with \(PendantSnapshot.displayName(d)) and this phone"
                                                   : "Listening with \(PendantSnapshot.displayName(d))",
                          systemImage: "circle.circle")
                        .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                    if micDenied && !reading {
                        Label("Microphone is off for this app, so only the pendant records. Turn it on in Settings to also use the phone.",
                              systemImage: "mic.slash")
                            .font(TypeScale.caption).foregroundStyle(Theme.warning)
                    }
                } else if !reading {
                    Label("No pendant is connected. Wake it and keep it near the phone.", systemImage: "exclamationmark.circle")
                        .font(TypeScale.caption).foregroundStyle(Theme.warning)
                }
                ForEach(Array(VoiceSampleScript.sentences.enumerated()), id: \.offset) { i, line in
                    Card {
                        HStack(alignment: .top, spacing: 10) {
                            Text("\(i + 1)").font(.callout.monospacedDigit().weight(.bold)).foregroundStyle(Theme.teal)
                            Text(line).font(.title3).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                if reading {
                    Text("\(Int(flow.elapsed(now: now))) s").font(.title2.monospacedDigit())
                        .foregroundStyle(Theme.textPrimary).frame(maxWidth: .infinity)
                    Button { Task { await finishReading() } } label: {
                        Label("Done reading", systemImage: "stop.circle.fill")
                    }
                    .buttonStyle(.primary(.large))
                } else {
                    Button {
                        Task { await startReading() }
                    } label: {
                        Label("Start", systemImage: "play.circle.fill")
                    }
                    .buttonStyle(.primary(.large))
                    .disabled(device == nil)
                }
            }
            .padding()
        }
    }

    private func status(icon: String?, title: String, detail: String) -> some View {
        VStack(spacing: 12) {
            if let icon { Image(systemName: icon).font(.system(size: 48)).foregroundStyle(Theme.teal) }
            else { ProgressView() }
            Text(title).font(TypeScale.title3).foregroundStyle(Theme.textPrimary).multilineTextAlignment(.center)
            if !detail.isEmpty {
                Text(detail).font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
            }
        }
        .padding()
    }

    // MARK: talking to the server

    private func startReading() async {
        guard let device else { return }
        let allowed = await PhoneRecorder.requestPermission()
        micDenied = !allowed
        let recording = allowed && recorder.start()
        if !flow.start(now: Date(), device: device, phoneRecording: recording), recording { recorder.discard() }
    }

    private func finishReading() async {
        let request = flow.done(now: Date())
        if request == nil { recorder.discard() }       // too short: nothing to send
        else { phoneAudio = recorder.stop() }
        if request != nil, phoneAudio == nil { flow.phoneFailed() }
        await send(request)
    }

    private func send(_ request: EnrollRequest?) async {
        guard let request else { return }
        sendProblem = nil
        do {
            let s = try await Feed.send(EnrollRequest.path, json: request.body, as: EnrollStatus.self)
            flow.sent(s)
            if s.id > 0 { await uploadPhone(id: s.id) }
        } catch let e as Feed.HTTPStatus where e.code == 409 {
            flow.sent(EnrollStatus(id: 0, status: "rejected", reason: "overlaps another voice sample"))
            dropPhone()
        } catch let e as Feed.HTTPStatus where e.code == 400 {   // an invalid name or window: retrying can't help
            flow.sent(EnrollStatus(id: 0, status: "rejected", reason: "bad request"))
            dropPhone()
        } catch {
            sendProblem = FeedProblem(error)
            flow.sendFailed()
        }
    }

    /// Uploads the phone recording for reading `id` and shows its verdict at once.
    private func uploadPhone(id: Int) async {
        guard let audio = phoneAudio else { return }
        flow.retryPhone()
        do {
            let s = try await Feed.upload(VoiceSampleScript.phonePath(id: id), body: audio, type: "audio/wav", as: EnrollStatus.self)
            flow.phoneSent(s)
            dropPhone()
        } catch let e as Feed.HTTPStatus where (400..<500).contains(e.code) {   // unusable audio: sending it again can't help
            flow.phoneFailed()
            dropPhone()
        } catch {
            flow.phoneFailed()                     // kept: "Send the phone recording again"
        }
    }

    /// "Yes, this is them": the server swaps what's saved under the name for this reading.
    private func replaceSaved() async {
        guard let id = flow.startReplace() else { return }
        replaceProblem = nil
        do {
            let s = try await Feed.send(VoiceSampleScript.replacePath(id: id), json: [:], as: EnrollStatus.self)
            flow.replaceFinished(s)
        } catch {
            flow.replaceFinished(nil)
            replaceProblem = FeedProblem(error).message
        }
    }

    private func dropPhone() {
        phoneAudio = nil
        recorder.finish()
    }

    /// While the verdict is pending, asks every 10 s (up to 3 hours: the pendant may be out of range).
    private func pollIfWaiting() async {
        guard case .waiting(let id) = flow.phase else { return }
        for _ in 0..<1080 {
            try? await Task.sleep(for: .seconds(10))
            if Task.isCancelled { return }
            if let s = try? await Feed.fetch("enroll/\(id)", as: EnrollStatus.self, cached: false), s.outcome != .waiting {
                flow.apply(s)
                return
            }
        }
    }
}

/// The "Record a voice sample" button shared by the People view and Voice Scoring.
struct VoiceSampleButton: View {
    @State private var showing = false
    var body: some View {
        Button { showing = true } label: {
            Label("Record a voice sample", systemImage: "waveform.badge.mic")
        }
        .sheet(isPresented: $showing) { VoiceSampleView() }
    }
}
