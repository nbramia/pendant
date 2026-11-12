import AVFoundation
import SwiftUI

/// "Who's this?" — plays a short voice snippet from a recent conversation and
/// lets the wearer tag it. Each answer teaches the server that voice (voiceprints
/// stay on the server). Allowed names are one tap; "Someone else" opens the name picker (a
/// typed name is added to the allowed list; Remove name keeps the voice unnamed and stops it
/// being taken for a known person); "Not a voice" drops background noise; "Skip for now"
/// leaves the voice out for the rest of the day. When the conversation's calendar event leaves one
/// likely attendee, "Yes, <name>" comes first (an ordinary hand label linked to that LifeOS person).
/// The face is `WhosThisPanel` (Core).
struct WhosThisCard: View {
    @State private var unknown: [UnknownSpeaker] = []
    @State private var roster: SpeakerRoster?
    @State private var player: AVAudioPlayer?
    @State private var playing: Int?
    @State private var busy = false
    @State private var thanks: String?
    @State private var typing = false
    @State private var newName = ""
    @State private var submit = 0
    @FocusState private var nameFocused: Bool

    private let refresh = Timer.publish(every: 300, on: .main, in: .common).autoconnect()

    var body: some View {
        // A VStack, not a Group: modifiers on an empty Group never run, so the
        // card's .task never loaded anything and the card never appeared.
        VStack(spacing: 0) {
            if let s = unknown.first, let roster {
                WhosThisPanel(remaining: unknown.count, guess: s.currentLabel ?? s.suggested, excerpt: s.excerpt,
                              playing: playing == s.id, names: roster.allowed, busy: busy,
                              onPlay: { Task { await play(s) } },
                              onName: { name in Task { await label(s, as: name) } },
                              onSomeoneElse: { typing = true },
                              onNotVoice: { Task { await noise(s) } },
                              onSkip: { Task { await skip(s) } },
                              likely: s.likelyOther,
                              onLikely: { if let p = s.likely { Task { await saveLikely(s, p) } } }) {
                    if typing {
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            SuggestionRows(query: newName, fallback: roster.allowed, excluding: nil, current: nil,
                                           lead: s.candidates ?? [], submitTick: submit) { pick in
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
            } else if ScreenTour.samples, let roster, !roster.allowed.isEmpty {
                // screen tour only (debug builds): an inert card so its buttons can be reviewed
                WhosThisPanel(remaining: 1, guess: nil, excerpt: "Sample for the screen tour",
                              playing: false, names: roster.allowed, onPlay: {}, onName: { _ in },
                              onSomeoneElse: {}, onNotVoice: {}, onSkip: {},
                              likely: CalendarHint.tourSample.likely) { EmptyView() }
                    .accessibilityElement(children: .contain)
                    .accessibilityIdentifier("whos-this")
            } else if let thanks {
                Card { Label(thanks, systemImage: "checkmark.seal.fill").font(TypeScale.callout).foregroundStyle(Theme.teal) }
            }
        }
        .frame(maxWidth: .infinity)
        .task { await load() }
        .onReceive(refresh) { _ in Task { await load() } }
    }

    private func load() async {
        roster = try? await Feed.fetch("speakers", as: SpeakerRoster.self)
        unknown = (try? await Feed.fetch("speakers/unknown", as: UnknownSpeakers.self))?.speakers ?? []
        PendantManager.shared.log("who's this: \(unknown.count) voice(s) to tag, roster \(roster?.allowed.count ?? -1)")
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
        advance(thanks: "Got it.")
    }

    /// Not now: the server leaves this voice out for the rest of the day.
    private func skip(_ s: UnknownSpeaker) async {
        busy = true
        defer { busy = false }
        guard (try? await Feed.post("speakers/\(s.id)/skip", json: [:])) != nil else { return }
        advance(thanks: "That's all for now.")
    }

    /// Remove name: the voice stays an unnamed speaker and is kept as a stranger on the server.
    private func removeName(_ s: UnknownSpeaker) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        guard (try? await Feed.post(VoiceAnswer.removeName.request(cardID: s.id).path, json: [:])) != nil else { return }
        advance(thanks: "Name removed. It stays an unnamed speaker.")
    }

    /// A row from the suggestions: a known voice or a LifeOS person (which links the name).
    private func saveSuggested(_ s: UnknownSpeaker, _ pick: PersonSuggestion) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        var body: [String: String] = ["name": pick.label, "source": "user"]
        if let id = pick.entityId { body["entity_id"] = id }
        guard (try? await Feed.post("speakers/\(s.id)/label", json: body)) != nil else { return }
        await refreshRoster()
        advance(thanks: "Thanks — I'll learn \(pick.label)'s voice from here.")
    }

    /// "Yes, <name>" on the calendar's suggestion: the same hand label as picking that LifeOS person.
    private func saveLikely(_ s: UnknownSpeaker, _ p: LikelyPerson) async {
        await saveSuggested(s, PersonSuggestion(name: p.name, label: p.label, entityId: p.entityId))
    }

    private func refreshRoster() async {
        if let r = try? await Feed.fetch("speakers", as: SpeakerRoster.self) { roster = r }
    }

    private func advance(thanks message: String) {
        player?.stop()
        typing = false
        newName = ""
        withAnimation(.snappy) {
            if !unknown.isEmpty { unknown.removeFirst() }
            if unknown.isEmpty { thanks = message }
        }
    }

    private func label(_ s: UnknownSpeaker, as name: String) async {
        busy = true
        defer { busy = false }
        guard (try? await Feed.post("speakers/\(s.id)/label", json: ["name": name])) != nil else { return }
        advance(thanks: "Thanks — I'll learn \(name)'s voice from here.")
    }
}
