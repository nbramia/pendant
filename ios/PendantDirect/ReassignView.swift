import SwiftUI

/// The paragraph (or speaker) the wearer tapped in a transcript.
struct ReassignTarget: Identifiable {
    let id = UUID()
    let tag: String
    /// Stamped-paragraph index, or nil when the line can't be addressed alone.
    let paragraph: Int?
    let line: String
    /// What the transcript shows now (may be ahead of the server's copy).
    var shown: String? = nil
    /// The transcript row that was tapped (where the Undo row appears afterwards).
    var row: Int? = nil
}

/// Who is speaking? The wearer picks where the change applies (this line, the whole voice in this
/// conversation, or that voice in all past conversations), then a name. "This conversation" covers the
/// sitting (the neighbouring pieces of the same real conversation, matched by voice) and says how much
/// ("This conversation (1 hr, 4 parts)"). The calendar's likely
/// person (from the conversation's event) comes first with a one-tap "Yes, <name> · this
/// conversation" that applies the selected scope at once; when the calendar offers nobody, an
/// unnamed voice gets LifeOS's first "maybe: <name>?" with the same one tap. Below it, known people (the event's
/// other attendees first, then LifeOS's other maybes with their reasons, then most recognized) and a new
/// person save on the first tap for this line or this conversation. With "All past conversations", and for
/// Remove name, the pick waits for the confirm button, which names the scope ("Change all past
/// conversations…", "Remove name · this line"). Remove name makes the speaker unknown again (a fresh "Speaker X" on the server) and
/// never applies to all past conversations. "All past conversations" shows a preview to confirm
/// first, from either button. A scope that can't apply here stays visible with its reason and
/// can't be picked; the sheet never switches the scope by itself. The answer also teaches
/// the server's voiceprints (`POST /pendant/feed/conversations/<device>/<start_ms>/speaker`).
struct ReassignSheet: View {
    let conversation: Conversation
    let target: ReassignTarget
    /// Called after the server accepted the answer: (scope, the label the transcript now shows, the
    /// server's receipt for Undo, whether it was Remove name).
    let saved: (ReassignScope, String, ReassignReceipt?, Bool) async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var choice: ScopeChoice
    @State private var picked: Pick?
    @State private var confirming: PendingAll?
    @State private var roster: SpeakerRoster?
    @State private var newName = ""
    @State private var submit = 0
    @State private var saving = false
    @State private var failed = false
    /// Who this voice probably is, from the conversation's calendar event.
    @State private var hint = CalendarHint.none
    @FocusState private var typing: Bool

    init(conversation: Conversation, target: ReassignTarget,
         saved: @escaping (ReassignScope, String, ReassignReceipt?, Bool) async -> Void) {
        self.conversation = conversation
        self.target = target
        self.saved = saved
        _choice = State(initialValue: ScopeChoice(lineOK: target.paragraph != nil,
                                                  voiceOK: conversation.speakerTags?[target.tag] != nil))
    }

    /// The name picked (or Remove name), waiting for the confirm button.
    struct Pick: Equatable {
        let label: String
        var entityId: String? = nil
        var remove = false
        static let removeName = Pick(label: "", remove: true)
    }

