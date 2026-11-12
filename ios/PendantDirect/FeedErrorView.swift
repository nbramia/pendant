import SwiftUI

/// The card every feed screen shows when its load failed: what actually went wrong
/// (`FeedProblem.message`), not a guess about Tailscale.
struct FeedErrorCard: View {
    let problem: FeedProblem
    /// False inside a `.surfaceRows()` row, which is already the card.
    var framed = true
    /// Asks again (the "Try again" button).
    let retry: () async -> Void

    var body: some View {
        if framed { Card { content } } else { content }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Label(problem.message, systemImage: problem.kind == .busy ? "arrow.triangle.2.circlepath" : "wifi.exclamationmark")
                .font(TypeScale.headline)
            Button("Try again") { Task { await retry() } }
                .buttonStyle(.secondary(.small))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("feed-error")
    }
}

extension View {
    /// While `problem` is set the screen asks again: when the app returns to the foreground, and
    /// every 10 s while it stays (a restart or a deploy ends within seconds; a lost connection
    /// can come back without anyone touching the phone).
    func retryWhileFailed(_ problem: FeedProblem?, _ reload: @escaping () async -> Void) -> some View {
        modifier(RetryWhileFailed(failed: problem != nil, reload: reload))
    }
}

private struct RetryWhileFailed: ViewModifier {
    let failed: Bool
    let reload: () async -> Void
    @Environment(\.scenePhase) private var phase

    func body(content: Content) -> some View {
        content
            .onChange(of: phase) { _, new in
                if new == .active && failed { Task { await reload() } }
            }
            .task(id: failed) {
                while failed && !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(10))
                    if failed && !Task.isCancelled { await reload() }
                }
            }
    }
}
