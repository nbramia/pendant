import Foundation

/// One Voice Scoring card: a speaker cluster the server wants confirmed or
/// named (`GET /pendant/feed/speakers/unknown`).
public struct VoiceCard: Codable, Identifiable, Hashable {
    public let id: Int
    public let when: Int?              // ms since epoch
    public let excerpt: String?
    public let currentLabel: String?   // the automatic name, or the closest voiceprint
    public let suggested: String?      // the likely wearer, before any wearer voiceprint exists
    public let confidence: Double?
    public let newVoice: Bool?
    public let noiseLike: Bool?
    public let device: String?
    public let durationS: Double?
    /// Who it probably is, from the conversation's calendar event (one attendee left).
    public let likely: LikelyPerson?
    /// The event's other remaining attendees: they head the picker.
    public let candidates: [PersonSuggestion]?

    enum CodingKeys: String, CodingKey {
        case id, when, excerpt, suggested, confidence, device, likely, candidates
        case currentLabel = "current_label"
        case newVoice = "new_voice"
        case noiseLike = "noise_like"
        case durationS = "duration_s"
    }

    public init(id: Int, when: Int? = nil, excerpt: String? = nil, currentLabel: String? = nil,
                suggested: String? = nil, confidence: Double? = nil, newVoice: Bool? = nil,
                noiseLike: Bool? = nil, device: String? = nil, durationS: Double? = nil,
                likely: LikelyPerson? = nil, candidates: [PersonSuggestion]? = nil) {
        self.id = id; self.when = when; self.excerpt = excerpt; self.currentLabel = currentLabel
        self.suggested = suggested; self.confidence = confidence; self.newVoice = newVoice
        self.noiseLike = noiseLike; self.device = device; self.durationS = durationS
        self.likely = likely; self.candidates = candidates
    }

    /// The calendar's likely person, unless the card already asks about that name.
    public var likelyOther: LikelyPerson? {
        guard let likely else { return nil }
        guard let g = guess else { return likely }
        return [likely.label, likely.name].contains { $0.caseInsensitiveCompare(g) == .orderedSame } ? nil : likely
    }

    /// The name the card asks about, if any.
    public var guess: String? {
        let g = (currentLabel ?? suggested)?.trimmingCharacters(in: .whitespaces)
        return (g?.isEmpty ?? true) ? nil : g
    }

    /// "Is this Alex?" or "Who's this?"
    public var prompt: String { guess.map { "Is this \($0)?" } ?? "Who's this?" }

    /// A short hint of how sure the server is.
    public var confidenceHint: String {
        if newVoice == true { return "Sounds like someone new" }
        if noiseLike == true { return "Might not be speech" }
        let c = confidence ?? 0
        switch c {
        case ..<0.25: return "Not sure"
        case ..<0.5: return "Somewhat sure"
        case ..<0.75: return "Fairly sure"
        default: return "Pretty sure"
        }
    }
}

public enum SwipeDirection: Equatable {
    case left, right, up

    /// The swipe a drag ends in, or nil when it should snap back. Up needs to be
    /// mostly vertical; left/right mostly horizontal.
    public static func of(dx: Double, dy: Double, threshold: Double = 110) -> SwipeDirection? {
        if -dy >= threshold && abs(dy) > abs(dx) { return .up }
        if abs(dx) >= threshold && abs(dx) > abs(dy) { return dx > 0 ? .right : .left }
        return nil
    }
}

/// What the wearer said about a card, and the feed request that records it.
public enum VoiceAnswer: Equatable {
    case confirm(String)       // yes, it's the guess
    case person(String)        // a known person from the picker
    case newPerson(String)     // a typed name
    case suggested(String, entityId: String)   // a LifeOS person from the suggestions: links the name
    case removeName            // back to an unnamed speaker: kept as a stranger, never a known person
    case notSpeech
    case skip

    /// (path under /pendant/feed, JSON body). Names are sent with source
    /// "user": the wearer picked or typed them, which is consent for a voiceprint.
    public func request(cardID: Int) -> (path: String, body: [String: String]) {
        switch self {
        case .confirm(let n), .person(let n), .newPerson(let n):
            return ("speakers/\(cardID)/label", ["name": n.trimmingCharacters(in: .whitespaces), "source": "user"])
        case .suggested(let n, let id):
            return ("speakers/\(cardID)/label", ["name": n.trimmingCharacters(in: .whitespaces), "source": "user", "entity_id": id])
        case .removeName: return ("speakers/\(cardID)/stranger", [:])
        case .notSpeech: return ("speakers/\(cardID)/noise", [:])
        case .skip: return ("speakers/\(cardID)/skip", [:])
        }
    }
}

