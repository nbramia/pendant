import SwiftUI

/// A search hit: a conversation plus its day and a snippet around the match.
struct SearchHit: Decodable, Identifiable, Hashable {
    let date: String
    let snippet: String
    let conversation: Conversation
    var id: String { "\(date)|\(conversation.id)" }

    enum Keys: String, CodingKey { case date, snippet }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        date = try c.decode(String.self, forKey: .date)
        snippet = try c.decode(String.self, forKey: .snippet)
        conversation = try Conversation(from: decoder)
    }
}

struct SearchResults: Decodable { let results: [SearchHit]; let count: Int }

/// Search everything ever said, across days, with filters.
struct SearchView: View {
    enum Span: String, CaseIterable, Identifiable {
        case any = "Any time", today = "Today", week = "7 days", month = "30 days"
        var id: String { rawValue }
        var days: Int? { switch self { case .any: nil; case .today: 0; case .week: 7; case .month: 30 } }
    }

    @State private var query = ""
    @State private var span: Span = .any
    @State private var pendant: String?        // "old" / "new"
    @State private var speaker: String?
    @State private var starred = false
    @State private var todos = false
    @State private var long = false
    @State private var hits: [SearchHit] = []
    @State private var searching = false
    @State private var failed: FeedProblem?
    @AppStorage("recentSearches") private var recentRaw = ""
    @State private var roster: [String] = []