    /// The answer waiting for the wearer to confirm "All past conversations".
    struct PendingAll: Identifiable, Hashable {
        let id = UUID()
        let name: String
        var entityId: String? = nil
        let preview: ReassignPreview
        /// True when the server found the matches had changed since the first preview.
        var changed = false
    }
    private var people: [String] {
        let enrolled = (roster?.enrolled ?? []).map {
            PickerOrder.Person(name: $0.name, recognized: $0.recognized ?? 0, count: $0.count)
        }
        return PickerOrder.names(enrolled: enrolled, allowed: roster?.allowed ?? [])
    }
    private var isSample: Bool { conversation.deviceId == Conversation.tourSampleDevice }
    /// The name the transcript shows for the tapped voice now.
    private var shown: String { target.shown ?? target.tag }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("“\(target.line)”").font(TypeScale.subheadline).italic().foregroundStyle(Theme.textSecondary).lineLimit(3)
                }
                .surfaceRows()
                Section {
                    ForEach(choice.options) { option in
                        ScopeRow(option: option, title: choice.title(option.scope),
                                 selected: choice.selected == option.scope) { choice.select(option.scope) }
                    }
                    if choice.selected == .all {
                        Text("You'll see what it would change first. Nothing is saved until you confirm.")
                            .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                    }
                } header: { SectionLabel("Apply to") }
                .surfaceRows()
                if let likely = hint.likely(for: shown), let title = choice.likelyTitle(likely.name) {
                    Section {
                        // one tap applies the selected scope (all past conversations still shows its preview)
                        LikelyConfirm(person: likely, title: title) {
                            apply(Pick(label: likely.label, entityId: likely.entityId))
                        }
                        .padding(.vertical, Spacing.xs)
                    }
                    .surfaceRows()
                } else if let maybe = hint.maybeChip(for: shown), let title = choice.likelyTitle(maybe.name) {
                    Section {
                        // LifeOS's best guess for an unnamed voice: the same one tap, the same hand label
                        LikelyConfirm(maybe: maybe, title: title) {
                            apply(Pick(label: maybe.label, entityId: maybe.entityId))
                        }
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
                // above the people list, which can run long: never buried under it
                Section {
                    RemoveNameRow(picked: picked?.remove == true) { pickRemove() }
                }
                .surfaceRows()
                Section {
                    SuggestionRows(query: newName, fallback: people, current: target.shown ?? target.tag,
                                   picked: picked?.label, lead: hint.pickerLead(for: shown), submitTick: submit) { s in
                        pick(s.label, entityId: s.entityId)
                    }
                } header: { SectionLabel("Who is it?") }
                .surfaceRows()
            }
            .nightList()
            .safeAreaInset(edge: .bottom) {
                // only a pick that waits for a confirm needs the bar (a name for this line or this
                // conversation is saved on the tap); a failed save still says so there
                if choice.confirmsPick || failed { confirmBar }
            }
            .disabled(saving)
            .navigationTitle("Who's \(target.shown ?? target.tag)?")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .navigationDestination(item: $confirming) { pending in
                ReassignAllConfirmView(conversation: conversation, target: target, name: pending.name,
                                       preview: pending.preview, changed: pending.changed) {
                    await send(pending.name, scope: .all, preview: pending.preview, entityId: pending.entityId)
                }
            }
            .task {
                async let coverage: Void = loadSitting()
                if let r = try? await Feed.fetch("speakers", as: SpeakerRoster.self) { roster = r }
                await loadHint()
                await coverage
            }
        }
        .presentationDetents([.large])
        .preferredColorScheme(.dark)
        .tint(Theme.teal)
    }

    /// The picked name and the confirm button, which names the selected scope.
    private var confirmBar: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if failed {
                Label("Couldn't save. Is Tailscale on?", systemImage: "wifi.exclamationmark")
                    .font(TypeScale.callout).foregroundStyle(Theme.warning)
            }
            // Remove name says what it does on its own row and button: no line here
            if picked?.remove != true && choice.confirmsPick {
                Text(picked.map { "To \($0.label)" } ?? "Pick a name")
                    .font(TypeScale.subheadline).foregroundStyle(picked == nil ? Theme.textSecondary : Theme.textPrimary)
                    .lineLimit(1)
                    .accessibilityIdentifier("reassign-picked")
            }
            if choice.confirmsPick {
                Button { if let picked { apply(picked) } } label: {
                    if saving { ProgressView().tint(choice.removing ? Theme.destructive : Theme.onAccent) }
                    else { Text(choice.confirmTitle) }
                }
                .buttonStyle(PendantButtonStyle(kind: choice.removing ? .destructive : .primary, size: .large))
                .disabled(picked == nil || choice.selected == nil || saving)
                .accessibilityIdentifier("reassign-confirm")
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.top, Spacing.m)
        .padding(.bottom, Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.sunken.ignoresSafeArea(edges: .bottom))
    }

    /// A name from the picker: saved at once for this line or this conversation; with "All past
    /// conversations" it waits for the confirm button.
    private func pick(_ name: String, entityId: String? = nil) {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        picked = Pick(label: n, entityId: entityId)
        choice.setRemoving(false)
        failed = false
        typing = false
        if !choice.confirmsPick, let picked { apply(picked) }
    }

    /// Remove name: waits for the confirm, which names the scope ("all past conversations" can't apply).
    private func pickRemove() {
        picked = .removeName
        choice.setRemoving(true)
        failed = false
        typing = false
    }

    /// The calendar suggestion for this voice (nothing when there's no match or the server can't say).
    private func loadHint() async {
        guard let device = conversation.deviceId, let start = conversation.startMs else { return }
        if isSample {   // screen tour only: an inert sample (the calendar's, or LifeOS's maybes without an event)
            hint = conversation.calendar == nil ? .tourSampleMaybe : .tourSample
            return
        }
        let path = Suggestions.hintPath(deviceId: device, startMs: start, tag: target.tag)
        hint = (try? await Feed.fetch(path, as: CalendarHint.self, cached: false)) ?? .none
    }

    /// How much "This conversation" covers for this voice (the plain title when the server can't say).
    private func loadSitting() async {
        guard choice.option(.conversation).enabled, let device = conversation.deviceId,
              let start = conversation.startMs else { return }
        if isSample {   // screen tour only: an inert sample hour in four parts
            choice.sitting = .tourSample
            return
        }
        let path = SittingScope.path(deviceId: device, startMs: start, tag: target.tag)
        if let s = try? await Feed.fetch(path, as: SittingScope.self, cached: false) { choice.sitting = s }
    }

    /// Applies a name to the selected scope ("All past conversations" opens its preview first).
    private func apply(_ picked: Pick) {
        guard !saving, let scope = choice.selected,
              let device = conversation.deviceId, let start = conversation.startMs else { return }
        self.picked = picked
        if !picked.remove { choice.setRemoving(false) }
        saving = true
        failed = false
        Task {
            if picked.remove {
                _ = await sendRemove(scope: scope)
            } else if scope == .all {
                do {
                    // the tour's inert sample has nothing to preview
                    let preview = isSample ? ReassignPreview(conversations: 0, days: 0, words: 0, sample: [])
                        : try await loadPreview(picked.label, device: device, start: start)
                    confirming = PendingAll(name: picked.label, entityId: picked.entityId, preview: preview)
                } catch {
                    failed = true
                }
            } else {
                _ = await send(picked.label, scope: scope, entityId: picked.entityId)
            }
            saving = false
        }
    }

    private func loadPreview(_ name: String, device: String, start: Int) async throws -> ReassignPreview {
        let path = Reassign.previewPath(deviceId: device, startMs: start, tag: target.tag, name: name)
        return try await Feed.fetch(path, as: ReassignPreview.self, cached: false)
    }

    /// Records the answer; true when the server accepted it (the sheet then closes). When the
    /// server says the matches changed since the preview (409), the preview is fetched again
    /// and shown with its new counts instead.
    private func send(_ name: String, scope: ReassignScope, preview: ReassignPreview? = nil,
                      entityId: String? = nil) async -> Bool {
        guard let device = conversation.deviceId, let start = conversation.startMs else { return false }
        let req = Reassign.request(deviceId: device, startMs: start, scope: scope, tag: target.tag,
                                   paragraph: target.paragraph, name: name, entityId: entityId, preview: preview)
        do {
            let receipt = try await Feed.reassign(deviceId: device, path: req.path, json: req.body)
            await saved(scope, name, receipt, false)
            dismiss()
            return true
        } catch let e as Feed.HTTPStatus where e.code == 409 && scope == .all {
            if let fresh = try? await loadPreview(name, device: device, start: start) {
                confirming = PendingAll(name: name, entityId: entityId, preview: fresh, changed: true)
            } else {
                failed = true
            }
            return true      // not an error to report: the screen shows the new numbers
        } catch {
            failed = true
            return false
        }
    }
}

