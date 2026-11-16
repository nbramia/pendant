import AVFoundation
import SwiftUI

/// Voice Scoring: a full-screen stack of voice cards, least confident first.
/// Each card plays its clip as it comes up. Swipe right = yes (the guess),
/// left = no (pick who it is), up = skip. A card with a likely person (the calendar's, else LifeOS's
/// maybe) shows it as one "<name>?" button that saves on the first tap; once a voice is named, the
/// other cards from that conversation stop offering that person. Every answer goes to the server
/// (`/pendant/feed/speakers/*`), which updates voiceprints and transcripts.
/// Opened from Today and the Pendants tab; it never notifies.
struct VoiceScoringView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var deck = VoiceDeck()
    @State private var roster: SpeakerRoster?
    @State private var loaded = false
    @State private var loadFailed: FeedProblem?
    @State private var drag: CGSize = .zero
    @State private var flyOut: CGSize = .zero
    @State private var player: AVAudioPlayer?
    @State private var playing = false
    @State private var picking = false
    @State private var tourStep = 0

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.night.ignoresSafeArea()
                content
            }
            .navigationTitle("Voice Scoring")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { player?.stop(); dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    VoiceSampleButton().labelStyle(.iconOnly)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if deck.done > 0 {
                        Text("\(deck.done) scored").font(.caption.monospacedDigit()).foregroundStyle(Theme.textSecondary)
                    }
                }
            }
            .sheet(isPresented: $picking, onDismiss: { deck.cancelPick() }) {
                if let card = deck.current {
                    VoicePicker(card: card, people: people(excluding: card.guess)) { answer in
                        picking = false
                        run(deck.choose(answer))
                    }
                    .presentationDetents([.medium, .large])
                }
            }
        }
        .preferredColorScheme(.dark)
        .tint(Theme.teal)
        .task { await load() }
        .retryWhileFailed(loadFailed) { await load() }
        .task(id: deck.current?.id) {
            if let c = deck.current { await play(c) }
        }
        .onDisappear { player?.stop() }
    }

    @ViewBuilder private var content: some View {
        if ScreenTour.samples, loaded, tourStep < tourSamples.count {
            // screen tour only (debug builds): inert cards ahead of the real ones, so the guess button can
            // be reviewed and tapped (the calendar's guess, LifeOS's maybe, then a card with no guess)
            let sample = tourSamples[tourStep]
            VStack(spacing: 20) {
                VoiceCardView(card: sample, playing: false, remaining: tourSamples.count - tourStep)
                    .frame(maxHeight: .infinity)
                VoiceScoringActions(guess: sample.guess, playing: false, onNo: {}, onYes: {}, onReplay: {},
                                    onSkip: { withAnimation(.snappy) { tourStep += 1 } },
                                    oneTap: sample.oneTap,
                                    onGuess: { _ in withAnimation(.snappy) { tourStep += 1 } })
            }
            .padding()
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("voice-scoring-sample")
        } else if let card = deck.current {
            VStack(spacing: 20) {
                ZStack {
                    ForEach(Array(deck.cards.prefix(3).enumerated().reversed()), id: \.element.id) { i, c in
                        stackedCard(i, c)
                    }
                }
                .frame(maxHeight: .infinity)
                if deck.failed {
                    Label("Couldn't reach \(AppSettings.serverName). Try again.", systemImage: "wifi.exclamationmark")
                        .font(TypeScale.caption).foregroundStyle(Theme.warning)
                }
                actions(card)
            }
            .padding()
        } else if !loaded {
            ProgressView()
        } else {
            VStack(spacing: 12) {
                Image(systemName: loadFailed != nil ? "wifi.exclamationmark" : "checkmark.seal.fill")
                    .font(.system(size: 48)).foregroundStyle(Theme.teal)
                Text(loadFailed?.message ?? "Nothing waiting").font(TypeScale.title3)
                    .foregroundStyle(Theme.textPrimary)
                Text(loadFailed != nil ? "Checking again shortly." : "New voices show up here after conversations are transcribed.")
                    .font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center)
                Button("Check again") { Task { await load() } }
                    .buttonStyle(.secondary(.medium)).padding(.top, Spacing.s)
            }
            .padding()
        }
    }

    private func stackedCard(_ i: Int, _ c: VoiceCard) -> some View {
        let top = i == 0
        let tilt: Double = top ? Double(offset.width / 20) : 0
        return VoiceCardView(card: c, playing: top && playing, remaining: deck.cards.count)
            .scaleEffect(1 - CGFloat(i) * 0.04)
            .offset(y: CGFloat(i) * 14)
            .opacity(top ? 1 : 0.6)
            .offset(top ? offset : .zero)
            .rotationEffect(.degrees(tilt))
            .overlay(alignment: .top) { if top { swipeBadge } }
            .gesture(dragGesture)
            .allowsHitTesting(top)
            .accessibilityHidden(!top)
    }

    private var offset: CGSize { CGSize(width: drag.width + flyOut.width, height: drag.height + flyOut.height) }

    @ViewBuilder private var swipeBadge: some View {
        if let d = SwipeDirection.of(dx: drag.width, dy: drag.height, threshold: 60) {
            let (text, color) = badge(d)
            Text(text).font(.title.weight(.heavy)).foregroundStyle(color)
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: Radius.small))
                .overlay(RoundedRectangle(cornerRadius: Radius.small).stroke(color, lineWidth: 3))
                .padding(.top, 28)
        }
    }

    private func badge(_ d: SwipeDirection) -> (String, Color) {
        switch d {
        case .right: return (deck.current?.guess == nil ? "WHO?" : "YES", Theme.success)
        case .left: return ("NO", Theme.destructive)
        case .up: return ("SKIP", Theme.textSecondary)
        }
    }

    private var dragGesture: some Gesture {
        DragGesture()
            .onChanged { v in if deck.phase == .card { drag = v.translation } }
            .onEnded { v in
                guard let d = SwipeDirection.of(dx: v.translation.width, dy: v.translation.height) else {
                    withAnimation(.spring) { drag = .zero }
                    return
                }
                swipe(d)
            }
    }

    private func actions(_ card: VoiceCard) -> some View {
        VoiceScoringActions(guess: card.guess, playing: playing, busy: deck.phase != .card,
                            onNo: { swipe(.left) }, onYes: { swipe(.right) },
                            onReplay: { Task { await play(card) } }, onSkip: { swipe(.up) },
                            oneTap: card.oneTap, onGuess: { _ in run(deck.confirmGuess()) })
            .disabled(deck.phase != .card)
    }

    /// Screen tour only: the sample cards, each asking about no one or the first known name.
    private var tourSamples: [VoiceCard] {
        [VoiceCard(id: 0, excerpt: "Sample for the screen tour", currentLabel: roster?.allowed.first, confidence: 0.42,
                   device: "new", hints: .tourSample),
         VoiceCard(id: 0, excerpt: "Sample for the screen tour", confidence: 0.2, device: "new", hints: .tourSampleMaybe),
         VoiceCard(id: 0, excerpt: "Sample for the screen tour", confidence: 0.2, device: "new")]
    }

    // MARK: flow

    private func swipe(_ d: SwipeDirection) {
        let step = deck.swipe(d)
        switch step {
        case .pick:
            withAnimation(.spring) { drag = .zero }
            picking = true
        case .submit:
            run(step)
        case .none:
            withAnimation(.spring) { drag = .zero }
        }
    }

    /// Animates the card off in its direction, sends the answer, then shows the next.
    private func run(_ step: VoiceDeck.Step) {
        guard case .submit(let answer) = step, let card = deck.current else { return }
        player?.stop()
        playing = false
        let away: CGSize = switch answer {
        case .skip: CGSize(width: 0, height: -900)
        case .confirm: CGSize(width: 600, height: 0)
        default: CGSize(width: -600, height: 0)
        }
        withAnimation(.easeIn(duration: 0.25)) { flyOut = away }
        Task {
            let req = answer.request(cardID: card.id)
            let ok = (try? await Feed.post(req.path, json: req.body)) != nil
            try? await Task.sleep(for: .milliseconds(150))
            drag = .zero
            if ok {
                var t = Transaction()
                t.disablesAnimations = true
                withTransaction(t) { flyOut = .zero; deck.finished(success: true) }
                switch answer {
                case .newPerson, .suggested: await loadRoster()
                default: break
                }
                if deck.cards.count < 3 { await load() }
            } else {
                deck.finished(success: false)
                withAnimation(.spring) { flyOut = .zero }
            }
        }
    }

    private func people(excluding: String?) -> [String] {
        let enrolled = (roster?.enrolled ?? []).map {
            PickerOrder.Person(name: $0.name, recognized: $0.recognized ?? 0, count: $0.count)
        }
        return PickerOrder.names(enrolled: enrolled, allowed: roster?.allowed ?? [], excluding: excluding)
    }

    private func load() async {
        await loadRoster()
        do {
            let fresh = try await Feed.fetch("speakers/unknown", as: VoiceCards.self).speakers
            deck.load(fresh)
            loadFailed = nil
        } catch {
            loadFailed = deck.isEmpty ? FeedProblem(error) : nil
        }
        loaded = true
    }

    private func loadRoster() async {
        if let r = try? await Feed.fetch("speakers", as: SpeakerRoster.self) { roster = r }
    }

    private func play(_ card: VoiceCard) async {
        player?.stop()
        do {
            let data = try await Feed.data("speakers/clip/\(card.id)")
            guard deck.current?.id == card.id else { return }
            try AVAudioSession.sharedInstance().setCategory(.playback, options: [.mixWithOthers])
            try AVAudioSession.sharedInstance().setActive(true)
            let p = try AVAudioPlayer(data: data)
            player = p
            playing = true
            p.play()
            try? await Task.sleep(for: .seconds(p.duration + 0.2))
            if player === p { playing = false }
        } catch {
            playing = false
        }
    }
}

