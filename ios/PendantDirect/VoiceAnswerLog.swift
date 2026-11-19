import Foundation
import Observation

/// The app's one record of today's voice answers (`AnsweredVoices`), shared by the Who's this card and
/// Voice Scoring and kept across launches. `refreshes` ticks when Today is pulled to refresh or Voice
/// Scoring closes, so the Who's this card fetches the server's list again.
@MainActor @Observable
final class VoiceAnswerLog {
    static let shared = VoiceAnswerLog()

    private(set) var answered: AnsweredVoices
    private(set) var refreshes = 0
    private static let key = "voiceAnswersToday"

    private init() {
        let saved = UserDefaults.standard.data(forKey: Self.key)
        answered = saved.flatMap { try? JSONDecoder().decode(AnsweredVoices.self, from: $0) } ?? AnsweredVoices()
    }

    private var today: String { AnsweredVoices.day(of: Date(), calendar: AnsweredVoices.serverCalendar) }

    /// Answers reached the server: the voices (with any a name also covered) leave both screens.
    func record(_ ids: some Sequence<Int>) {
        let ids = Array(ids)
        guard !ids.isEmpty else { return }
        answered.add(ids, on: today)
        if let data = try? JSONEncoder().encode(answered) {
            UserDefaults.standard.set(data, forKey: Self.key)
        }
    }

    func unanswered<C: Identifiable>(_ cards: [C]) -> [C] where C.ID == Int {
        answered.unanswered(cards, on: today)
    }

    func refresh() { refreshes += 1 }
}
