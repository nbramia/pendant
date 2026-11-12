import SwiftUI

// The design system: colour, type, spacing and radius tokens, the four button styles, and the
// card, chip and badge components every screen is built from. Colours come from `Palette`
// (checked for WCAG contrast by `PaletteTests`); this file only turns them into SwiftUI.

extension Color {
    init(_ c: RGB) { self.init(.sRGB, red: c.r, green: c.g, blue: c.b, opacity: 1) }
}

enum Theme {
    /// The night-blue background gradient.
    static let night = LinearGradient(colors: [Color(Palette.nightTop), Color(Palette.nightBottom)],
                                      startPoint: .top, endPoint: .bottom)
    static let teal = Color(Palette.accent)
    static let accentPressed = Color(Palette.accentPressed)

    // Surfaces
    static let surface = Color(Palette.surface)
    static let raised = Color(Palette.raised)
    static let sunken = Color(Palette.sunken)
    static let accentWash = Color(Palette.accentWash)
    static let playingWash = Color(Palette.playingWash)
    static let starWash = Color(Palette.starWash)
    static let destructiveWash = Color(Palette.destructiveWash)
    /// Hairline around cards and controls.
    static let hairline = Color.white.opacity(0.09)

    // Text
    static let textPrimary = Color(Palette.textPrimary)
    static let textSecondary = Color(Palette.textSecondary)
    static let textTertiary = Color(Palette.textTertiary)
    static let onAccent = Color(Palette.onAccent)
    static let disabledText = Color(Palette.disabledText)
    static let disabledFill = Color(Palette.disabledFill)
    static let unavailableTitle = Color(Palette.unavailableOption.title)
    static let unavailableReason = Color(Palette.unavailableOption.reason)

    // Status
    static let destructive = Color(Palette.destructive)
    static let star = Color(Palette.star)
    static let warning = Color(Palette.warning)
    static let success = Color(Palette.success)
    /// Swipe-action fills (iOS draws their labels white).
    static let swipeStar = Color(Palette.swipeStar)
    static let swipeDelete = Color(Palette.swipeDelete)

    // The pendant drawn in the app (orb, pendant rows).
    static let metalLight = Color(red: 0.93, green: 0.94, blue: 0.95)
    static let metalDark = Color(red: 0.50, green: 0.52, blue: 0.56)
}

/// The system font at the sizes the app already used, named by role.
enum TypeScale {
    static let display = Font.largeTitle.weight(.bold)
    static let title = Font.title2.weight(.semibold)
    static let title3 = Font.title3.weight(.semibold)
    static let headline = Font.headline
    static let body = Font.body
    static let callout = Font.callout
    static let subheadline = Font.subheadline
    static let label = Font.subheadline.weight(.semibold)
    static let caption = Font.caption
    static let captionStrong = Font.caption.weight(.semibold)
    static let micro = Font.caption2
}

enum Spacing {
    static let xxs: CGFloat = 2
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let xl: CGFloat = 24
}

enum Radius {
    static let small: CGFloat = 10
    static let control: CGFloat = 14
    static let row: CGFloat = 18
    static let card: CGFloat = 22
    static let hero: CGFloat = 28
}

/// Smallest tap target (Apple HIG).
let minTapTarget: CGFloat = 44

// MARK: - Buttons

/// A button's size comes from its role, never from its label:
/// - `.large`: 52 pt tall, full width. The single main action of a screen or sheet.
/// - `.medium`: 44 pt tall. Buttons in a `ButtonRow` share the row's width equally.
/// - `.small`: 32 pt tall (44 pt tap area). Inline actions in rows and cards.
/// A long label wraps to two lines, then truncates; it never grows the button. Icons in a
/// label are one fixed size per button size, so an icon can't make one button bigger than
/// its neighbours. Icon-only buttons use `IconButtonSize`.
enum ButtonSize: CaseIterable {
    case large, medium, small

