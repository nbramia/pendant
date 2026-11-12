import Charts
import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            Tab("Today", systemImage: "waveform") { TodayView() }
            Tab("Search", systemImage: "magnifyingglass") { SearchView() }
            Tab("History", systemImage: "calendar") { HistoryView() }
            Tab("Pendants", systemImage: "circle.circle") { PendantsView() }
        }
        .tint(Theme.teal)
        .preferredColorScheme(.dark)
    }
}

// MARK: - Today

struct TodayView: View {
    @EnvironmentObject var pm: PendantManager
    @State private var scoring = false
    var body: some View {
        NavigationStack {
            DayView(date: Feed.todayString(), showsLiveHeader: true)
                .navigationTitle(greeting)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { scoring = true } label: {
                            Label("Voice Scoring", systemImage: "person.crop.rectangle.stack")
                        }
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        NavigationLink(value: AppRoute.people) { Label("People", systemImage: "person.2") }
                    }
                }
                .fullScreenCover(isPresented: $scoring) { VoiceScoringView() }
                .navigationDestination(for: AppRoute.self) { $0.destination }
        }
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<12: "Good morning"
        case 12..<17: "Good afternoon"
        default: "Good evening"
        }
    }
}

/// One day's transcripts: optional live header, stats, hourly chart, timeline.
struct DayView: View {
    @EnvironmentObject var pm: PendantManager
    let date: String
    var showsLiveHeader = false
    @State private var feed: DayFeed?
    /// The conversation a tapped row opens.
    @State private var opened: Conversation?
    @State private var loadError: FeedProblem?
    @State private var query = ""
    /// Rows swiped away: shown as "Deleted · Undo" while their Undo window is open.
    @State private var undoRows: [String: Conversation] = [:]
    /// Rows deleted here, kept out of view until the feed stops listing them.
    @State private var hidden: Set<String> = []
    /// Each delete's request, so Undo can wait for it before restoring.
    @State private var deletions: [String: Task<Bool, Never>] = [:]
    /// App-star changes shown until the reloaded feed agrees.
    @State private var starShown: [String: Bool] = [:]
    /// Whether this day's folded short clips are showing (resets on relaunch).
    @State private var clipsExpanded = false
    private let refresh = Timer.publish(every: 60, on: .main, in: .common).autoconnect()