/// The card stack's state machine. The view drives it with swipes and picker
/// choices, sends the returned answer, and reports back with `finished`.
public struct VoiceDeck: Equatable {
    public enum Phase: Equatable { case card, picking, sending }
    public enum Step: Equatable {
        case submit(VoiceAnswer)
        case pick                  // open the people sheet
        case none
    }

    public private(set) var cards: [VoiceCard] = []
    public private(set) var phase: Phase = .card
    public private(set) var done = 0
    public private(set) var failed = false
    /// Cards answered or skipped this session: a refresh never brings them back.
    private var seen = Set<Int>()

    public init(_ cards: [VoiceCard] = []) { load(cards) }

    public var current: VoiceCard? { cards.first }
    public var isEmpty: Bool { cards.isEmpty }

    /// Server order (least confident first) with noise-like cards last; cards
    /// already handled this session are dropped. Keeps the current card in
    /// front while it is being answered.
    public mutating func load(_ fresh: [VoiceCard]) {
        var list = VoiceDeck.ordered(fresh.filter { !seen.contains($0.id) })
        if phase != .card, let cur = current {
            list.removeAll { $0.id == cur.id }
            list.insert(cur, at: 0)
        }
        cards = list
    }

    public static func ordered(_ cards: [VoiceCard]) -> [VoiceCard] {
        cards.enumerated().sorted { a, b in
            let an = a.element.noiseLike == true, bn = b.element.noiseLike == true
            if an != bn { return !an }
            let ac = a.element.confidence ?? 0, bc = b.element.confidence ?? 0
            if ac != bc { return ac < bc }
            return a.offset < b.offset
        }.map(\.element)
    }

    /// Right = yes (or the picker when there's no guess), left = no (the
    /// picker), up = skip.
    public mutating func swipe(_ d: SwipeDirection) -> Step {
        guard phase == .card, let card = current else { return .none }
        switch d {
        case .right:
            if let g = card.guess { return begin(.confirm(g)) }
            phase = .picking
            return .pick
        case .left:
            phase = .picking
            return .pick
        case .up:
            return begin(.skip)
        }
    }

    /// "Yes, <name>" on the calendar's likely person: a hand label linked to that LifeOS person.
    /// Works from the card and from the picker sheet; nothing without a likely person.
    public mutating func confirmLikely() -> Step {
        guard phase == .card || phase == .picking, let p = current?.likely else { return .none }
        return begin(.suggested(p.label, entityId: p.entityId))
    }

    /// A choice from the picker sheet.
    public mutating func choose(_ a: VoiceAnswer) -> Step {
        guard phase == .picking, current != nil else { return .none }
        if case .newPerson(let n) = a, n.trimmingCharacters(in: .whitespaces).isEmpty { return .none }
        return begin(a)
    }

    /// The picker was dismissed without an answer: back to the card.
    public mutating func cancelPick() {
        if phase == .picking { phase = .card }
    }

    /// The answer reached the server (or not). On success the next card
    /// becomes current (the view auto-plays it); on failure the card stays.
    public mutating func finished(success: Bool) {
        guard phase == .sending else { return }
        phase = .card
        failed = !success
        guard success, let card = current else { return }
        seen.insert(card.id)
        cards.removeFirst()
        done += 1
    }

    private mutating func begin(_ a: VoiceAnswer) -> Step {
        phase = .sending
        failed = false
        return .submit(a)
    }
}

/// People for the "who is it?" sheet: enrolled voiceprints most recognized
/// first, then other allowed names alphabetically; the rejected guess is left
/// out and names are de-duplicated case-insensitively.
public enum PickerOrder {
    public struct Person: Equatable {
        public let name: String
        public let recognized: Int
        public let count: Int
        public init(name: String, recognized: Int, count: Int) {
            self.name = name; self.recognized = recognized; self.count = count
        }
    }

    public static func names(enrolled: [Person], allowed: [String], excluding: String? = nil) -> [String] {
        var out: [String] = []
        var have = Set<String>()
        if let e = excluding { have.insert(e.lowercased()) }
        let ranked = enrolled.sorted {
            ($0.recognized, $0.count) != ($1.recognized, $1.count)
                ? ($0.recognized, $0.count) > ($1.recognized, $1.count)
                : $0.name.lowercased() < $1.name.lowercased()
        }
        for n in ranked.map(\.name) + allowed.sorted(by: { $0.lowercased() < $1.lowercased() })
        where have.insert(n.lowercased()).inserted {
            out.append(n)
        }
        return out
    }
}
