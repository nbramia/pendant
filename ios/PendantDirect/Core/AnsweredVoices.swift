import Foundation

/// Voices answered today on either screen (Who's this or Voice Scoring): a name, a one-tap guess, Remove
/// name, Not a voice or a skip, plus the voices a name also covered in the rest of the conversation.
/// Both screens leave them out of what the server lists, so an answer given on one screen is gone from
/// the other at once, and a list fetched before the answer (a slow request, a stale copy) can't bring
/// it back. It keeps only the current day: the server leaves a skipped voice out until the day ends
/// and may offer it again the next day, and every other answer leaves its list for good.
public struct AnsweredVoices: Codable, Equatable {
    public private(set) var day: String
    public private(set) var ids: Set<Int>

    public init(day: String = "", ids: Set<Int> = []) {
        self.day = day
        self.ids = ids
    }

    /// The local calendar day (`YYYY-MM-DD`), as the server counts its days.
    public static func day(of date: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// Records answered voices; a new day starts empty.
    public mutating func add(_ answered: some Sequence<Int>, on today: String) {
        roll(to: today)
        ids.formUnion(answered)
    }

    public func contains(_ id: Int, on today: String) -> Bool {
        day == today && ids.contains(id)
    }

    /// The cards still to ask about today, in their order.
    public func unanswered<C: Identifiable>(_ cards: [C], on today: String) -> [C] where C.ID == Int {
        day == today ? cards.filter { !ids.contains($0.id) } : cards
    }

    private mutating func roll(to today: String) {
        if day != today {
            day = today
            ids = []
        }
    }
}