    var body: some View {
        // One native List: header, chart, Who's this card and rows scroll together, and the
        // system handles the horizontal/vertical direction lock for row swipes.
        List {
            // While a query is typed the screen is only its matches: no header, stats, chart,
            // Who's this card or review filter (they come back when the query is cleared).
            if !SearchOnly.active(query) {
                if showsLiveHeader { plainRow { LiveHeader() } }
                if showsLiveHeader {
                    plainRow { WhosThisCard() }
                }
                plainRow { stats.accessibilityElement(children: .contain).accessibilityIdentifier("day-stats") }
                if let feed, feed.hourlyMinutes.contains(where: { $0 > 0 }) { plainRow { HourlyChart(minutes: feed.hourlyMinutes) } }
            }
            timeline
            Color.clear.frame(height: 24).plain()
        }
        .listStyle(.plain)
        .tabBarClearance()
        .navigationDestination(for: Conversation.self) { ConversationDetail(conversation: $0, date: date) }
        .navigationDestination(item: $opened) { ConversationDetail(conversation: $0, date: date) }
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 1)
        .background(Theme.night.ignoresSafeArea())
        .searchable(text: $query, prompt: "Search what was said")
        .refreshable { await load() }
        .task { await load() }
        .onReceive(refresh) { _ in Task { await load() } }
        .retryWhileFailed(loadError) { await load() }
    }

    /// A list row with no background, separator or card chrome of its own.
    private func plainRow<C: View>(@ViewBuilder _ content: () -> C) -> some View {
        content().plain(top: 0, bottom: 16)
    }

    private var stats: some View {
        HStack(spacing: 12) {
            StatChip(value: feed.map { String(format: "%.0f", $0.talkMinutes) } ?? "–", label: "minutes", symbol: "waveform")
            StatChip(value: feed.map { "\($0.conversations.count)" } ?? "–", label: "conversations", symbol: "bubble.left.and.bubble.right")
            StatChip(value: feed.map { $0.words >= 1000 ? String(format: "%.1fk", Double($0.words) / 1000) : "\($0.words)" } ?? "–",
                     label: "words", symbol: "text.quote")
        }
    }

    @ViewBuilder private var timeline: some View {
        let listed = feed?.conversations ?? []
        let keys = Set(listed.compactMap(\.rowKey))
        // Rows swiped away stay in place (as "Deleted · Undo").
        let convs = (listed.filter { c in c.rowKey.map { !hidden.contains($0) || undoRows[$0] != nil } ?? true }
                     + undoRows.values.filter { !keys.contains($0.rowKey ?? "") })
            .filter { SearchOnly.matches($0.text, query) }
            .sorted { $0.time > $1.time }
        if let loadError, feed == nil {
            plainRow { FeedErrorCard(problem: loadError) { await load() } }
        } else if convs.isEmpty {
            plainRow {
                MessageCard(title: !SearchOnly.active(query) ? "No transcripts yet" : "No matches",
                            detail: !SearchOnly.active(query)
                                ? "Conversations appear here a few minutes after a pause."
                                : "Nothing said today matches “\(query)”.")
            }
        } else {
            if SearchOnly.active(query) {
                Text(SearchOnly.countLabel(convs.count)).font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                    .accessibilityIdentifier("match-count")
                    .plain(top: 0, bottom: 8)
            }
            // Short clips fold into one row, except while searching.
            let folds = !SearchOnly.active(query)
            let byId = Dictionary(convs.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            let entries = folds
                ? JunkFold.entries(convs.map { c in
                    JunkFold.Row(id: c.id, isJunk: (c.rowKey.map { undoRows[$0] == nil } ?? true)
                                 && JunkFold.isJunk(kind: c.recordingKind, starCount: c.starCount,
                                                    appStar: appStarred(c), notable: c.notable == true))
                }, expanded: clipsExpanded)
                : convs.map { JunkFold.Entry.item($0.id) }
            ForEach(entries) { entry in
                switch entry {
                case let .toggle(count, expanded):
                    FoldRow(count: count, expanded: expanded) {
                        withAnimation(.snappy) { clipsExpanded.toggle() }
                    }
                    .plain()
                case let .item(id):
                    if let c = byId[id] { conversationRow(c) }
                case let .clip(id):
                    if let c = byId[id] { conversationRow(c, grouped: true) }
                }
            }
        }
    }

    @ViewBuilder private func conversationRow(_ c: Conversation, grouped: Bool = false) -> some View {
        if let key = c.rowKey, undoRows[key] != nil {
            DeletedRow { undo(c) }
                .transition(.opacity)
                .plain()
        } else {
            Button { opened = c } label: {
                TimelineRow(conversation: c, query: query, appStar: appStarred(c), grouped: grouped)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            // the screen tour's inert samples are told apart so the tour never opens one as a real conversation
            .accessibilityIdentifier(c.deviceId == Conversation.tourSampleDevice ? "tour-sample-\(c.time)" : "conversation-\(c.time)")
            .plain(top: 0, bottom: 0)
                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                    if c.rowKey != nil {
                        Button { star(c) } label: {
                            Label(appStarred(c) ? "Remove star" : "Star",
                                  systemImage: appStarred(c) ? "star.slash.fill" : "star.fill")
                        }
                        .tint(Theme.swipeStar)
                    }
                }
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    if c.rowKey != nil {
                        Button { delete(c) } label: { Label("Delete", systemImage: "trash.fill") }
                            .tint(Theme.swipeDelete)
                    }
                }
                .accessibilityActions {
                    if c.rowKey != nil {
                        Button(appStarred(c) ? "Remove star" : "Star") { star(c) }
                        Button("Delete") { delete(c) }
                    }
                }
        }
    }

    private func load() async {
        do {
            var f = try await Feed.fetch("day/\(date)", as: DayFeed.self)
            if ScreenTour.samples { f = f.withTourClips() }
            withAnimation(.snappy) {
                feed = f
                let listed = Set(f.conversations.compactMap(\.rowKey))
                hidden = hidden.filter { listed.contains($0) || undoRows[$0] != nil }
                for c in f.conversations {
                    if let key = c.rowKey, starShown[key] == (c.appStar ?? false) { starShown[key] = nil }
                }
            }
            loadError = nil
        } catch {
            if !(error is CancellationError) { loadError = FeedProblem(error) }
        }
    }

    private func appStarred(_ c: Conversation) -> Bool {
        c.rowKey.flatMap { starShown[$0] } ?? c.appStar ?? false
    }

    /// Swipe right: star it from the app, or take that star off. Shown at once; put back if the server says no.
    private func star(_ c: Conversation) {
        guard let key = c.rowKey, let device = c.deviceId, let start = c.startMs else { return }
        let on = RowRequest.starBody(appStarred: appStarred(c))["on"] ?? true
        withAnimation(.snappy) { starShown[key] = on }
        Task {
            do {
                try await Feed.star(deviceId: device, startMs: start, on: on)
                await load()
            } catch {
                withAnimation(.snappy) { starShown[key] = nil }
            }
        }
    }

    /// Swipe left: delete at once, no dialog. The row shows "Deleted · Undo" for a few seconds;
    /// if the server couldn't delete it, the row comes back.
    private func delete(_ c: Conversation) {
        guard let key = c.rowKey, let device = c.deviceId, let start = c.startMs else { return }
        withAnimation(.snappy) {
            undoRows[key] = c
            hidden.insert(key)
        }
        let request = Task { () -> Bool in
            (try? await Feed.delete(deviceId: device, startMs: start)) != nil
        }
        deletions[key] = request
        Task {
            let ok = await request.value
            if !ok {
                withAnimation(.snappy) {
                    undoRows[key] = nil
                    hidden.remove(key)
                }
                return
            }
            try? await Task.sleep(for: .seconds(UndoWindow.seconds))
            guard deletions[key] == request else { return }   // undone meanwhile
            deletions[key] = nil
            withAnimation(.snappy) { undoRows[key] = nil }
            await load()
        }
    }

    private func undo(_ c: Conversation) {
        guard let key = c.rowKey, let device = c.deviceId, let start = c.startMs,
              let request = deletions.removeValue(forKey: key) else { return }
        Task {
            if await request.value {
                guard (try? await Feed.restore(deviceId: device, startMs: start)) != nil else {
                    // still in Recently deleted; leave it deleted here too
                    withAnimation(.snappy) { undoRows[key] = nil }
                    return
                }
            }
            withAnimation(.snappy) {
                undoRows[key] = nil
                hidden.remove(key)
            }
            await load()
        }
    }
}

