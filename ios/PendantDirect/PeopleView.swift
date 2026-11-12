import SwiftUI

/// One named speaker's totals over a period (GET /pendant/feed/people).
struct PersonSummary: Codable, Identifiable, Hashable {
    let name: String
    let talkMinutes: Double
    let conversations: Int
    let lastHeard: String
    let share: Double
    /// The LifeOS person this name is linked to, and their page in the LifeOS web app.
    var entityId: String? = nil
    var lifeosUrl: String? = nil
    /// That LifeOS person's full name and circle, when LifeOS answered.
    var lifeosName: String? = nil
    var lifeosCircle: Int? = nil
    var id: String { name }

    /// "LifeOS: Alex Rivera · circle 0" or "Not linked".
    var linkLine: String { Suggestions.linkLine(entityId: entityId, name: lifeosName, circle: lifeosCircle) }

    enum CodingKeys: String, CodingKey {
        case name, conversations, share
        case entityId = "entity_id"
        case lifeosUrl = "lifeos_url"
        case lifeosName = "lifeos_name"
        case lifeosCircle = "lifeos_circle"
        case talkMinutes = "talk_minutes"
        case lastHeard = "last_heard"
    }

    var lastHeardDate: Date? { ISO8601DateFormatter().date(from: lastHeard) }
}

/// One conversation a person spoke in (GET /pendant/feed/people/<name>).
struct PersonConversation: Codable, Identifiable, Hashable {
    let date: String
    let time: String
    let title: String
    let device: String?
    let theirMinutes: Double
    let totalMinutes: Double
    var id: String { "\(date)|\(time)|\(title)|\(device ?? "")" }

    enum CodingKeys: String, CodingKey {
        case date, time, title, device
        case theirMinutes = "their_minutes"
        case totalMinutes = "total_minutes"
    }
}

/// "12 min" / "1 h 05 min" / "<1 min".
func minutesLabel(_ m: Double) -> String {
    if m < 1 { return m > 0 ? "<1 min" : "0 min" }
    let n = Int(m.rounded())
    return n >= 60 ? String(format: "%d h %02d min", n / 60, n % 60) : "\(n) min"
}

/// Who the wearer talks with: named speakers ranked by talk time.
struct PeopleView: View {
    enum Period: String, CaseIterable, Identifiable {
        case week = "Week", today = "Today", month = "Month"
        var id: String { rawValue }
        var days: Int { switch self { case .week: 7; case .today: 1; case .month: 30 } }
    }

    @State private var period: Period = .week
    @State private var people: [PersonSummary] = []
    @State private var loaded = false
    @State private var failed: FeedProblem?

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                Picker("Period", selection: $period) {
                    ForEach(Period.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                content
            }
            .padding(.horizontal)
            .padding(.bottom, 24)
        }
        .background(Theme.night.ignoresSafeArea())
        .tabBarClearance()
        .navigationTitle("People")
        .toolbar { ToolbarItem(placement: .topBarTrailing) { VoiceSampleButton().labelStyle(.iconOnly) } }
        .refreshable { await load() }
        .task(id: period) { await load() }
        .retryWhileFailed(failed) { await load() }
        .navigationDestination(for: PersonSummary.self) { PersonView(person: $0, days: period.days) }
        .navigationDestination(for: PersonConversation.self) { r in
            ConversationLoader(date: r.date, time: r.time, title: r.title, device: r.device)
        }
    }

    @ViewBuilder private var content: some View {
        if let failed, people.isEmpty {
            FeedErrorCard(problem: failed) { await load() }
        } else if people.isEmpty && loaded {
            MessageCard(title: "No one yet", detail: "Named speakers show up here once their voice is labeled.")
        } else {
            let top = people.map(\.share).max() ?? 1
            ForEach(people) { p in
                NavigationLink(value: p) { PersonRow(person: p, relativeShare: top > 0 ? p.share / top : 0) }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("person-\(p.name)")
            }
        }
    }

    private func load() async {
        do {
            let r = try await Feed.fetch("people?days=\(period.days)", as: [PersonSummary].self)
            withAnimation(.snappy) { people = r }
            failed = nil
        } catch {
            if !(error is CancellationError) { failed = FeedProblem(error) }
        }
        loaded = true
    }
}

struct PersonRow: View {
    let person: PersonSummary
    /// Bar length relative to the top person (0...1).
    let relativeShare: Double

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(person.name).font(TypeScale.headline)
                    if person.entityId != nil {
                        Image(systemName: "link").font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                            .accessibilityLabel("Linked to LifeOS")
                    }
                    Spacer()
                    Text(minutesLabel(person.talkMinutes)).font(.subheadline.weight(.semibold).monospacedDigit())
                    Image(systemName: "chevron.right").font(TypeScale.captionStrong).foregroundStyle(Theme.textTertiary)
                }
                GeometryReader { g in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Theme.raised)
                        Capsule().fill(Theme.teal).frame(width: max(4, g.size.width * relativeShare))
                    }
                }
                .frame(height: 6)
                Text(person.linkLine).font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
                HStack {
                    Text("\(person.conversations) conversation\(person.conversations == 1 ? "" : "s")")
                    Spacer()
                    if let d = person.lastHeardDate { Text("heard \(d.relative)") }
                }
                .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
            }
        }
    }
}

