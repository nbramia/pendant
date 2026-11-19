import SwiftUI

// The faces of the two voice-tagging screens, built only from design-system components so
// `swift test` can render them (`SnapshotTests`). The app views own the data and networking.

/// The Who's this card on Today: play the voice, confirm the top guess in one tap, tap a name, or
/// answer another way: Not a voice, Stranger (someone not worth tagging) or Two people (a clip
/// that mixes two voices). None of the three comes back.
struct WhosThisPanel<Extra: View>: View {
    let remaining: Int
    /// The name the server guesses (filled button), if any.
    let guess: String?
    let excerpt: String?
    let playing: Bool
    let names: [String]
    var busy = false
    let onPlay: () -> Void
    let onName: (String) -> Void
    let onSomeoneElse: () -> Void
    let onNotVoice: () -> Void
    let onStranger: () -> Void
    let onTwoPeople: () -> Void
    /// The top guess (the calendar's likely person, else LifeOS's maybe): one "<name>?" button above
    /// the names, sent as `onGuess` with no sheet in between.
    var oneTap: CardGuess? = nil
    var onGuess: (VoiceAnswer) -> Void = { _ in }
    /// Shown under the names (the type-a-name field once "Someone else" is tapped).
    @ViewBuilder var extra: Extra

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: Spacing.m) {
                HStack {
                    Label("Who's this?", systemImage: "person.wave.2").font(TypeScale.headline)
                    Spacer()
                    Badge(text: "\(remaining) to tag")
                }
                HStack(alignment: .center, spacing: 14) {
                    Button(action: onPlay) {
                        Label(playing ? "Playing" : "Play voice",
                              systemImage: playing ? "waveform" : "play.fill")
                            .symbolEffect(.variableColor.iterative, isActive: playing)
                    }
                    .buttonStyle(.icon(.play))
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        if let guess {
                            Text("Is this").font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                            Text("\(guess)?").font(.title2.weight(.bold)).lineLimit(2).minimumScaleFactor(0.7)
                        } else {
                            Text("Tap play, then pick who it is").font(TypeScale.title3)
                        }
                        if let e = excerpt, !e.isEmpty {
                            Text("“\(e)”").font(.footnote).italic().foregroundStyle(Theme.textSecondary).lineLimit(2)
                        }
                    }
                    Spacer(minLength: 0)
                }
                if let oneTap {
                    GuessConfirm(guess: oneTap, action: confirmGuess).disabled(busy)
                }
                // every name and "Someone else": medium buttons, two to a row, all the same width
                VStack(spacing: Spacing.s) {
                    ForEach(Array(Self.rows(names).enumerated()), id: \.offset) { _, row in
                        ButtonRow(columns: 2) {
                            ForEach(row, id: \.self) { n in nameButton(n) }
                        }
                    }
                }
                .disabled(busy)
                extra
                ButtonRow {
                    Button(action: onNotVoice) { Label("Not a voice", systemImage: "speaker.slash") }
                        .buttonStyle(.tertiary(.small))
                    Button(action: onStranger) { Label("Stranger", systemImage: "person.fill.questionmark") }
                        .buttonStyle(.tertiary(.small))
                        .accessibilityHint("Someone you don't want to tag. They're asked about again if they turn up later.")
                    Button(action: onTwoPeople) { Label("Two people", systemImage: "person.2") }
                        .buttonStyle(.tertiary(.small))
                        .accessibilityHint("This clip mixes two voices. It's never used to learn a voice.")
                }
                .disabled(busy)
            }
        }
    }
}

extension WhosThisPanel {
    /// The guess button's action: the guess's answer, once, unless an answer is already being sent.
    func confirmGuess() {
        guard !busy, let oneTap else { return }
        onGuess(oneTap.answer)
    }

    /// "Someone else" in the name grid (names are never empty strings).
    static var someoneElse: String { "" }