extension DayFeed {
    /// Screen tour only (`-screenTourSamples`): three inert sample clips (swipeable, but Feed sends
    /// no request for them) on a day that has conversations, so the fold row can be reviewed when the live
    /// feed has no short clips, and one sample meeting with a calendar match.
    func withTourClips() -> DayFeed {
        guard !conversations.isEmpty else { return self }
        func clip(_ time: String, _ kind: String, _ text: String) -> Conversation? {
            // A sentinel device id gives the samples swipe actions so the tour can show them;
            // Feed refuses every request for that device, so nothing reaches the server.
            let start = 1_000_000 + Int(time.replacingOccurrences(of: ":", with: ""))!
            let json = #"{"time":"\#(time)","title":"Sample clip","duration_s":9,"text":"\#(text)","words":4,"kind":"\#(kind)","importance":0,"device_id":"\#(Conversation.tourSampleDevice)","start_ms":\#(start)}"#
            return try? JSONDecoder().decode(Conversation.self, from: Data(json.utf8))
        }
        // and an inert sample meeting with a calendar match, for the calendar line and the "Probably …" confirm;
        // its second voice is missing from speaker_tags (a name two voices share), for the scope reasons
        let meetingJSON = #"{"time":"23:58","title":"Sample meeting","duration_s":1500,"text":"[23:58] **Speaker B:** Sample line for the screen tour.\n[23:59] **Sample Guest:** A sample voice whose name another voice shares.","words":15,"kind":"conversation","importance":1,"device_id":"\#(Conversation.tourSampleDevice)","start_ms":1002358,"speaker_tags":{"Speaker B":0},"meetings":[{"note":"Sample week-ahead prep","source":"granola"}],"calendar":{"title":"Sample week-ahead prep","start_ms":0,"end_ms":1800000,"attendees":[{"name":"the wearer","wearer":true},{"name":"Sample Attendee","entity_id":"tour-sample"}]}}"#
        let meeting = try? JSONDecoder().decode(Conversation.self, from: Data(meetingJSON.utf8))
        let samples = [clip("09:12", "fragment", "Sample fragment for the screen tour."),
                       clip("09:41", "media", "Sample background audio for the screen tour."),
                       clip("11:03", "fragment", "Another sample fragment."), meeting].compactMap { $0 }
        return DayFeed(date: date, conversations: conversations + samples, hourlyMinutes: hourlyMinutes,
                       talkMinutes: talkMinutes, words: words)
    }
}

extension View {
    /// Strips a List row to bare content: no separator, no background, card-width insets.
    func plain(top: CGFloat = 0, bottom: CGFloat = 0) -> some View {
        self.listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: top, leading: 16, bottom: bottom, trailing: 16))
    }
}

/// The day's folded short clips as one slim line: "N hidden · Show", or "Hide" while open.
/// It carries the timeline line through, so the clips grouped under it stay on the timeline.
struct FoldRow: View {
    let count: Int
    let expanded: Bool
    let action: () -> Void
    var body: some View {
        HStack(spacing: 12) {
            Rectangle().fill(Theme.raised).frame(width: 1.5).frame(width: 9)
            Button(action: action) {
                Label(JunkFold.label(count: count, expanded: expanded), systemImage: expanded ? "eye" : "eye.slash")
            }
            .buttonStyle(.tertiary(.small))
            .accessibilityIdentifier("short-clips-row")
            .accessibilityHint(expanded ? "Hides the short clips" : "Shows the short clips")
            Spacer(minLength: 0)
        }
    }
}

/// What a swiped-away row leaves behind for a few seconds.
struct DeletedRow: View {
    let onUndo: () -> Void
    var body: some View {
        HStack(spacing: Spacing.s) {
            Label("Deleted", systemImage: "trash").font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary)
            Spacer()
            Button(action: onUndo) { Label("Undo", systemImage: "arrow.uturn.backward") }
                .buttonStyle(.tertiary(.small))
        }
        .padding(.leading, 14).padding(.trailing, Spacing.xs)
        .surface(Theme.sunken, radius: Radius.row)
        .padding(.leading, 21)
        .padding(.bottom, 10)
    }
}

/// "Changed to <Name> · Undo" ("Name removed · Undo" after Remove name) under the changed line for
/// a few seconds after a speaker change.
/// While the undo runs it shows progress; after a failure it stays with the reason, and Retry
/// when trying again can help, else Close.
struct ChangeUndoRow: View {
    let offer: UndoOffer
    let onUndo: () -> Void
    let onClose: () -> Void

    var body: some View {
        HStack(spacing: Spacing.s) {
            VStack(alignment: .leading, spacing: Spacing.xxs) {
                Label(offer.title, systemImage: offer.removed ? "person.crop.circle.badge.xmark"
                                                              : "person.crop.circle.badge.checkmark")
                    .font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary).lineLimit(2)
                if case let .failed(f) = offer.phase {
                    Text(f.message).font(TypeScale.caption).foregroundStyle(Theme.warning)
                        .accessibilityIdentifier("undo-change-error")
                }
            }
            Spacer(minLength: Spacing.s)
            switch offer.phase {
            case .offered:
                Button(action: onUndo) { Label("Undo", systemImage: "arrow.uturn.backward") }
                    .buttonStyle(.tertiary(.small))
                    .accessibilityIdentifier("undo-change-button")
            case .undoing:
                ProgressView().tint(Theme.teal).accessibilityLabel("Undoing")
                    .frame(minWidth: minTapTarget, minHeight: minTapTarget)
            case let .failed(f):
                if f.canRetry {
                    Button(action: onUndo) { Label("Retry", systemImage: "arrow.clockwise") }
                        .buttonStyle(.tertiary(.small))
                } else {
                    Button("Close", action: onClose).buttonStyle(.tertiary(.small))
                }
            }
        }
        .padding(.leading, 14).padding(.trailing, Spacing.xs).padding(.vertical, Spacing.xxs)
        .surface(Theme.sunken, radius: Radius.row)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("undo-change")
    }
}

/// Why a line's playback didn't start, with a retry.
struct LineErrorRow: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        HStack(spacing: Spacing.s) {
            Label(message, systemImage: "exclamationmark.triangle").font(TypeScale.caption).foregroundStyle(Theme.warning)
            Spacer(minLength: Spacing.s)
            Button(action: retry) { Label("Retry", systemImage: "arrow.clockwise") }
                .buttonStyle(.tertiary(.small))
                .accessibilityIdentifier("line-retry")
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("line-error")
    }
}

struct LiveHeader: View {
    @EnvironmentObject var pm: PendantManager
    var body: some View {
        VStack(spacing: 6) {
            PendantOrb(activity: activity)
            Text(headline).font(TypeScale.title3).foregroundStyle(Theme.textPrimary)
            Text(subline).font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 4)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("live-header")
    }