    var height: CGFloat {
        switch self { case .large: 52; case .medium: 44; case .small: 32 }
    }
    /// Narrowest the button draws, however short its label.
    var minWidth: CGFloat {
        switch self { case .large: 160; case .medium: 96; case .small: 80 }
    }
    /// The SF Symbol size of an icon in the label.
    var iconSize: CGFloat {
        switch self { case .large: 20; case .medium: 17; case .small: 14 }
    }
    var font: Font {
        switch self {
        case .large: .body.weight(.semibold)
        case .medium: .callout.weight(.semibold)
        case .small: .footnote.weight(.semibold)
        }
    }
    var horizontalPadding: CGFloat {
        switch self { case .large: Spacing.l; case .medium: Spacing.m; case .small: Spacing.s + 2 }
    }
    var radius: CGFloat { self == .small ? Radius.small : Radius.control }
}

/// The two icon-only button sizes: a 44 pt control, and the 64 pt play control.
enum IconButtonSize: CGFloat {
    case standard = 44
    case play = 64
    /// The symbol inside the circle.
    var iconSize: CGFloat { rawValue * 0.42 }
}

/// The app's four buttons. Every button uses one, with a `ButtonSize`:
/// - `.primary(_:)`: filled teal, dark text. The one thing a screen most wants you to do.
/// - `.secondary(_:)`: bordered, teal text on a raised fill.
/// - `.tertiary(_:)`: text only, teal and semibold.
/// - `.destructive(_:)`: coral text and border on a coral wash.
/// `.icon(_:kind:)` draws an icon-only circle of a fixed size.
struct PendantButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, tertiary, destructive }
    let kind: Kind
    var size: ButtonSize = .medium
    var icon: IconButtonSize? = nil

    func makeBody(configuration: Configuration) -> some View {
        PendantButton(kind: kind, size: size, icon: icon, configuration: configuration)
    }
}

extension ButtonStyle where Self == PendantButtonStyle {
    static func primary(_ size: ButtonSize) -> PendantButtonStyle { PendantButtonStyle(kind: .primary, size: size) }
    static func secondary(_ size: ButtonSize) -> PendantButtonStyle { PendantButtonStyle(kind: .secondary, size: size) }
    static func tertiary(_ size: ButtonSize) -> PendantButtonStyle { PendantButtonStyle(kind: .tertiary, size: size) }
    static func destructive(_ size: ButtonSize) -> PendantButtonStyle { PendantButtonStyle(kind: .destructive, size: size) }
    /// An icon-only circle.
    static func icon(_ size: IconButtonSize, kind: PendantButtonStyle.Kind = .primary) -> PendantButtonStyle {
        PendantButtonStyle(kind: kind, icon: size)
    }
}

/// Set by `ButtonRow`: buttons fill the width the row gives them.
private struct ButtonFillsWidthKey: EnvironmentKey { static let defaultValue = false }
extension EnvironmentValues {
    var buttonFillsWidth: Bool {
        get { self[ButtonFillsWidthKey.self] }
        set { self[ButtonFillsWidthKey.self] = newValue }
    }
}

/// A button label's icon at a fixed size, so it can't change the button's size.
struct FixedIconLabelStyle: LabelStyle {
    let iconSize: CGFloat
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: Spacing.xs + 2) {
            configuration.icon
                .font(.system(size: iconSize, weight: .semibold))
                .imageScale(.medium)
                .frame(width: iconSize + 4, height: iconSize)
            configuration.title
        }
    }
}

private struct PendantButton: View {
    let kind: PendantButtonStyle.Kind
    let size: ButtonSize
    let icon: IconButtonSize?
    let configuration: ButtonStyleConfiguration
    @Environment(\.isEnabled) private var enabled
    @Environment(\.buttonFillsWidth) private var inRow

    private var pressed: Bool { configuration.isPressed }
    private var fills: Bool { size == .large || inRow }