/// A person's recent conversations; each opens the conversation detail.
struct PersonView: View {
    let person: PersonSummary
    let days: Int
    /// The link as changed from here; nil until the wearer re-points it.
    @State private var relinked: PersonLinkResult?
    @State private var changing = false
    @State private var rows: [PersonConversation] = []
    @State private var loaded = false
    @State private var failed: FeedProblem?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(relinked.map { Suggestions.linkLine(entityId: $0.entityId, name: $0.lifeosName, circle: $0.lifeosCircle) }
                     ?? person.linkLine)
                    .font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary)
                ButtonRow(columns: 2) {
                    if let id = relinked?.entityId ?? person.entityId,
                       let url = URL(string: relinked == nil ? person.lifeosUrl ?? "" : lifeosPage(id)) {
                        // short, text-only labels: both fit one line at half width
                        Link("Open in LifeOS", destination: url)
                        .buttonStyle(.secondary(.medium))
                    }
                    Button("Change person") { changing = true }
                    .buttonStyle(.secondary(.medium))
                    .accessibilityIdentifier("change-lifeos-person")
                }
                if let failed, rows.isEmpty {
                    FeedErrorCard(problem: failed) { await load() }
                } else if rows.isEmpty && loaded {
                    MessageCard(title: "No conversations in this period")
                }
                ForEach(rows) { r in
                    NavigationLink(value: r) { row(r) }
                        .buttonStyle(.plain)
                }
            }
            .padding(.horizontal)
            .padding(.bottom, 24)
        }
        .background(Theme.night.ignoresSafeArea())
        .tabBarClearance()
        .navigationTitle(person.name)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
        .retryWhileFailed(failed) { await load() }
        .sheet(isPresented: $changing) {
            ChangeLifeOSPersonView(name: person.name) { relinked = $0 }
        }
    }

    /// The person's page in the LifeOS web app (same host as `lifeosUrl`).
    private func lifeosPage(_ id: String) -> String {
        guard let base = person.lifeosUrl, let r = base.range(of: "/crm/") else { return "" }
        return String(base[..<r.upperBound]) + id
    }

    private func row(_ r: PersonConversation) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(prettyDay(r.date)).font(TypeScale.label).foregroundStyle(Theme.textPrimary)
                Text(r.time).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.textSecondary)
                Spacer()
                Text("\(minutesLabel(r.theirMinutes)) of \(minutesLabel(r.totalMinutes))")
                    .font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
            }
            if !(r.title.contains("_") && r.title.split(separator: "_").count == 3) {
                Text(r.title).font(TypeScale.callout).foregroundStyle(Theme.textPrimary).lineLimit(2)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(radius: Radius.row)
    }

    private func load() async {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        let name = person.name.addingPercentEncoding(withAllowedCharacters: allowed) ?? person.name
        do {
            rows = try await Feed.fetch("people/\(name)?days=\(days)", as: [PersonConversation].self)
            failed = nil
        } catch {
            if !(error is CancellationError) { failed = FeedProblem(error) }
        }
        loaded = true
    }

    private func prettyDay(_ iso: String) -> String {
        let p = DateFormatter(); p.dateFormat = "yyyy-MM-dd"
        guard let d = p.date(from: iso) else { return iso }
        if Calendar.current.isDateInToday(d) { return "Today" }
        if Calendar.current.isDateInYesterday(d) { return "Yesterday" }
        let f = DateFormatter(); f.dateFormat = "EEE, MMM d"
        return f.string(from: d)
    }
}

/// "Change LifeOS person": the shared picker, LifeOS people only. Picking one re-points this
/// speaker name's link; transcript labels, voiceprints and the voice library keep the name.
struct ChangeLifeOSPersonView: View {
    let name: String
    let done: (PersonLinkResult) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var submit = 0
    @State private var saving = false
    @State private var failed = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    NameField(text: $query, placeholder: "Search for a person").onSubmit { submit += 1 }
                } header: { SectionLabel("Search LifeOS") }
                .surfaceRows()
                Section {
                    SuggestionRows(query: query, linkOnly: true, submitTick: submit) { s in
                        if let id = s.entityId { Task { await relink(id) } }
                    }
                } header: { SectionLabel("Who is \(name) in LifeOS?") }
                .surfaceRows()
                if failed {
                    Label("Couldn't change it. Is Tailscale on and LifeOS up?", systemImage: "wifi.exclamationmark")
                        .foregroundStyle(Theme.warning)
                        .surfaceRows()
                }
            }
            .nightList()
            .disabled(saving)
            .navigationTitle("Change LifeOS person")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
    }

    private func relink(_ entityId: String) async {
        saving = true
        failed = false
        defer { saving = false }
        do {
            let r = try await Feed.send(Suggestions.linkPath(name), json: ["entity_id": entityId], as: PersonLinkResult.self)
            done(r)
            dismiss()
        } catch {
            failed = true
        }
    }
}

/// Fetches a day's feed and shows the conversation matching time + title.
struct ConversationLoader: View {
    let date: String
    let time: String
    let title: String
    let device: String?
    @State private var conversation: Conversation?
    @State private var failed = false

    var body: some View {
        Group {
            if let conversation {
                ConversationDetail(conversation: conversation, date: date)
            } else {
                ZStack {
                    Theme.night.ignoresSafeArea()
                    if failed {
                        Label("Couldn't load this conversation", systemImage: "wifi.exclamationmark").foregroundStyle(Theme.textSecondary)
                    } else {
                        ProgressView()
                    }
                }
                .navigationTitle(time)
                .navigationBarTitleDisplayMode(.inline)
            }
        }
        .task {
            do {
                let day = try await Feed.fetch("day/\(date)", as: DayFeed.self)
                conversation = day.conversations.first { $0.time == time && $0.title == title && $0.device == device }
                    ?? day.conversations.first { $0.time == time && $0.title == title }
                failed = conversation == nil
            } catch {
                failed = true
            }
        }
    }
}
