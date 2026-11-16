#if canImport(AppKit)
import ImageIO
import SwiftUI
import XCTest
@testable import PendantCore

/// Renders the design-system components to PNG with `ImageRenderer` (macOS). Always checks that
/// they render; writes the PNGs only when `PENDANT_SNAPSHOT_DIR` is set, for looking at by eye.
/// Fonts are the Mac's text-style sizes, a little smaller than the phone's.
@MainActor
final class SnapshotTests: XCTestCase {
    private func render<V: View>(_ name: String, width: CGFloat = 390, _ view: V) throws {
        let framed = view
            .padding(Spacing.l)
            .frame(width: width)
            .background(Theme.night)
            .environment(\.colorScheme, .dark)
        let r = ImageRenderer(content: framed)
        r.scale = 2
        let image = try XCTUnwrap(r.cgImage, "\(name) did not render")
        XCTAssertGreaterThan(image.width, 100)
        guard let dir = ProcessInfo.processInfo.environment["PENDANT_SNAPSHOT_DIR"] else { return }
        let url = URL(fileURLWithPath: dir).appendingPathComponent("\(name).png")
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let dest = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(dest, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(dest))
    }

    func testButtons() throws {
        try render("buttons", VStack(alignment: .leading, spacing: Spacing.m) {
            Text("Buttons").font(TypeScale.headline).foregroundStyle(Theme.textPrimary)
            Button("Save just this voice") {}.buttonStyle(.primary(.large))
            ButtonRow {
                Button("Primary") {}.buttonStyle(.primary(.medium))
                Button("Secondary") {}.buttonStyle(.secondary(.medium))
                Button("Destructive") {}.buttonStyle(.destructive(.medium))
            }
            ButtonRow {
                Button { } label: { Label("Noah Park", systemImage: "checkmark") }.buttonStyle(.primary(.medium))
                Button("Alex Rivera") {}.buttonStyle(.secondary(.medium))
            }
            ButtonRow {
                Button { } label: { Label("Open in LifeOS", systemImage: "person.crop.circle.badge.checkmark") }
                    .buttonStyle(.secondary(.small))
                Button { } label: { Label("Change LifeOS person", systemImage: "arrow.triangle.2.circlepath") }
                    .buttonStyle(.secondary(.small))
            }
            HStack {
                Button { } label: { Label("Skip for now", systemImage: "forward") }.buttonStyle(.tertiary(.small))
                Button("Disabled") {}.buttonStyle(.primary(.medium)).disabled(true)
                Button("Add to tasks") {}.buttonStyle(.secondary(.small))
            }
            HStack {
                Button("Restore") {}.buttonStyle(.secondary(.small))
                Button { } label: { Label("Play", systemImage: "play.fill") }.buttonStyle(.icon(.standard))
                Button { } label: { Label("Play voice", systemImage: "play.fill") }.buttonStyle(.icon(.play))
            }
        })
    }