    private var recent: [String] { recentRaw.split(separator: "\n").map(String.init) }
    private var anyFilter: Bool { span != .any || pendant != nil || speaker != nil || starred || todos || long }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    filters
                    content
                }
                .padding(.horizontal)
                .padding(.bottom, 24)
            }
            .background(Theme.night.ignoresSafeArea())
            .tabBarClearance()
            .navigationTitle("Search")
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "Words, names, topics…")
            .onSubmit(of: .search) { remember(query) }
            .task(id: key) {
                try? await Task.sleep(for: .milliseconds(350))   // debounce typing
                guard !Task.isCancelled else { return }
                await run()
            }
            .task {
                if let r = try? await Feed.fetch("speakers", as: SpeakerRoster.self) { roster = r.allowed }
            }
            .retryWhileFailed(failed) { await run() }
            .navigationDestination(for: SearchHit.self) { h in
                ConversationDetail(conversation: h.conversation, date: h.date)
            }
        }
    }

    private var key: String {
        [query, span.rawValue, pendant ?? "", speaker ?? "", "\(starred)\(todos)\(long)"].joined(separator: "|")
    }

    // MARK: Filters

    /// Six filter chips in an equal-width grid, three to a row; Clear under them once any is on.
    private var filters: some View {
        VStack(alignment: .trailing, spacing: 0) {
            ButtonRow(columns: 3) {
                Menu {
                    Picker("When", selection: $span) { ForEach(Span.allCases) { Text($0.rawValue).tag($0) } }
                } label: { Chip(text: span.rawValue, symbol: "calendar", on: span != .any) }
                Menu {
                    Button("Any pendant") { pendant = nil }
                    Button("Old pendant") { pendant = "old" }
                    Button("New pendant") { pendant = "new" }
                } label: { Chip(text: pendant.map { "\($0.capitalized) pendant" } ?? "Pendant", symbol: "circle.circle", on: pendant != nil) }
                Menu {
                    Button("Anyone") { speaker = nil }
                    ForEach(roster, id: \.self) { n in Button(n) { speaker = n } }
                } label: { Chip(text: speaker ?? "Speaker", symbol: "person.wave.2", on: speaker != nil) }
            }
            ButtonRow(columns: 3) {
                Button { starred.toggle() } label: { Chip(text: "Notable", symbol: "star.fill", on: starred) }
                Button { todos.toggle() } label: { Chip(text: "Has to-dos", symbol: "checklist", on: todos) }
                Button { long.toggle() } label: { Chip(text: "10+ min", symbol: "clock", on: long) }
            }
            .buttonStyle(.plain)   // chips are the buttons' faces
            if anyFilter {
                Button("Clear") {
                    withAnimation { span = .any; pendant = nil; speaker = nil; starred = false; todos = false; long = false }
                }
                .buttonStyle(.tertiary(.small))
            }
        }
    }

    // MARK: Results

    @ViewBuilder private var content: some View {
        if query.isEmpty && !anyFilter {
            if !recent.isEmpty {
                Text("Recent").font(TypeScale.headline).foregroundStyle(Theme.textPrimary)
                ForEach(recent, id: \.self) { r in
                    Button { query = r } label: { RowLabel(title: r, symbol: "clock.arrow.circlepath") }
                        .buttonStyle(.plain)
                }
            }
            MessageCard(title: "Search everything you've said",
                        detail: "Every word must match. Narrow by day, pendant, speaker, notable conversations, ones with to-dos, or long ones.")
        } else if let failed, hits.isEmpty {
            FeedErrorCard(problem: failed) { await run() }
        } else if hits.isEmpty && !searching {
            MessageCard(title: "No matches")
        } else {
            Text("\(hits.count) conversation\(hits.count == 1 ? "" : "s")").font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
            ForEach(groupedDates, id: \.self) { date in
                Text(prettyDay(date)).font(TypeScale.headline).foregroundStyle(Theme.textPrimary).padding(.top, 6)
                ForEach(hits.filter { $0.date == date }) { h in
                    NavigationLink(value: h) { HitRow(hit: h, terms: terms) }.buttonStyle(.plain)
                }
            }
        }
    }

    private var terms: [String] { query.lowercased().split(separator: " ").map(String.init) }
    private var groupedDates: [String] { Array(Set(hits.map(\.date))).sorted(by: >) }

    private func run() async {
        if query.isEmpty && !anyFilter { hits = []; return }
        searching = true
        defer { searching = false }
        var items = [URLQueryItem(name: "q", value: query)]
        if let d = span.days {
            let from = Calendar.current.date(byAdding: .day, value: -d, to: Date())!
            items.append(URLQueryItem(name: "from", value: Feed.todayString(from)))
        } else {
            items.append(URLQueryItem(name: "from", value: "2020-01-01"))
        }
        if let pendant { items.append(URLQueryItem(name: "device", value: pendant)) }
        if let speaker { items.append(URLQueryItem(name: "speaker", value: speaker)) }
        if starred { items.append(URLQueryItem(name: "notable", value: "1")) }
        if todos { items.append(URLQueryItem(name: "actions", value: "1")) }
        if long { items.append(URLQueryItem(name: "min_minutes", value: "10")) }
        var comps = URLComponents()
        comps.queryItems = items
        do {
            let r = try await Feed.fetch("search" + (comps.string ?? ""), as: SearchResults.self)
            withAnimation(.snappy) { hits = r.results }
            failed = nil
        } catch {
            if !(error is CancellationError) { failed = FeedProblem(error) }
        }
    }

    private func remember(_ q: String) {
        let t = q.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        recentRaw = ([t] + recent.filter { $0 != t }).prefix(8).joined(separator: "\n")
    }

    private func prettyDay(_ iso: String) -> String {
        let p = DateFormatter(); p.dateFormat = "yyyy-MM-dd"
        guard let d = p.date(from: iso) else { return iso }
        if Calendar.current.isDateInToday(d) { return "Today" }
        if Calendar.current.isDateInYesterday(d) { return "Yesterday" }
        let f = DateFormatter(); f.dateFormat = "EEEE, MMM d"
        return f.string(from: d)
    }
}

struct HitRow: View {
    let hit: SearchHit
    let terms: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(hit.conversation.time).font(.subheadline.monospacedDigit().weight(.semibold))
                if hit.conversation.hasRealTitle { Text(hit.conversation.title).font(TypeScale.label).lineLimit(1) }
                Spacer()
                if hit.conversation.notable == true {
                    Image(systemName: "star.fill").font(TypeScale.caption).foregroundStyle(Theme.star).accessibilityLabel("Notable")
                }
                Badge(text: hit.conversation.durationLabel)
            }
            .foregroundStyle(Theme.textPrimary)
            chippedPreview(hit.snippet, font: .callout, terms: terms).font(.callout).foregroundStyle(Theme.textSecondary).lineLimit(4)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(radius: Radius.row)
    }
}
