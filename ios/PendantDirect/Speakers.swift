import AVFoundation
import SwiftUI

/// "Who's this?" — plays a short voice snippet from a recent conversation and
/// lets the wearer tag it. Each answer teaches the server that voice (voiceprints
/// stay on the server). Allowed names are one tap; "Someone else" opens the name picker (a
/// typed name is added to the allowed list; up to three likely people not already on the card come
/// first there as one-tap names; Remove name keeps the voice unnamed and stops it
/// being taken for a known person); "Not a voice" drops background noise; "Stranger" is Remove name in
/// one tap (someone not worth tagging: a voice like theirs is asked about again if it turns up later);
/// "Two people" marks a clip that mixes two voices, never used to learn a voice. None of them comes back. The top guess comes first as one "<name>?" button:
/// the calendar event's one likely attendee, else LifeOS's first maybe when the calendar offers
/// nobody. One tap saves it (an ordinary hand label linked to that LifeOS person) and moves on.
/// The face is `WhosThisPanel` (Core).
struct WhosThisCard: View {
    @State private var unknown: [UnknownSpeaker] = []
    /// Today's answers from both screens (Voice Scoring's too): they never show here again today.
    private let answers = VoiceAnswerLog.shared
    @State private var roster: SpeakerRoster?
    @State private var player: AVAudioPlayer?
    @State private var playing: Int?
    @State private var busy = false
    @State private var thanks: String?
    @State private var typing = false
    @State private var newName = ""
    @State private var submit = 0
    @State private var tourStep = 0
    @State private var tourTyping = false
    @FocusState private var nameFocused: Bool

    private let refresh = Timer.publish(every: 300, on: .main, in: .common).autoconnect()