    var body: some View {
        Group {
            if let icon {
                configuration.label
                    .labelStyle(.iconOnly)
                    .font(.system(size: icon.iconSize, weight: .semibold))
                    .foregroundStyle(foreground)
                    .frame(width: icon.rawValue, height: icon.rawValue)
                    .background(fill, in: Circle())
                    .overlay(Circle().strokeBorder(stroke, lineWidth: kind == .primary ? 0 : 1.5))
                    .contentShape(Circle())
            } else {
                configuration.label
                    .labelStyle(FixedIconLabelStyle(iconSize: size.iconSize))
                    .font(size.font)
                    .dynamicTypeSize(...DynamicTypeSize.xxLarge)
                    .multilineTextAlignment(.center)
                    // a long label (a full LifeOS name) wraps to two lines, then loses its middle
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .minimumScaleFactor(0.85)
                    .foregroundStyle(foreground)
                    .padding(.horizontal, size.horizontalPadding)
                    .frame(minWidth: size.minWidth, maxWidth: fills ? .infinity : nil)
                    .frame(height: size.height)
                    .background(fill, in: RoundedRectangle(cornerRadius: size.radius, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: size.radius, style: .continuous)
                        .strokeBorder(stroke, lineWidth: 1))
                    // small buttons draw 32 pt but keep a 44 pt tap area
                    .padding(.vertical, max(0, (minTapTarget - size.height) / 2))
                    .contentShape(Rectangle())
            }
        }
        .scaleEffect(pressed ? 0.97 : 1)
        .animation(.snappy(duration: 0.15), value: pressed)
    }

    private var foreground: Color {
        guard enabled else { return Theme.disabledText }
        return switch kind {
        case .primary: Theme.onAccent
        case .secondary, .tertiary: Theme.teal
        case .destructive: Theme.destructive
        }
    }

    private var fill: Color {
        guard enabled else { return kind == .tertiary ? .clear : Theme.disabledFill }
        return switch kind {
        case .primary: pressed ? Theme.accentPressed : Theme.teal
        case .secondary: pressed ? Theme.accentWash : Theme.raised
        case .tertiary: pressed ? Theme.accentWash : .clear
        case .destructive: Theme.destructiveWash
        }
    }

    private var stroke: Color {
        guard enabled else { return kind == .tertiary ? .clear : Theme.hairline }
        return switch kind {
        case .primary, .tertiary: .clear
        case .secondary: Theme.teal.opacity(0.45)
        case .destructive: Theme.destructive.opacity(0.6)
        }
    }
}

/// Lays out 2–3 buttons side by side at exactly equal widths, whatever their labels. With
/// `columns` set, a row with fewer buttons keeps the column width (a grid's last row).
struct ButtonRow<Content: View>: View {
    var columns: Int? = nil
    var spacing: CGFloat = Spacing.s
    @ViewBuilder var content: Content

    var body: some View {
        EqualWidthLayout(columns: columns, spacing: spacing) { content }
            .environment(\.buttonFillsWidth, true)
    }
}

/// `ButtonRow`'s layout: every slot is `ButtonRow.slotWidth` wide; the row is as tall as its tallest child.
struct EqualWidthLayout: Layout {
    var columns: Int? = nil
    var spacing: CGFloat = Spacing.s

    /// The width of each of `count` slots across `width`, `spacing` apart.
    static func slotWidth(width: CGFloat, count: Int, spacing: CGFloat) -> CGFloat {
        guard count > 0 else { return 0 }
        return max(0, (width - spacing * CGFloat(count - 1)) / CGFloat(count))
    }

    private func slots(_ n: Int) -> Int { max(n, columns ?? 0) }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let n = slots(subviews.count)
        guard n > 0 else { return .zero }
        let width = proposal.width
            ?? (subviews.map { $0.sizeThatFits(.unspecified).width }.max() ?? 0) * CGFloat(n) + spacing * CGFloat(n - 1)
        let w = Self.slotWidth(width: width, count: n, spacing: spacing)
        let height = subviews.map { $0.sizeThatFits(ProposedViewSize(width: w, height: nil)).height }.max() ?? 0
        return CGSize(width: width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let w = Self.slotWidth(width: bounds.width, count: slots(subviews.count), spacing: spacing)
        for (i, v) in subviews.enumerated() {
            v.place(at: CGPoint(x: bounds.minX + CGFloat(i) * (w + spacing), y: bounds.midY), anchor: .leading,
                    proposal: ProposedViewSize(width: w, height: bounds.height))
        }
    }
}

// MARK: - Surfaces

extension View {
    /// Lays the view on a solid surface token with the hairline border.
    func surface(_ fill: Color = Theme.surface, radius: CGFloat = Radius.card) -> some View {
        background(fill, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).strokeBorder(Theme.hairline))
    }

    /// The night gradient behind a whole screen.
    func nightBackground() -> some View { background(Theme.night.ignoresSafeArea()) }
}