/// "Who is it?": the card's guess first (one tap: the calendar's likely person, else LifeOS's maybe),
/// then known people (most recognized first, the event's other attendees and LifeOS's other maybes ahead of them), a new name, Remove name (back to
/// an unnamed speaker), or not speech.
struct VoicePicker: View {
    let card: VoiceCard
    let people: [String]
    let answer: (VoiceAnswer) -> Void
    @State private var newName = ""
    @State private var submit = 0
    @FocusState private var typing: Bool

    var body: some View {
        NavigationStack {
            List {
                if let g = card.oneTap {
                    Section {
                        GuessConfirm(guess: g) { answer(g.answer) }
                            .padding(.vertical, Spacing.xs)
                    }
                    .surfaceRows()
                }
                Section {
                    NameField(text: $newName, placeholder: "Search for a name")
                        .focused($typing)
                        .onSubmit { submit += 1 }
                } header: { SectionLabel("Search for a person") }
                .surfaceRows()
                Section {
                    SuggestionRows(query: newName, fallback: people, excluding: card.guess, lead: card.pickerLead,
                                   submitTick: submit) { s in
                        if s.isNew { answer(.newPerson(s.label)) }
                        else if let id = s.entityId, !(s.known && s.voiceprint) { answer(.suggested(s.label, entityId: id)) }
                        else { answer(.person(s.label)) }
                    }
                } header: { SectionLabel("Who is it?") }
                .surfaceRows()
                Section {
                    RemoveNameRow { answer(.removeName) }
                    Button { answer(.notSpeech) } label: {
                        RowLabel(title: "Not speech", symbol: "speaker.slash")
                    }
                    .buttonStyle(.plain)
                }
                .surfaceRows()
            }
            .nightList()
            .navigationTitle(card.guess.map { "Not \($0)" } ?? "Who's this?")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

}