    /// The names, then "Someone else", two to a row.
    static func rows(_ names: [String]) -> [[String]] {
        let all = names + [someoneElse]
        return stride(from: 0, to: all.count, by: 2).map { Array(all[$0..<min($0 + 2, all.count)]) }
    }

    @ViewBuilder func nameButton(_ n: String) -> some View {
        if n == Self.someoneElse {
            Button(action: onSomeoneElse) { Label("Someone else", systemImage: "plus") }
                .buttonStyle(.secondary(.medium))
        } else if n == guess {
            Button { onName(n) } label: { Label(n, systemImage: "checkmark") }
                .buttonStyle(.primary(.medium))
        } else {
            Button(n) { onName(n) }.buttonStyle(.secondary(.medium))
        }
    }
}

/// The Who's this card's guess: "Alex Rivera?" as the card's main button, its reason under it.
struct GuessConfirm: View {
    let guess: CardGuess
    let action: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Button(action: action) { Label(guess.title, systemImage: "checkmark") }
                .buttonStyle(.primary(.large))
                .accessibilityLabel(guess.accessibilityLabel)
                .accessibilityHint(guess.reason)
                .accessibilityIdentifier("guess-confirm")
            Label {
                Text(guess.reason).font(TypeScale.caption).foregroundStyle(Theme.textSecondary).lineLimit(2)
            } icon: {
                Image(systemName: guess.symbol).foregroundStyle(Theme.teal)
            }
            .font(TypeScale.caption)
            .accessibilityHidden(true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// "Probably Morgan Hale · from <event>" with a one-tap "Yes, Morgan Hale": the calendar's
/// suggestion for a voice, shown first in every picker; and the same card for LifeOS's
/// "maybe: Alex Rivera?" with its reasons. Confirming is an ordinary hand label.
struct LikelyConfirm: View {
    let prompt: String
    let source: String
    let confirmTitle: String
    var symbol = "calendar"
    /// Accessibility identifiers: "<id>-person" for the card, "<id>-confirm" for its button.
    var id = "likely"
    /// The confirm button's title when it says more than "Yes, <name>" (the reassign sheet names the scope).
    var title: String? = nil
    let action: () -> Void

    init(person: LikelyPerson, title: String? = nil, action: @escaping () -> Void) {
        self.init(prompt: person.prompt, source: person.source, confirmTitle: person.confirmTitle, title: title,
                  action: action)
    }

    init(maybe: MaybePerson, title: String? = nil, action: @escaping () -> Void) {
        self.init(prompt: maybe.prompt, source: maybe.source, confirmTitle: maybe.confirmTitle,
                  symbol: "questionmark.circle", id: "maybe", title: title, action: action)
    }

    init(prompt: String, source: String, confirmTitle: String, symbol: String = "calendar", id: String = "likely",
         title: String? = nil, action: @escaping () -> Void) {
        self.prompt = prompt; self.source = source; self.confirmTitle = confirmTitle; self.symbol = symbol
        self.id = id; self.title = title; self.action = action
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Image(systemName: symbol).foregroundStyle(Theme.teal).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: Spacing.xxs) {
                    Text(prompt).font(TypeScale.label).foregroundStyle(Theme.textPrimary)
                    Text(source).font(TypeScale.caption).foregroundStyle(Theme.textSecondary).lineLimit(2)
                }
            }
            Button(action: action) { Label(title ?? confirmTitle, systemImage: "checkmark") }
                .buttonStyle(.primary(.large))
                .accessibilityIdentifier("\(id)-confirm")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("\(id)-person")
    }
}

/// The conversation's calendar match: "<event> · <attendees>" under the title.
struct CalendarLine: View {
    let match: CalendarMatch

    var body: some View {
        Label {
            Text(match.line).font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary).lineLimit(3)
        } icon: {
            Image(systemName: "calendar").foregroundStyle(Theme.teal)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Calendar: \(match.line)")
        .accessibilityIdentifier("conversation-calendar")
    }
}

/// A linked meeting note under the title, laid out like `CalendarLine`: the link icon, then
/// "Also recorded in Granola:" with the note's name in the accent. Opens it in Obsidian.
struct MeetingLinkRow: View {
    let link: MeetingLink