    private var activity: PendantOrb.Activity {
        let s = pm.state
        if s == "live" { return .listening }
        if s.hasPrefix("draining") { return .syncing }
        if s == "quiet" { return .quiet }
        if s.hasPrefix("discovering") || s.hasPrefix("connected") || s.hasPrefix("scanning") { return .connecting }
        return .waiting
    }

    private var headline: String {
        switch activity {
        case .listening: "Listening"
        case .quiet: "Connected · not hearing anything"
        case .syncing: "Catching up"
        case .connecting: "Connecting…"
        case .waiting: pm.pairedCount == 0 ? "No pendant yet" : "Pendant not nearby"
        }
    }

    private var subline: String {
        if pm.pairedCount == 0 { return "Add one on the Pendants tab." }
        if pm.connectedDevices.count > 1 {
            let names = pm.connectedDevices.sorted().map(PendantSnapshot.displayName).joined(separator: " + ")
            return "\(names) connected" + (activity == .listening ? " · audio from \(pm.activeDevice.map(PendantSnapshot.displayName) ?? "one")" : "")
        }
        guard let id = pm.activeDevice, let snap = pm.pendants[id] else {
            return "It keeps recording on its own and syncs when it's back in range."
        }
        var parts = [snap.name]
        if let b = snap.battery { parts.append("\(b)% battery") }
        return parts.joined(separator: " · ")
    }
}

struct HourlyChart: View {
    let minutes: [Double]
    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                Text("When you talked").font(TypeScale.headline)
                Chart(Array(minutes.enumerated()), id: \.offset) { hour, value in
                    BarMark(x: .value("Hour", hour), y: .value("Minutes", value))
                        .foregroundStyle(Theme.teal.gradient)
                        .cornerRadius(3)
                }
                .chartXScale(domain: 0...23)
                .chartXAxis {
                    AxisMarks(values: [0, 6, 12, 18]) { v in
                        AxisValueLabel {
                            if let h = v.as(Int.self) { Text(hourLabel(h)).foregroundStyle(Theme.textSecondary) }
                        }
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(Theme.hairline)
                        AxisValueLabel().foregroundStyle(Theme.textSecondary)
                    }
                }
                .frame(height: 120)
            }
        }
    }

    private func hourLabel(_ h: Int) -> String {
        h == 0 ? "12a" : h < 12 ? "\(h)a" : h == 12 ? "12p" : "\(h - 12)p"
    }
}

struct TimelineRow: View {
    let conversation: Conversation
    let query: String
    var appStar = false
    /// A short clip shown under its fold row: accent bar, tighter spacing.
    var grouped = false
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 0) {
                Circle().fill(Theme.teal).frame(width: 9, height: 9).padding(.top, 6)
                Rectangle().fill(Theme.raised).frame(width: 1.5)
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(conversation.time).font(.headline.monospacedDigit()).foregroundStyle(Theme.textPrimary)
                    Badge(text: conversation.durationLabel)
                    if conversation.starCount > 0 {
                        Badge(text: "\(conversation.starCount)", symbol: "star.fill", tone: .star)
                            .accessibilityLabel("\(conversation.starCount) starred moment\(conversation.starCount == 1 ? "" : "s")")
                    } else if appStar {
                        Badge(text: "", symbol: "star.fill", tone: .star)
                            .accessibilityLabel("Starred")
                    } else if conversation.notable == true {
                        Image(systemName: "star.fill").font(TypeScale.caption).foregroundStyle(Theme.star)
                            .accessibilityLabel("Notable")
                    }
                    if let badge = conversation.recordingKind.badge {
                        Badge(text: badge)
                    }
                    Spacer()
                    if let d = conversation.device {
                        Text(PendantSnapshot.displayName(d)).font(TypeScale.micro).foregroundStyle(Theme.textTertiary)
                    }
                }
                let review = conversation.recordingKind.needsReview
                if conversation.hasRealTitle {
                    Text(conversation.title).font(TypeScale.label)
                        .foregroundStyle(review ? Theme.textSecondary : Theme.textPrimary)
                }
                if let t = conversation.transcriptPreview {
                    chippedPreview(t, font: .subheadline).font(.subheadline)
                        .foregroundStyle(review ? Theme.textTertiary : Theme.textSecondary).lineLimit(3)
                } else {
                    Text(conversation.preview).font(TypeScale.subheadline)
                        .foregroundStyle(review ? Theme.textTertiary : Theme.textSecondary).lineLimit(3)
                }
                if let n = conversation.actionItems?.count, n > 0 {
                    Label("\(n) action item\(n == 1 ? "" : "s")", systemImage: "checklist").font(TypeScale.caption).foregroundStyle(Theme.teal)
                }
                let links = conversation.meetingLinks
                if !links.isEmpty {
                    Label("Also in \(links.map(\.app).joined(separator: " and "))", systemImage: "link")
                        .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            // media and fragments sit back on the sunken surface instead of fading (fading broke contrast)
            .surface(conversation.recordingKind.needsReview ? Theme.sunken : Theme.surface, radius: Radius.row)
            .overlay(alignment: .leading) {
                if grouped { Capsule().fill(Theme.teal).frame(width: 3).padding(.vertical, 12).padding(.leading, 5) }
            }
            .padding(.bottom, grouped ? 6 : 10)
        }
        .contentShape(Rectangle())
    }
}

/// One action item with an "Add to tasks" button. Nothing becomes a LifeOS task
/// unless the wearer taps it; the feed's `tasked` list restores the "Added" state.
struct ActionItemRow: View {
    let item: String
    let conversation: Conversation
    @State private var added = false
    @State private var sending = false
    @State private var failed = false

    /// Mirrors the server's `tasks.clean_item` (collapse whitespace, trim punctuation, 200 chars)
    /// so the raw item text matches the feed's `tasked` entries.
    static func cleaned(_ s: String) -> String {
        let collapsed = s.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        let trimmed = collapsed.trimmingCharacters(in: CharacterSet(charactersIn: " \t.,;:!?\"'"))
        return String(trimmed.prefix(200)).trimmingCharacters(in: .whitespaces)
    }

