import SwiftUI

/// The one name picker every tagging screen shares. Rows are live LifeOS people (full name,
/// circle badge, category), then known voices, with the typed text as a new name only as the
/// last row. Picking a row hands back the suggestion; a LifeOS person's `entityId` is what
/// links the name, and `label` is the speaker name to save. If the server can't be asked,
/// `fallback` names are listed instead. Use inside a `List` section.
struct SuggestionRows: View {
    let query: String
    /// The plain known names, shown when the suggestions can't be fetched.
    var fallback: [String] = []
    /// A name to leave out (a guess the wearer just rejected).
    var excluding: String? = nil
    /// The name that is current now (gets a checkmark).
    var current: String? = nil
    /// The row picked but not yet confirmed (gets a filled checkmark; it replaces `current`'s).
    var picked: String? = nil
    /// Only LifeOS people can be picked (re-pointing a link): no known-only or new-name rows.
    var linkOnly = false
    /// People to list first (a calendar event's remaining attendees, then LifeOS's maybes), when they
    /// match what was typed.
    var lead: [PersonSuggestion] = []
    /// People already offered as one-tap buttons above the picker: left out of the rows.
    var hidden: [PersonSuggestion] = []
    /// Asked instead when `query` finds nobody to list (the relink picker's prefilled name falls back to
    /// the default list).
    var orElse: String? = nil
    /// Bumped by the name field's submit key: picks the first row when it is exactly what was typed.
    var submitTick = 0
    let pick: (PersonSuggestion) -> Void
    @State private var state = SuggestionState()

    private var shown: [PersonSuggestion] {
        state.shown(query: query, fallback: fallback, lead: lead, linkOnly: linkOnly, hidden: hidden, excluding: excluding)
    }

    var body: some View {
        // A modifier on an empty Group never runs, so the .task below would never load anything:
        // until the first answer there is always a row (the loading line).
        Group {
            if !state.loaded && shown.isEmpty {
                Text("Loading people…").font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                    .accessibilityIdentifier("suggestions-loading")
            }
            ForEach(shown) { s in
                Button { pick(s) } label: {
                    RowLabel(title: s.isNew ? "Add “\(s.name)” as a new name" : s.name,
                             subtitle: s.isNew ? "" : s.subtitle) {
                        if let badge = s.circleBadge {
                            Text(badge).font(TypeScale.micro.weight(.semibold)).foregroundStyle(Theme.teal)
                                .padding(.horizontal, Spacing.s).padding(.vertical, Spacing.xxs)
                                .background(Theme.accentWash, in: Capsule())
                        }
                        if let picked {
                            if s.label.caseInsensitiveCompare(picked) == .orderedSame {
                                Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.teal)
                                    .accessibilityLabel("Picked")
                            } else if s.isNew {
                                Image(systemName: "plus.circle").foregroundStyle(Theme.teal)
                            }
                        } else if let current, s.known, s.label.caseInsensitiveCompare(current) == .orderedSame {
                            Image(systemName: "checkmark").foregroundStyle(Theme.teal)
                        } else if s.isNew {
                            Image(systemName: "plus.circle").foregroundStyle(Theme.teal)
                        }
                    }
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier(s.isNew ? "suggestion-new" : s.known ? "suggestion-tagged-\(s.label)"
                                         : "suggestion-lifeos-\(s.label)")
            }
            if state.lifeosDown || state.failed {
                Text(state.failed ? "Couldn't load suggestions." : "LifeOS isn't answering; showing known voices only.")
                    .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
            }
        }
        .task(id: query) { await load() }
        .onChange(of: submitTick) { _, _ in
            if let s = Suggestions.exactFirst(shown, query: query) { pick(s) }
        }
    }

    private func load() async {
        // wait for a pause in typing; a newer query cancels this task
        if !query.isEmpty { try? await Task.sleep(for: .milliseconds(250)) }
        if Task.isCancelled { return }
        do {
            state.received(try await Feed.fetch(Suggestions.path(query), as: SuggestionResponse.self, cached: false))
            if let orElse, shown.isEmpty {
                state.received(try await Feed.fetch(Suggestions.path(orElse), as: SuggestionResponse.self, cached: false))
            }
        } catch {
            if !Task.isCancelled { state.loadFailed() }
        }
    }
}