    var body: some View {
        // A VStack, not a Group: modifiers on an empty Group never run, so the
        // card's .task never loaded anything and the card never appeared.
        VStack(spacing: 0) {
            if let s = shown.first, let roster {
                WhosThisPanel(remaining: shown.count, guess: s.currentLabel ?? s.suggested, excerpt: s.excerpt,
                              playing: playing == s.id, names: roster.allowed, busy: busy,
                              onPlay: { Task { await play(s) } },
                              onName: { name in Task { await label(s, as: name) } },
                              onSomeoneElse: { typing = true },
                              onNotVoice: { Task { await noise(s) } },
                              onStranger: { Task { await removeName(s) } },
                              onTwoPeople: { Task { await twoPeople(s) } },
                              oneTap: s.oneTap,
                              onGuess: { answer in Task { await send(s, answer) } }) {
                    if typing {
                        // the likeliest few as one-tap names above the search; the rows below skip them
                        let quick = s.someoneElse(onCard: roster.allowed)
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            if !quick.isEmpty {
                                SuggestionButtons(people: quick, busy: busy) { pick in Task { await saveSuggested(s, pick) } }
                                    .padding(.bottom, Spacing.xs)
                            }
                            SuggestionRows(query: newName, fallback: roster.allowed, excluding: nil, current: nil,
                                           lead: s.pickerLead, hidden: quick, submitTick: submit) { pick in
                                Task { await saveSuggested(s, pick) }
                            }
                            RemoveNameRow { Task { await removeName(s) } }.disabled(busy)
                        }
                        .padding(.vertical, Spacing.xxs)
                        NameField(text: $newName, placeholder: "Search for a name")
                            .focused($nameFocused)
                            .onSubmit { submit += 1 }
                            .onAppear { nameFocused = true }
                    }
                }
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("whos-this")
                .transition(.move(edge: .trailing).combined(with: .opacity))
            } else if let thanks {
                Card { Label(thanks, systemImage: "checkmark.seal.fill").font(TypeScale.callout).foregroundStyle(Theme.teal) }
            }
            if ScreenTour.samples, let roster, !roster.allowed.isEmpty, tourStep < Self.tourHints.count {
                // screen tour only (debug builds): inert cards so the guess button and "Someone else"'s
                // one-tap names can be reviewed and tapped: the calendar's guess, LifeOS's maybe, then a
                // card with no guess whose "Someone else" offers the calendar's candidates and a maybe
                let hint = Self.tourHints[tourStep]
                let next = { withAnimation(.snappy) { tourStep += 1; tourTyping = false } }
                WhosThisPanel(remaining: Self.tourHints.count - tourStep, guess: nil, excerpt: "Sample for the screen tour",
                              playing: false, names: roster.allowed, onPlay: {}, onName: { _ in },
                              onSomeoneElse: { tourTyping = true }, onNotVoice: {}, onStranger: { next() }, onTwoPeople: { next() },
                              oneTap: hint.cardGuess(asking: nil),
                              onGuess: { _ in next() }) {
                    let quick = hint.someoneElse(asking: nil, onCard: roster.allowed)
                    if tourTyping && !quick.isEmpty {
                        SuggestionButtons(people: quick) { _ in next() }
                    }
                }
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("whos-this-sample")
            }
        }
        .frame(maxWidth: .infinity)
        // first appearance, Today's pull to refresh and Voice Scoring closing
        .task(id: answers.refreshes) { await load() }
        // an answer on either screen also leaves the list the card holds, so a failed fetch after
        // midnight (when the day's record starts over) can't bring it back
        .onChange(of: answers.answered) { _, a in unknown.removeAll { a.ids.contains($0.id) } }
        .onReceive(refresh) { _ in Task { await load() } }
    }

    /// The server's list without the voices answered today on either screen.
    private var shown: [UnknownSpeaker] { answers.unanswered(unknown) }

    /// Never the saved copy of the list: it can hold voices answered since it was saved. When the
    /// server can't be reached the card keeps what it has.
    private func load() async {
        if let r = try? await Feed.fetch("speakers", as: SpeakerRoster.self) { roster = r }
        guard let fresh = try? await Feed.fetch("speakers/unknown", as: UnknownSpeakers.self, cached: false) else {
            PendantManager.shared.log("who's this: list unavailable, keeping \(shown.count)")
            return
        }
        unknown = fresh.speakers
        PendantManager.shared.log("who's this: \(shown.count) voice(s) to tag (\(unknown.count - shown.count) answered today), roster \(roster?.allowed.count ?? -1)")
    }

    private func play(_ s: UnknownSpeaker) async {
        do {
            let data = try await Feed.data("speakers/clip/\(s.id)")
            try AVAudioSession.sharedInstance().setCategory(.playback, options: [.mixWithOthers])
            let p = try AVAudioPlayer(data: data)
            player = p
            playing = s.id
            p.play()
            try? await Task.sleep(for: .seconds(p.duration + 0.2))
            if playing == s.id { playing = nil }
        } catch {
            playing = nil
        }
    }

    private func noise(_ s: UnknownSpeaker) async {
        busy = true
        defer { busy = false }
        guard (try? await Feed.post("speakers/\(s.id)/noise", json: [:])) != nil else { return }
        advance(s, thanks: "Got it.")
    }

    /// Two people: the clip mixes two voices. Never asked again, and never a voiceprint sample.
    private func twoPeople(_ s: UnknownSpeaker) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        guard (try? await Feed.post(VoiceAnswer.twoPeople.request(cardID: s.id).path, json: [:])) != nil else { return }
        advance(s, thanks: "Got it. That clip won't be used to learn a voice.")
    }

    /// Remove name: the voice stays an unnamed speaker and is kept as a stranger on the server.
    private func removeName(_ s: UnknownSpeaker) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        guard (try? await Feed.post(VoiceAnswer.removeName.request(cardID: s.id).path, json: [:])) != nil else { return }
        advance(s, thanks: "Got it. They stay an unnamed speaker.")
    }

    /// A row from the suggestions: a known voice or a LifeOS person (which links the name).
    private func saveSuggested(_ s: UnknownSpeaker, _ pick: PersonSuggestion) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        let r = VoiceAnswer.picked(pick).request(cardID: s.id)
        guard let data = try? await Feed.post(r.path, json: r.body) else { return }
        named(s, pick.label, entityId: pick.entityId)
        dropAlsoNamed(data)
        advance(s, thanks: "Thanks — I'll learn \(pick.label)'s voice from here.")
        await refreshRoster()
    }

    /// The guess button: its answer (a hand label linked to the LifeOS person), then the next card.
    private func send(_ s: UnknownSpeaker, _ answer: VoiceAnswer) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        let r = answer.request(cardID: s.id)
        guard let data = try? await Feed.post(r.path, json: r.body) else { return }
        if let name = r.body["name"] { named(s, name, entityId: r.body["entity_id"]) }
        dropAlsoNamed(data)
        advance(s, thanks: "Thanks — I'll learn \(r.body["name"] ?? "that")'s voice from here.")
        await refreshRoster()
    }

    /// Screen tour only: the sample cards' hints, the last card with no guess.
    private static let tourHints: [CalendarHint] = [.tourSample, .tourSampleMaybe, .tourSampleSomeoneElse]

    /// A name covers the whole conversation: the cards for the voices it also named in the other
    /// parts go (the current card goes with `advance`).
    private func dropAlsoNamed(_ answer: Data) {
        let ids = (try? JSONDecoder().decode(ReassignReceipt.self, from: answer))?.otherIds ?? []
        answers.record(ids)
    }

    /// A voice was named: the other cards from its conversation stop offering that person.
    private func named(_ s: UnknownSpeaker, _ name: String, entityId: String?) {
        unknown = CardHints.named(unknown, answered: s, name: name, entityId: entityId)
    }

    private func refreshRoster() async {
        if let r = try? await Feed.fetch("speakers", as: SpeakerRoster.self) { roster = r }
    }

    /// The answer reached the server: the voice leaves this card and Voice Scoring for the day.
    private func advance(_ s: UnknownSpeaker, thanks message: String) {
        player?.stop()
        typing = false
        newName = ""
        withAnimation(.snappy) {
            answers.record([s.id])
            if shown.isEmpty { thanks = message }
        }
    }

    private func label(_ s: UnknownSpeaker, as name: String) async {
        busy = true
        defer { busy = false }
        guard let data = try? await Feed.post("speakers/\(s.id)/label", json: ["name": name]) else { return }
        named(s, name, entityId: nil)
        dropAlsoNamed(data)
        advance(s, thanks: "Thanks — I'll learn \(name)'s voice from here.")
    }
}