    private var canAdd: Bool { conversation.deviceId != nil && conversation.startMs != nil }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Label(item, systemImage: added ? "checkmark.circle.fill" : "circle").font(TypeScale.callout)
                .foregroundStyle(Theme.textPrimary)
            Spacer(minLength: Spacing.s)
            if canAdd {
                if added {
                    Text("Added").font(TypeScale.captionStrong).foregroundStyle(Theme.teal)
                } else {
                    Button { add() } label: {
                        if failed { Label("Retry", systemImage: "exclamationmark.arrow.circlepath") } else { Text("Add to tasks") }
                    }
                    .buttonStyle(.secondary(.small))
                    .disabled(sending)
                }
            }
        }
        .onAppear {
            let key = ActionItemRow.cleaned(item)
            added = added || (conversation.tasked ?? []).contains { ActionItemRow.cleaned($0) == key }
        }
    }

    private func add() {
        guard let device = conversation.deviceId, let start = conversation.startMs else { return }
        sending = true
        failed = false
        Task {
            do {
                try await Feed.addToTasks(deviceId: device, startMs: start, item: item)
                added = true
            } catch {
                failed = true
            }
            sending = false
        }
    }
}

struct ConversationDetail: View {
    let conversation: Conversation
    let date: String
    /// Paragraph indexes to mark (the lines a bulk relabel would change).
    var highlight: Set<Int> = []
    /// The conversation as last reloaded after a speaker change.
    @State private var fresh: Conversation?
    @State private var reassigning: ReassignTarget?
    /// New names shown until a reload's transcript agrees (the edit may be queued behind the processor).
    @State private var pending = PendingLabels()
    @StateObject private var player = ConversationPlayer()
    /// The line whose time stamp was tapped: loading, playing or failed.
    @State private var tap = LineTap()
    /// "Changed to <Name> · Undo" / "Name removed · Undo" rows, one per recent speaker change.
    @State private var undos = UndoOffers()
    /// Scrolling follows the line being played until the wearer scrolls by hand.
    @State private var follow = true
    private var current: Conversation { fresh ?? conversation }