/// The card every screen groups content in.
struct Card<Content: View>: View {
    var fill: Color = Theme.surface
    @ViewBuilder var content: Content
    var body: some View {
        content
            .foregroundStyle(Theme.textPrimary)
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .surface(fill)
    }
}

/// A plain message on a card: an icon, a title and an optional line of detail.
struct MessageCard: View {
    let title: String
    var detail: String? = nil
    var symbol: String? = nil
    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: Spacing.xs + 2) {
                if let symbol {
                    Label(title, systemImage: symbol).font(TypeScale.headline)
                } else {
                    Text(title).font(TypeScale.headline)
                }
                if let detail { Text(detail).font(TypeScale.subheadline).foregroundStyle(Theme.textSecondary) }
            }
        }
    }
}

struct StatChip: View {
    let value: String
    let label: String
    let symbol: String
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Image(systemName: symbol).font(TypeScale.caption).foregroundStyle(Theme.teal)
            Text(value).font(.title2.weight(.semibold)).monospacedDigit()
                .foregroundStyle(Theme.textPrimary)
                .contentTransition(.numericText())
            Text(label).font(TypeScale.caption).foregroundStyle(Theme.textSecondary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(radius: Radius.row)
    }
}

/// A speaker's name as a tinted chip with a person icon.
struct SpeakerChip: View {
    let name: String
    var body: some View {
        Label(name, systemImage: "person.fill")
            .font(.callout.weight(.semibold))
            .foregroundStyle(Theme.teal)
            .padding(.horizontal, 9).padding(.vertical, 3)
            .background(Theme.accentWash, in: Capsule())
    }
}

/// A filter chip: teal when on. Put it in a `Button` (`.plain`) or a `Menu` label. In a
/// `ButtonRow` it fills its slot, so every chip in the row is the same width.
struct Chip: View {
    let text: String
    let symbol: String
    let on: Bool
    @Environment(\.buttonFillsWidth) private var fills
    var body: some View {
        Label(text, systemImage: symbol)
            .font(.caption.weight(.semibold))
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .padding(.horizontal, fills ? Spacing.s : 12)
            .frame(maxWidth: fills ? .infinity : nil, minHeight: 34)
            .background(on ? Theme.accentWash : Theme.raised, in: Capsule())
            .overlay(Capsule().strokeBorder(on ? Theme.teal : Theme.hairline))
            .foregroundStyle(on ? Theme.teal : Theme.textPrimary)
            .padding(.vertical, (minTapTarget - 34) / 2)
            .contentShape(Capsule())
    }
}

/// A small read-only capsule (duration, kind, star count).
struct Badge: View {
    enum Tone { case neutral, accent, star }
    let text: String
    var symbol: String? = nil
    var tone: Tone = .neutral

    var body: some View {
        Group {
            if let symbol {
                if text.isEmpty { Image(systemName: symbol) } else { Label(text, systemImage: symbol) }
            } else {
                Text(text)
            }
        }
        .font(.caption.weight(.semibold).monospacedDigit())
        .foregroundStyle(foreground)
        .padding(.horizontal, 7).padding(.vertical, 2)
        .background(background, in: Capsule())
    }

    private var foreground: Color {
        switch tone { case .neutral: Theme.textSecondary; case .accent: Theme.teal; case .star: Theme.star }
    }
    private var background: Color {
        switch tone { case .neutral: Theme.raised; case .accent: Theme.accentWash; case .star: Theme.starWash }
    }
}

/// A List section header or footer in the secondary text colour.
struct SectionLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).foregroundStyle(Theme.textSecondary) }
}

/// Lays out buttons left to right, wrapping onto new lines. An item wider than the row is
/// offered the row's width, so a long name wraps or truncates inside the card instead of
/// running off it.
struct FlowLayout: Layout {
    var spacing: CGFloat = Spacing.s

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        let frames = Self.frames(count: subviews.count, width: width, spacing: spacing) { measure(subviews[$0], $1) }
        let used = frames.map(\.maxX).max() ?? 0
        return CGSize(width: proposal.width ?? used, height: frames.map(\.maxY).max() ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let frames = Self.frames(count: subviews.count, width: bounds.width, spacing: spacing) { measure(subviews[$0], $1) }
        for (i, f) in frames.enumerated() {
            subviews[i].place(at: CGPoint(x: bounds.minX + f.minX, y: bounds.minY + f.minY),
                              proposal: ProposedViewSize(width: f.width, height: f.height))
        }
    }