    var body: some View {
        if let url = link.url {
            Link(destination: url) {
                Label {
                    (Text("\(link.lead) ").foregroundStyle(Theme.textSecondary)
                     + Text(link.name).foregroundStyle(Theme.teal))
                        .font(TypeScale.subheadline)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } icon: {
                    Image(systemName: "link").foregroundStyle(Theme.teal)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(link.title)
            .accessibilityHint("Opens the note in Obsidian")
        }
    }
}

/// The face of one Voice Scoring card.
struct VoiceCardView: View {
    let card: VoiceCard
    let playing: Bool
    let remaining: Int

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.l) {
            HStack {
                Badge(text: card.confidenceHint,
                      symbol: card.guess == nil ? "person.wave.2" : "person.fill.questionmark", tone: .accent)
                Spacer()
                Text("\(remaining) left").font(.caption.monospacedDigit()).foregroundStyle(Theme.textSecondary)
            }
            Text(card.prompt).font(TypeScale.display).minimumScaleFactor(0.6).lineLimit(2)
                .foregroundStyle(Theme.textPrimary)
            Image(systemName: "waveform")
                .font(.system(size: 56)).foregroundStyle(Theme.teal)
                .symbolEffect(.variableColor.iterative, isActive: playing)
                .frame(maxWidth: .infinity)
                .padding(.vertical, Spacing.s)
                .accessibilityLabel(playing ? "Playing" : "Voice clip")
            if let e = card.excerpt, !e.isEmpty {
                Text("“\(e)”").font(.title3).italic().foregroundStyle(Theme.textSecondary).lineLimit(6)
            }
            Spacer(minLength: 0)
            HStack {
                if let w = card.when {
                    Text(Date(timeIntervalSince1970: Double(w) / 1000).relative)
                }
                if let d = card.device { Text("· \(d)") }
                Spacer()
                if let c = card.confidence, card.guess != nil {
                    Text("\(Int((c * 100).rounded()))% sure").monospacedDigit()
                }
            }
            .font(TypeScale.caption).foregroundStyle(Theme.textTertiary)
        }
        .padding(Spacing.xl)
        .frame(maxWidth: .infinity, maxHeight: 520, alignment: .topLeading)
        .surface(radius: Radius.hero)
        .accessibilityElement(children: .combine)
    }
}

/// Voice Scoring's answers under the card: the top guess as one "<name>?" button when there is one,
/// No and Yes side by side, then Play again, Stranger and Two people (swiping up skips for good).
struct VoiceScoringActions: View {
    let guess: String?
    let playing: Bool
    var busy = false
    let onNo: () -> Void
    let onYes: () -> Void
    let onReplay: () -> Void
    let onStranger: () -> Void
    let onTwoPeople: () -> Void
    /// The card's one-tap guess (the calendar's likely person, else LifeOS's maybe), sent as `onGuess`
    /// with no sheet in between, as on the Who's this card.
    var oneTap: CardGuess? = nil
    var onGuess: (VoiceAnswer) -> Void = { _ in }

    var body: some View {
        VStack(spacing: Spacing.xs) {
            if let oneTap {
                GuessConfirm(guess: oneTap, action: confirmGuess).padding(.bottom, Spacing.xs)
            }
            ButtonRow(spacing: Spacing.m) {
                Button(action: onNo) { Label("No", systemImage: "xmark") }
                    .buttonStyle(.destructive(.medium))
                    .accessibilityLabel("No, pick who it is")
                Button(action: onYes) { Label(guess == nil ? "Name it" : "Yes", systemImage: "checkmark") }
                    .buttonStyle(.primary(.medium))
                    .accessibilityLabel(guess.map { "Yes, it's \($0)" } ?? "Name this voice")
            }
            ButtonRow(spacing: Spacing.m) {
                Button(action: onReplay) {
                    Label(playing ? "Playing" : "Play again", systemImage: playing ? "waveform" : "arrow.counterclockwise")
                }
                .buttonStyle(.tertiary(.small))
                Button(action: onStranger) { Label("Stranger", systemImage: "person.fill.questionmark") }
                    .buttonStyle(.tertiary(.small))
                    .accessibilityHint("Someone you don't want to tag. They're asked about again if they turn up later.")
                Button(action: onTwoPeople) { Label("Two people", systemImage: "person.2") }
                    .buttonStyle(.tertiary(.small))
                    .accessibilityHint("This clip mixes two voices. It's never used to learn a voice.")
            }
        }
    }
}