    var body: some View {
        let conversation = current
        let starts = lineStarts(conversation)
        let active = player.state == .ready && (player.isPlaying || player.time > 0)
            ? Playback.activeLine(starts: starts, at: player.time) : nil
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if conversation.hasRealTitle {
                    Text(conversation.title).font(TypeScale.title).foregroundStyle(Theme.textPrimary)
                }
                HStack(spacing: 8) {
                    Label(conversation.durationLabel, systemImage: "clock")
                    Label("\(conversation.words) words", systemImage: "text.quote")
                    if let d = conversation.device { Label(PendantSnapshot.displayName(d), systemImage: "circle.circle") }
                }
                .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                if canReassign(conversation), conversation.paragraphs.contains(where: { $0.speaker != nil }) {
                    Text("Tap a name to change who's speaking").font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                }
                if let match = conversation.calendar { CalendarLine(match: match) }
                ForEach(conversation.meetingLinks, id: \.note) { m in MeetingLinkRow(link: m) }
                if canReassign(conversation) { ConversationPlayerBar(player: player, follow: $follow) }
                // rows whose line is gone after a reload show at the top
                ForEach(undos.all.filter { !($0.row.map { conversation.paragraphs.indices.contains($0) } ?? false) }) { undoRow($0) }
                if let o = conversation.overview, !o.isEmpty {
                    Card { Text(o).font(TypeScale.callout) }
                }
                if let items = conversation.actionItems, !items.isEmpty {
                    Card {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Action items", systemImage: "checklist").font(TypeScale.headline)
                            ForEach(items, id: \.self) { item in
                                ActionItemRow(item: item, conversation: conversation)
                            }
                        }
                    }
                }
                ForEach(Array(conversation.paragraphs.enumerated()), id: \.offset) { n, p in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            if p.starred {
                                Image(systemName: "star.fill").font(TypeScale.caption).foregroundStyle(Theme.star)
                                    .accessibilityLabel("Starred")
                            }
                            if let stamp = p.stamp {
                                if canReassign(conversation), player.state != .missing, starts[n] != nil {
                                    Button { follow = true; playLine(n) } label: {
                                        Group {
                                            if tap.isLoading(n) {
                                                // the stamp gives way to a clear loading state until the audio plays
                                                HStack(spacing: 6) {
                                                    ProgressView().controlSize(.small).tint(Theme.teal)
                                                    Text("Loading…")
                                                }
                                                .accessibilityIdentifier("line-loading")
                                            } else {
                                                Label(stamp, systemImage: "play.fill").labelStyle(StampLabelStyle())
                                            }
                                        }
                                        .font(.caption.monospacedDigit().weight(.semibold))
                                        .foregroundStyle(Theme.teal)
                                        .padding(.horizontal, 8).padding(.vertical, 3)
                                        .background(Theme.accentWash, in: Capsule())
                                        .contentShape(Capsule())
                                    }
                                    // an inline chip: wrapped plain, like the speaker chips
                                    .buttonStyle(.plain)
                                    .accessibilityIdentifier("line-stamp")
                                    .accessibilityLabel("Play from \(stamp)")
                                    .accessibilityValue(tap.isLoading(n) ? "Loading" : "")
                                } else {
                                    Text(stamp).font(.caption.monospacedDigit().weight(.semibold)).foregroundStyle(Theme.teal)
                                }
                            }
                            if let speaker = p.speaker {
                                let shown = pending.label(index: p.index, tag: speaker,
                                                          overrides: Set(conversation.lineOverrides ?? []))
                                if canReassign(conversation) {
                                    Button { change(p, row: n, speaker: speaker, shown: shown) } label: { SpeakerChip(name: shown) }
                                        .buttonStyle(.plain)
                                        .accessibilityIdentifier("speaker-chip")
                                        .accessibilityLabel("\(shown), change speaker")
                                } else {
                                    SpeakerChip(name: shown)
                                }
                            }
                        }
                        .contextMenu {
                            if let speaker = p.speaker, canReassign(conversation) {
                                Button("Change speaker…", systemImage: "person.crop.circle.badge.questionmark") {
                                    change(p, row: n, speaker: speaker, shown: pending.label(
                                        index: p.index, tag: speaker, overrides: Set(conversation.lineOverrides ?? [])))
                                }
                                Button("Copy text", systemImage: "doc.on.doc") { UIPasteboard.general.string = p.spoken }
                            }
                        }
                        Text(p.speaker == nil ? markdown(p.text) : AttributedString(p.spoken))
                            .font(TypeScale.body).foregroundStyle(Theme.textPrimary).textSelection(.enabled)
                        if let message = tap.error(on: n) {
                            LineErrorRow(message: message) { playLine(n) }
                        }
                    }
                    .padding(p.starred || marked(p) || n == active ? 10 : 0)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(marked(p) ? Theme.accentWash : n == active ? Theme.playingWash
                                : p.starred ? Theme.starWash : Color.clear,
                                in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .overlay(alignment: .leading) {
                        if marked(p) || n == active { Capsule().fill(Theme.teal).frame(width: 3).padding(.vertical, 4) }
                    }
                    .id(n)
                    ForEach(undos.at(n)) { undoRow($0) }
                }
            }
            .padding()
        }
        .simultaneousGesture(DragGesture(minimumDistance: 8).onChanged { _ in follow = false })
        .tabBarClearance()
        .onChange(of: active) { _, line in
            if follow, let line { withAnimation(.easeInOut(duration: 0.3)) { proxy.scrollTo(line, anchor: .center) } }
        }
        }
        .background(Theme.night.ignoresSafeArea())
        .navigationTitle(conversation.time)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ShareLink(item: "\(date) \(conversation.time)\n\n\(conversation.text)")
        }
        .task(id: conversation.rowKey) {
            if let d = conversation.deviceId, let ms = conversation.startMs, d != Conversation.tourSampleDevice {
                await player.load(device: d, startMs: ms, title: conversation.hasRealTitle ? conversation.title : "Conversation \(conversation.time)",
                                  knownDuration: Double(conversation.durationS))
            }
        }
        .onDisappear { player.stop() }
        .task(id: undos.nextExpiry) {
            // each row goes away by itself at its own time, only while nothing is happening (not during
            // or after a failed undo)
            guard let next = undos.nextExpiry else { return }
            try? await Task.sleep(for: .seconds(max(0, next.timeIntervalSinceNow)))
            if !Task.isCancelled { withAnimation(.snappy) { undos.expire() } }
        }
        .sheet(item: $reassigning) { target in
            ReassignSheet(conversation: conversation, target: target) { scope, name, receipt, removed in
                let change = pending.record(scope: scope, tag: target.tag, paragraph: target.paragraph, name: name)
                if let id = receipt?.undoId() {
                    // the tour's inert sample rows stay longer, so its shot can show two at once
                    let shownFor = id == ReassignReceipt.tourSampleId ? 60 : UndoOffer.shownFor
                    undos.add(UndoOffer(name: name, changeId: id, scope: scope, row: target.row, change: change,
                                        removed: removed, shownFor: shownFor))
                }
                await reload()
            }
        }
    }

    private func undoRow(_ offer: UndoOffer) -> some View {
        ChangeUndoRow(offer: offer, onUndo: { runUndo(offer.id) },
                      onClose: { withAnimation(.snappy) { undos.remove(offer.id) } })
            .transition(.opacity)
    }

    /// Undoes one speaker change on the server, takes its labels back off the screen and reloads.
    /// Other rows keep their own state.
    private func runUndo(_ id: UUID) {
        guard let offer = undos.begin(id) else { return }
        Task {
            do {
                try await Feed.undoReassign(changeId: offer.changeId)
                pending.revert(offer.change)
                withAnimation(.snappy) { undos.remove(id) }
                await reload()
            } catch {
                undos.fail(id, status: FeedProblem(error).status)
            }
        }
    }

    /// Plays from line `n`: the line shows loading until the audio is ready and at its start (the
    /// first tap may wait for the server's transcode). One load at a time: a tap while it runs only
    /// changes which line it plays. A failure shows on the line with a retry.
    private func playLine(_ n: Int) {
        guard tap.tap(n) else { return }
        Task {
            do {
                if current.deviceId == Conversation.tourSampleDevice {
                    // screen tour only: the inert sample has no audio, so its line stays loading for the shot
                    try await Task.sleep(for: .seconds(LineTap.timeout))
                    throw ConversationPlayer.PrepareError.timedOut
                }
                try await player.prepare(timeout: LineTap.timeout)
                while let target = tap.target {
                    let starts = lineStarts(current)
                    guard starts.indices.contains(target), let at = starts[target] else {
                        tap.fail("This line has no place in the audio")
                        return
                    }
                    _ = await player.seekExactly(to: at)
                    if tap.ready(at: target) {
                        player.play(rewindAtEnd: false)
                        return
                    }
                }
            } catch let e as ConversationPlayer.PrepareError {
                tap.fail(e.message)
            } catch {
                tap.fail(ConversationPlayer.PrepareError.failed.message)
            }
        }
    }

    /// Each line's start in the audio (nil where nothing places it).
    private func lineStarts(_ c: Conversation) -> [Double?] {
        Playback.lineStarts(c.paragraphs.map { Playback.Line(index: $0.index, stamp: $0.stamp, words: $0.spoken.split(whereSeparator: \.isWhitespace).count) },
                            offsets: c.paragraphOffsets, startMs: c.startMs)
    }

    private func canReassign(_ c: Conversation) -> Bool { c.deviceId != nil && c.startMs != nil }

    private func change(_ p: Conversation.Paragraph, row: Int, speaker: String, shown: String) {
        reassigning = ReassignTarget(tag: speaker, paragraph: p.index, line: p.spoken, shown: shown, row: row)
    }

    private func marked(_ p: Conversation.Paragraph) -> Bool {
        guard let i = p.index else { return false }
        return highlight.contains(i)
    }

    /// Re-reads the day so the transcript shows the new names.
    private func reload() async {
        guard let day = try? await Feed.fetch("day/\(date)", as: DayFeed.self) else { return }
        let c = conversation
        if let match = day.conversations.first(where: { $0.deviceId == c.deviceId && $0.startMs == c.startMs }) {
            fresh = match
            var tags: [Int: String] = [:]
            for p in match.paragraphs { if let i = p.index, let t = p.speaker { tags[i] = t } }
            pending.reconcile(serverTags: tags, overrides: Set(match.lineOverrides ?? []))
        }
    }
}