    func testChipsBadgesAndCards() throws {
        try render("components", VStack(alignment: .leading, spacing: Spacing.m) {
            HStack { StatChip(value: "142", label: "minutes", symbol: "waveform")
                     StatChip(value: "9", label: "conversations", symbol: "bubble.left.and.bubble.right") }
            HStack {
                Chip(text: "Notable", symbol: "star.fill", on: true)
            }
            HStack {
                Badge(text: "12m 04s")
                Badge(text: "3", symbol: "star.fill", tone: .star)
                Badge(text: "Media")
                SpeakerChip(name: "Alex")
            }
            MessageCard(title: "No transcripts yet", detail: "Conversations appear here a few minutes after a pause.")
            Card(fill: Theme.sunken) {
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text("Fragment (sunken)").font(TypeScale.label).foregroundStyle(Theme.textSecondary)
                    Text("…and then the TV said something about the weather").font(TypeScale.subheadline)
                        .foregroundStyle(Theme.textTertiary)
                }
            }
        })
    }

    func testTextTokensOnSurfaces() throws {
        let surfaces: [(String, Color)] = [("night", Color(Palette.nightTop)), ("surface", Theme.surface),
                                           ("raised", Theme.raised), ("sunken", Theme.sunken),
                                           ("accentWash", Theme.accentWash)]
        try render("text-tokens", VStack(spacing: Spacing.s) {
            ForEach(surfaces, id: \.0) { name, fill in
                VStack(alignment: .leading, spacing: 2) {
                    Text(name).font(TypeScale.captionStrong).foregroundStyle(Theme.textPrimary)
                    Text("Primary text reads first").foregroundStyle(Theme.textPrimary)
                    Text("Secondary text, the details").font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary)
                    Text("Tertiary, the smallest print").font(TypeScale.caption).foregroundStyle(Theme.textTertiary)
                    Text("Accent teal").font(TypeScale.caption).foregroundStyle(Theme.teal)
                }
                .padding(Spacing.m)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(fill, in: RoundedRectangle(cornerRadius: Radius.control))
            }
        })
    }

    func testWhosThis() throws {
        try render("whos-this", WhosThisPanel(remaining: 4, guess: "Alex Rivera",
                                             excerpt: "so I told him we'd be there by seven, maybe half past",
                                             playing: false, names: ["Noah Park", "Alex Rivera", "Riley"],
                                             onPlay: {}, onName: { _ in }, onSomeoneElse: {}, onNotVoice: {},
                                             onSkip: {}) { EmptyView() })
        try render("whos-this-unknown", WhosThisPanel(remaining: 1, guess: nil, excerpt: nil, playing: true,
                                                     names: ["Noah", "Alex", "Riley", "Someone with a long name"],
                                                     onPlay: {}, onName: { _ in }, onSomeoneElse: {}, onNotVoice: {},
                                                     onSkip: {}) { EmptyView() })
    }

    func testWhosThisGuess() throws {
        let maybe = CalendarHint(maybe: [MaybePerson(name: "Alex Rivera", entityId: "e-lex",
                                                     reasons: ["texted on my way", "home all evening"])])
        try render("whos-this-guess", WhosThisPanel(remaining: 3, guess: nil, excerpt: "are we still on for seven?",
                                                   playing: false, names: ["Noah Park", "Riley Park"],
                                                   onPlay: {}, onName: { _ in }, onSomeoneElse: {}, onNotVoice: {},
                                                   onSkip: {}, oneTap: maybe.cardGuess(asking: nil)) { EmptyView() })
    }

    func testCalendarSuggestion() throws {
        let morgan = LikelyPerson(name: "Morgan Hale", entityId: "e-morgan", event: "Morgan-Noah week-ahead prep")
        try render("calendar-suggestion", VStack(alignment: .leading, spacing: Spacing.l) {
            CalendarLine(match: CalendarMatch(title: "Morgan-Noah week-ahead prep", attendees: [
                .init(name: "Noah Park", wearer: true), .init(name: "Morgan Hale", entityId: "e-morgan")]))
            MeetingLinkRow(link: MeetingLink(note: "Work/Team/Morgan-Noah week-ahead prep 20261109", source: "granola"))
            Card { LikelyConfirm(person: morgan) {} }
            Card { LikelyConfirm(maybe: MaybePerson(name: "Alex Rivera", entityId: "e-lex",
                                                    reasons: ["texted on my way", "home all evening"])) {} }
            WhosThisPanel(remaining: 2, guess: nil, excerpt: "Can people give the access they want to give?",
                          playing: false, names: ["Noah Park", "Alex Rivera"], onPlay: {}, onName: { _ in },
                          onSomeoneElse: {}, onNotVoice: {}, onSkip: {},
                          oneTap: CalendarHint(likely: morgan).cardGuess(asking: nil)) { EmptyView() }
        })
    }

    /// A full LifeOS name (40 characters) in a narrow card must stay inside it.
    func testWhosThisLongNameInNarrowCard() throws {
        let long = "Riley Doctor Paula Grantham-Abernathy Jr"
        XCTAssertEqual(long.count, 40)
        try render("whos-this-long-name", width: 320,
                   WhosThisPanel(remaining: 2, guess: long, excerpt: "see you at the clinic on Tuesday", playing: false,
                                 names: ["Noah", long, "Alex"],
                                 onPlay: {}, onName: { _ in }, onSomeoneElse: {}, onNotVoice: {}, onSkip: {}) { EmptyView() })
    }

    func testVoiceScoring() throws {
        let card = VoiceCard(id: 1, when: nil, excerpt: "Did you get the tickets for Saturday?", currentLabel: "Riley",
                             confidence: 0.42, device: "new")
        try render("voice-scoring", VStack(spacing: Spacing.xl) {
            VoiceCardView(card: card, playing: false, remaining: 7).frame(height: 420)
            VoiceScoringActions(guess: card.guess, playing: false, onNo: {}, onYes: {}, onReplay: {}, onSkip: {})
        })
    }

    func testVoiceScoringGuess() throws {
        let card = VoiceCard(id: 1, excerpt: "are we still on for seven?", confidence: 0.2, device: "new",
                             hints: CalendarHint(maybe: [MaybePerson(name: "Alex Rivera", entityId: "e-lex",
                                                                     reasons: ["texted on my way"])]))
        try render("voice-scoring-guess", VStack(spacing: Spacing.xl) {
            VoiceCardView(card: card, playing: false, remaining: 3).frame(height: 420)
            VoiceScoringActions(guess: card.guess, playing: false, onNo: {}, onYes: {}, onReplay: {}, onSkip: {},
                                oneTap: card.oneTap)
        })
    }

    func testSomeoneElseButtons() throws {
        try render("whos-this-someone-else",
                   WhosThisPanel(remaining: 2, guess: nil, excerpt: nil, playing: false, names: ["Noah Park"],
                                 onPlay: {}, onName: { _ in }, onSomeoneElse: {}, onNotVoice: {}, onSkip: {}) {
                       SuggestionButtons(people: CalendarHint.tourSampleSomeoneElse.someoneElse(asking: nil,
                                                                                                onCard: ["Noah Park"])) { _ in }
                   })
    }

    func testPlayerCard() throws {
        try render("player", PlayerCard(isPlaying: false, time: 74, duration: 612, speed: "1.5×", follow: true,
                                        onToggle: {}, onSpeed: {}, onFollow: {}) {
            // Slider is a native control ImageRenderer can't draw; a stand-in bar shows the layout.
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.raised).frame(height: 4)
                    Capsule().fill(Theme.teal).frame(width: g.size.width * 0.12, height: 4)
                }
                .frame(maxHeight: .infinity)
            }
            .frame(height: 28)
        })
    }

    func testRemoveNameRow() throws {
        try render("remove-name", VStack(alignment: .leading, spacing: Spacing.s) {
            RemoveNameRow {}
            RemoveNameRow(picked: true) {}
            Button("Remove name · this line") {}.buttonStyle(.destructive(.large))
        }
        .padding(Spacing.m)
        .surface(Theme.surface, radius: Radius.row))
    }
}
#endif