extension VoiceScoringActions {
    /// The guess button's action: the guess's answer, once, unless an answer is already being sent.
    func confirmGuess() {
        guard !busy, let oneTap else { return }
        onGuess(oneTap.answer)
    }
}

/// A short list of likely people as one-tap name buttons (medium, two to a row, all the same
/// width), e.g. above "Someone else"'s search. Tapping one picks that person.
struct SuggestionButtons: View {
    let people: [PersonSuggestion]
    var busy = false
    let pick: (PersonSuggestion) -> Void

    var body: some View {
        VStack(spacing: Spacing.s) {
            ForEach(Array(stride(from: 0, to: people.count, by: 2)), id: \.self) { i in
                ButtonRow(columns: 2) {
                    ForEach(people[i..<min(i + 2, people.count)]) { p in
                        Button(p.name) { choose(p) }
                            .buttonStyle(.secondary(.medium))
                            .accessibilityHint(p.detail ?? "")
                            .accessibilityIdentifier("someone-else-\(p.label)")
                    }
                }
            }
        }
        .disabled(busy)
    }

    /// A button's action: picks that person, unless an answer is already being sent.
    func choose(_ p: PersonSuggestion) {
        guard !busy else { return }
        pick(p)
    }
}

/// The conversation player (#54): play/pause, a scrubber slot, elapsed and total time, speed and Follow.
struct PlayerCard<Scrubber: View>: View {
    let isPlaying: Bool
    let time: Double
    let duration: Double
    let speed: String
    let follow: Bool
    let onToggle: () -> Void
    let onSpeed: () -> Void
    let onFollow: () -> Void
    @ViewBuilder var scrubber: Scrubber

    var body: some View {
        Card {
            VStack(spacing: Spacing.s) {
                HStack(spacing: 14) {
                    Button(action: onToggle) {
                        Label(isPlaying ? "Pause" : "Play", systemImage: isPlaying ? "pause.fill" : "play.fill")
                    }
                    .buttonStyle(.icon(.play))
                    scrubber
                }
                HStack(spacing: Spacing.s) {
                    Text(Playback.clock(time)).frame(minWidth: 40, alignment: .leading)
                    ButtonRow {
                        Button(speed, action: onSpeed)
                            .buttonStyle(.secondary(.small))
                            .accessibilityLabel("Speed \(speed)")
                        Button(action: onFollow) {
                            Label("Follow", systemImage: follow ? "checkmark.circle.fill" : "circle")
                        }
                        .buttonStyle(.secondary(.small))
                        .accessibilityAddTraits(follow ? .isSelected : [])
                    }
                    Text(Playback.clock(duration)).frame(minWidth: 40, alignment: .trailing)
                }
                .font(.caption.monospacedDigit())
                .foregroundStyle(Theme.textSecondary)
            }
        }
    }
}

/// "Remove name · Back to an unnamed speaker": the picker row that makes a voice (or line)
/// unknown again, in destructive coral. `picked` marks it while it waits for a confirm.
/// Use inside a `List` section.
struct RemoveNameRow: View {
    var picked = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            RowLabel(title: "Remove name", subtitle: Reassign.removeCaption,
                     symbol: "person.crop.circle.badge.xmark", tint: Theme.destructive) {
                if picked {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.destructive)
                        .accessibilityLabel("Picked")
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("remove-name")
        .accessibilityValue(picked ? "Picked" : "")
    }
}