/// Renders the processor's `**Speaker:**` labels as bold.
func markdown(_ s: String) -> AttributedString {
    (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
        ?? AttributedString(s)
}

// MARK: - History

struct HistoryView: View {
    @State private var days: [DaySummary] = []
    @State private var loadError: FeedProblem?

    var body: some View {
        NavigationStack {
            List {
                if let loadError, days.isEmpty {
                    FeedErrorCard(problem: loadError, framed: false) { await load() }.surfaceRows()
                }
                NavigationLink(value: AppRoute.trash) {
                    Label("Recently deleted", systemImage: "trash").foregroundStyle(Theme.textPrimary)
                }
                .accessibilityIdentifier("recently-deleted")
                .surfaceRows()
                ForEach(days) { d in
                    NavigationLink(value: d) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(pretty(d.date)).font(TypeScale.headline).foregroundStyle(Theme.textPrimary)
                                Text("\(d.conversations) conversations · \(d.words) words")
                                    .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                            }
                            Spacer()
                            Gauge(value: min(d.talkMinutes, 240), in: 0...240) {
                                EmptyView()
                            } currentValueLabel: {
                                Text("\(Int(d.talkMinutes))").font(.caption2.monospacedDigit()).foregroundStyle(Theme.textPrimary)
                            }
                            .gaugeStyle(.accessoryCircularCapacity)
                            .tint(Theme.teal)
                            .scaleEffect(0.8)
                        }
                    }
                    .accessibilityIdentifier("day-\(d.date)")
                    .surfaceRows()
                }
            }
            .nightList()
            .navigationTitle("History")
            .navigationDestination(for: AppRoute.self) { $0.destination }
            .navigationDestination(for: DaySummary.self) { d in
                DayView(date: d.date).navigationTitle(pretty(d.date))
            }
            .refreshable { await load() }
            .task { await load() }
            .retryWhileFailed(loadError) { await load() }
        }
    }

    private func load() async {
        do { days = try await Feed.fetch("days", as: DaysFeed.self).days; loadError = nil }
        catch { if !(error is CancellationError) { loadError = FeedProblem(error) } }
    }

    private func pretty(_ iso: String) -> String {
        let p = DateFormatter(); p.dateFormat = "yyyy-MM-dd"
        guard let d = p.date(from: iso) else { return iso }
        if Calendar.current.isDateInToday(d) { return "Today" }
        if Calendar.current.isDateInYesterday(d) { return "Yesterday" }
        let f = DateFormatter(); f.dateFormat = "EEEE, MMM d"
        return f.string(from: d)
    }
}

/// Recordings deleted in the last 7 days, with Restore.
struct TrashView: View {
    @State private var items: [TrashEntry] = []
    @State private var loaded = false
    @State private var loadError: FeedProblem?
    @State private var restoring: Set<String> = []
    @State private var failed: Set<String> = []