extension ReassignSheet {
    /// Remove name for this line or this conversation; true when the server accepted it (the sheet
    /// then closes). The transcript's new label is the server's fresh "Speaker X".
    private func sendRemove(scope: ReassignScope) async -> Bool {
        guard let device = conversation.deviceId, let start = conversation.startMs,
              let req = Reassign.removeRequest(deviceId: device, startMs: start, scope: scope, tag: target.tag,
                                               paragraph: target.paragraph) else { return false }
        do {
            let receipt = try await Feed.reassign(deviceId: device, path: req.path, json: req.body)
            let used = Set(conversation.paragraphs.compactMap(\.speaker)).union((conversation.speakerTags ?? [:]).keys)
            let name = receipt?.name ?? (scope == .conversation
                ? Reassign.freshTag(avoiding: used.subtracting([target.tag]), keeping: target.tag)
                : Reassign.freshTag(avoiding: used))
            await saved(scope, name, receipt, true)
            dismiss()
            return true
        } catch {
            failed = true
            return false
        }
    }
}

/// One "Apply to" option: a checkmark when selected; when it can't apply here, dimmed with its
/// reason underneath, and tapping it changes nothing.
struct ScopeRow: View {
    let option: ScopeChoice.Option
    /// The option's title ("This conversation (1 hr, 4 parts)" says how much it covers).
    let title: String
    let selected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: Spacing.xxs) {
                    Text(title).foregroundStyle(option.enabled ? Theme.textPrimary : Theme.unavailableTitle)
                    if let reason = option.reason {
                        Text(reason).font(TypeScale.caption).foregroundStyle(Theme.unavailableReason)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("scope-reason-\(option.scope.rawValue)")
                    }
                }
                Spacer(minLength: Spacing.s)
                if selected { Image(systemName: "checkmark").foregroundStyle(Theme.teal) }
            }
            .padding(.vertical, Spacing.xs)
            .frame(maxWidth: .infinity, minHeight: minTapTarget, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("scope-\(option.scope.rawValue)")
        .accessibilityValue(selected ? "Selected" : option.enabled ? "" : "Unavailable")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// "All past conversations": what the change would touch, a sample to look at, and the
/// confirm button. Nothing has changed on the server until it is pressed.
struct ReassignAllConfirmView: View {
    let conversation: Conversation
    let target: ReassignTarget
    let name: String
    let preview: ReassignPreview
    var changed = false
    let confirm: () async -> Bool
    @State private var working = false
    @State private var failed = false

    var body: some View {
        List {
            if changed {
                Section {
                    Label("Conversations changed while you were looking. These are the new numbers; nothing was saved.",
                          systemImage: "arrow.triangle.2.circlepath").foregroundStyle(Theme.warning)
                }
                .surfaceRows()
            }
            Section {
                if preview.isEmpty {
                    Text("No other past conversation has a voice that sounds like this one and isn't already settled.")
                        .foregroundStyle(Theme.textSecondary)
                } else {
                    Text("Voices like \(target.tag) in other conversations become \(name): \(preview.summary).")
                        .foregroundStyle(Theme.textPrimary)
                }
                if let skipped = preview.skipped, skipped > 0 {
                    Text("\(skipped) matching \(skipped == 1 ? "voice was" : "voices were") skipped because another voice in the same conversation shares its name.")
                        .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                }
                Text("This voice here is saved too. Only it teaches \(name)'s voiceprint; the others just get the name.")
                    .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
            }
            .surfaceRows()
            if !preview.sample.isEmpty {
                ForEach(preview.sampleByDay, id: \.date) { group in
                    Section {
                        ForEach(group.items) { item in
                            NavigationLink {
                                SampleConversationView(sample: item)
                            } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack {
                                        Text(item.title.isEmpty ? item.time : "\(item.time)  \(item.title)").font(TypeScale.label)
                                            .foregroundStyle(Theme.textPrimary)
                                        Spacer()
                                        Text("\(Int((item.similarity * 100).rounded()))%").font(.caption.monospacedDigit()).foregroundStyle(Theme.textSecondary)
                                    }
                                    Text("\(item.label): \(item.excerpt)").font(TypeScale.caption).foregroundStyle(Theme.textSecondary).lineLimit(2)
                                }
                            }
                        }
                    } header: { SectionLabel(Self.pretty(group.date)) }
                    .surfaceRows()
                }
                if preview.conversations > preview.sample.count {
                    Text("Showing \(preview.sample.count) of \(preview.conversations) conversations.")
                        .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                        .listRowBackground(Color.clear)
                }
            }
            Section {
                Button {
                    working = true
                    failed = false
                    Task {
                        let ok = await confirm()
                        if !ok { failed = true }
                        working = false
                    }
                } label: {
                    Text(preview.isEmpty ? "Save just this voice" : preview.confirmTitle)
                }
                .buttonStyle(.primary(.large))
                .disabled(working)
                if failed {
                    Label("Couldn't save. Is Tailscale on?", systemImage: "wifi.exclamationmark").foregroundStyle(Theme.warning)
                }
            }
            .listRowBackground(Color.clear)
        }
        .nightList()
        .navigationTitle("All past conversations")
        .navigationBarTitleDisplayMode(.inline)
    }

    static func pretty(_ iso: String) -> String {
        let p = DateFormatter(); p.dateFormat = "yyyy-MM-dd"
        guard let d = p.date(from: iso) else { return iso }
        let f = DateFormatter(); f.dateFormat = "EEE, MMM d"
        return f.string(from: d)
    }
}

/// A sample conversation, opened with the lines the relabel would change marked.
struct SampleConversationView: View {
    let sample: ReassignPreview.Sample
    @State private var conversation: Conversation?
    @State private var failed = false

    var body: some View {
        Group {
            if let conversation {
                ConversationDetail(conversation: conversation, date: sample.date, highlight: Set(sample.paragraphs))
            } else {
                ZStack {
                    Theme.night.ignoresSafeArea()
                    if failed {
                        Label("Couldn't load this conversation", systemImage: "wifi.exclamationmark").foregroundStyle(Theme.textSecondary)
                    } else {
                        ProgressView()
                    }
                }
                .navigationTitle(sample.time)
                .navigationBarTitleDisplayMode(.inline)
            }
        }
        .task {
            do {
                let day = try await Feed.fetch("day/\(sample.date)", as: DayFeed.self)
                conversation = day.conversations.first { $0.deviceId == sample.deviceId && $0.startMs == sample.startMs }
                failed = conversation == nil
            } catch {
                failed = true
            }
        }
    }
}