    private func measure(_ view: LayoutSubview, _ width: CGFloat?) -> CGSize {
        view.sizeThatFits(ProposedViewSize(width: width, height: nil))
    }

    /// Each item's frame, rows top to bottom. `measure(i, nil)` is an item's ideal size;
    /// `measure(i, w)` its size when offered at most `w` (it may wrap and grow taller).
    /// No frame is wider than `width`.
    static func frames(count: Int, width: CGFloat, spacing: CGFloat,
                       measure: (Int, CGFloat?) -> CGSize) -> [CGRect] {
        var frames: [CGRect] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for i in 0..<count {
            var s = measure(i, nil)
            if s.width > width {
                s = measure(i, width)
                s.width = min(s.width, width)
            }
            if x > 0, x + s.width > width {   // doesn't fit after what's on this row: new row
                y += rowHeight + spacing
                x = 0
                rowHeight = 0
            }
            frames.append(CGRect(x: x, y: y, width: s.width, height: s.height))
            x += s.width + spacing
            rowHeight = max(rowHeight, s.height)
        }
        return frames
    }
}

extension Date {
    var relative: String {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f.localizedString(for: self, relativeTo: Date())
    }
}

/// A name entry field on a raised fill: words capitalised, no autocorrect, Done to submit.
struct NameField: View {
    @Binding var text: String
    var placeholder = "Name"
    var body: some View {
        TextField(placeholder, text: $text, prompt: Text(placeholder).foregroundStyle(Theme.textTertiary))
            .textFieldStyle(.plain)
            .autocorrectionDisabled()
            #if os(iOS)
            .textInputAutocapitalization(.words)
            #endif
            .submitLabel(.done)
            .foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, Spacing.m)
            .frame(minHeight: minTapTarget)
            .surface(Theme.raised, radius: Radius.control)
    }
}

/// A tappable row's label (pickers, suggestion lists): fills the row so the whole width taps.
/// Use it with `.buttonStyle(.plain)`.
struct RowLabel<Trailing: View>: View {
    let title: String
    var subtitle: String = ""
    var symbol: String? = nil
    var tint: Color = Theme.textPrimary
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(spacing: Spacing.m) {
            if let symbol { Image(systemName: symbol).foregroundStyle(tint == Theme.textPrimary ? Theme.teal : tint).frame(width: 24) }
            VStack(alignment: .leading, spacing: Spacing.xxs) {
                Text(title).foregroundStyle(tint)
                if !subtitle.isEmpty { Text(subtitle).font(TypeScale.caption).foregroundStyle(Theme.textSecondary) }
            }
            Spacer(minLength: Spacing.s)
            trailing
        }
        .frame(maxWidth: .infinity, minHeight: minTapTarget, alignment: .leading)
        .contentShape(Rectangle())
    }
}

extension RowLabel where Trailing == EmptyView {
    init(title: String, subtitle: String = "", symbol: String? = nil, tint: Color = Theme.textPrimary) {
        self.init(title: title, subtitle: subtitle, symbol: symbol, tint: tint) { EmptyView() }
    }
}

extension View {
    /// A `List` on the night background (no system grouped grey).
    func nightList() -> some View { scrollContentBackground(.hidden).nightBackground().tabBarClearance() }

    /// Extra room under a scroll view's last item, so it scrolls fully clear of the floating tab bar.
    func tabBarClearance() -> some View { contentMargins(.bottom, Spacing.xl, for: .scrollContent) }

    /// List rows on the surface token.
    func surfaceRows() -> some View { listRowBackground(Theme.surface) }
}

/// The screen tour (`ScreenTourUITests`) launches debug builds with `-screenTourSamples`: screens
/// with nothing waiting (Who's this, Voice Scoring) then show an inert sample so their buttons can
/// be reviewed. Release builds never do.
enum ScreenTour {
    static var samples: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("-screenTourSamples")
        #else
        false
        #endif
    }
}