    var body: some View {
        List {
            if let loadError, items.isEmpty {
                FeedErrorCard(problem: loadError, framed: false) { await load() }.surfaceRows()
            } else if loaded && items.isEmpty {
                Text("Nothing deleted in the last 7 days.").foregroundStyle(Theme.textSecondary)
                    .surfaceRows()
            }
            ForEach(items) { t in
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(t.title ?? "Untitled").font(TypeScale.headline).foregroundStyle(Theme.textPrimary)
                        Text(when(t)).font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                        Text(t.daysLeftLabel()).font(TypeScale.micro).foregroundStyle(Theme.textTertiary)
                    }
                    Spacer()
                    Button { Task { await restore(t) } } label: {
                        if failed.contains(t.id) { Label("Retry", systemImage: "exclamationmark.arrow.circlepath") }
                        else { Text("Restore") }
                    }
                    .buttonStyle(.secondary(.small))
                    .disabled(restoring.contains(t.id))
                }
                .surfaceRows()
            }
        }
        .nightList()
        .navigationTitle("Recently deleted")
        .refreshable { await load() }
        .task { await load() }
        .retryWhileFailed(loadError) { await load() }
    }

    private func load() async {
        do {
            items = try await Feed.fetch("trash", as: TrashFeed.self, cached: false).trash
            loadError = nil
        } catch {
            if !(error is CancellationError) { loadError = FeedProblem(error) }
        }
        loaded = true
    }

    private func restore(_ t: TrashEntry) async {
        restoring.insert(t.id)
        defer { restoring.remove(t.id) }
        do {
            try await Feed.restore(deviceId: t.deviceId, startMs: t.startMs)
            failed.remove(t.id)
            withAnimation(.snappy) { items.removeAll { $0.id == t.id } }
        } catch {
            failed.insert(t.id)
        }
    }

    private func when(_ t: TrashEntry) -> String {
        let f = DateFormatter()
        f.dateFormat = "EEE, MMM d · h:mm a"
        var parts = [f.string(from: t.start)]
        if let d = t.durationS { parts.append(d >= 60 ? "\(d / 60) min" : "\(d) s") }
        if let p = t.device { parts.append(PendantSnapshot.displayName(p)) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Pendants

struct PendantsView: View {
    @EnvironmentObject var pm: PendantManager
    @State private var confirmForget = false
    @State private var scoring = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(pm.pendants.values.sorted { $0.id < $1.id }) { p in
                        PendantRow(snapshot: p, active: pm.connectedDevices.contains(p.id),
                                   hearing: p.id == pm.activeDevice && pm.state == "live")
                    }
                    Button { pm.addPendant() } label: { RowLabel(title: "Add pendant", symbol: "plus.circle", tint: Theme.teal) }
                        .buttonStyle(.plain)
                } header: { SectionLabel("Your pendants") } footer: {
                    SectionLabel("Every paired pendant stays connected at once and works the same way. Whichever you wear streams live; the others sync their memory when they're in range.")
                }
                .surfaceRows()

                Section {
                    row("Status", pm.state)
                    row("Drained this session", "\(pm.batchPages) pages")
                    row("Live this session", "\(pm.livePages) pages")
                    row("Waiting to upload", ByteCountFormatter.string(fromByteCount: Int64(pm.spoolBytes), countStyle: .file))
                    row("Uploaded this session", ByteCountFormatter.string(fromByteCount: Int64(pm.uploadedBytes), countStyle: .file))
                    Button { pm.refreshNow() } label: { RowLabel(title: "Check now", symbol: "arrow.clockwise", tint: Theme.teal) }
                        .buttonStyle(.plain)
                } header: { SectionLabel("Sync") }
                .surfaceRows()

                Section {
                    Button { scoring = true } label: {
                        RowLabel(title: "Voice Scoring", symbol: "person.crop.rectangle.stack", tint: Theme.teal)
                    }
                    .buttonStyle(.plain)
                } header: { SectionLabel("Voices") } footer: {
                    SectionLabel("Confirm or correct who's speaking, least certain voices first. Every answer teaches \(AppSettings.serverName).")
                }
                .surfaceRows()

                Section {
                    Toggle("Live stream", isOn: $pm.liveEnabled).tint(Theme.teal).foregroundStyle(Theme.textPrimary)
                    NavigationLink(value: AppRoute.log) { Text("Log").foregroundStyle(Theme.textPrimary) }
                    Button(role: .destructive) { confirmForget = true } label: {
                        RowLabel(title: "Forget all pendants", symbol: "xmark.circle", tint: Theme.destructive)
                    }
                    .buttonStyle(.plain)
                } header: { SectionLabel("Advanced") }
                .surfaceRows()
            }
            .nightList()
            .navigationTitle("Pendants")
            .navigationDestination(for: AppRoute.self) { $0.destination }
            .fullScreenCover(isPresented: $scoring) { VoiceScoringView() }
            .confirmationDialog("Forget all pendants?", isPresented: $confirmForget, titleVisibility: .visible) {
                Button("Forget", role: .destructive) { pm.forgetPendants() }
            } message: {
                Text("The app stops collecting from them until you add them again. Nothing already saved is deleted.")
            }
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k).foregroundStyle(Theme.textPrimary)
            Spacer()
            Text(v).foregroundStyle(Theme.textSecondary).font(.callout.monospacedDigit())
        }
    }
}

struct PendantRow: View {
    let snapshot: PendantSnapshot
    let active: Bool
    var hearing = false
    var body: some View {
        HStack(spacing: 14) {
            ZStack(alignment: .bottomTrailing) {
                Circle()
                    .fill(RadialGradient(colors: [Theme.metalLight, Theme.metalDark],
                                         center: UnitPoint(x: 0.35, y: 0.3), startRadius: 1, endRadius: 26))
                    .frame(width: 38, height: 38)
                Circle().fill(active ? Theme.teal : Theme.textTertiary).frame(width: 10, height: 10)
                    .overlay(Circle().stroke(.black.opacity(0.4)))
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(snapshot.name).font(TypeScale.headline).foregroundStyle(Theme.textPrimary)
                Text(hearing ? "Listening" : active ? "Connected" : snapshot.lastSeen.map { "Seen \($0.relative)" } ?? "Not seen yet")
                    .font(TypeScale.caption).foregroundStyle(active ? Theme.teal : Theme.textSecondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                if let b = snapshot.battery {
                    Label("\(b)%", systemImage: batterySymbol(b)).font(.caption.monospacedDigit())
                        .foregroundStyle(b <= 10 ? Theme.destructive : Theme.textPrimary)
                }
                if let used = snapshot.storageUsed {
                    Text(used < 0.01 ? "memory clear" : "\(Int(used * 100))% memory used")
                        .font(TypeScale.micro).foregroundStyle(used > 0.5 ? Theme.warning : Theme.textSecondary)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func batterySymbol(_ b: Int) -> String {
        b > 85 ? "battery.100" : b > 60 ? "battery.75" : b > 35 ? "battery.50" : b > 10 ? "battery.25" : "battery.0"
    }
}

struct LogView: View {
    @EnvironmentObject var pm: PendantManager
    var body: some View {
        List(pm.logLines.reversed(), id: \.self) { line in
            Text(line).font(.caption.monospaced()).foregroundStyle(Theme.textPrimary).surfaceRows()
        }
        .nightList()
        .navigationTitle("Log")
    }
}

/// A time stamp chip: the play glyph small before the time.
struct StampLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 3) {
            configuration.icon.font(.system(size: 8, weight: .bold))
            configuration.title
        }
    }
}

/// Screens pushed by value. Every push in a NavigationStack is value-based: mixing
/// view-destination links with value links in one stack makes pushes land late
/// (the person page opened only after tapping Back).
enum AppRoute: Hashable {
    case people, trash, log

    @ViewBuilder var destination: some View {
        switch self {
        case .people: PeopleView()
        case .trash: TrashView()
        case .log: LogView()
        }
    }
}
