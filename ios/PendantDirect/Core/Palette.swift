import Foundation

/// An sRGB colour, 0...1 per channel. The design system's colours are defined here, free of
/// SwiftUI, so `swift test` can check their contrast.
public struct RGB: Equatable, Hashable, CustomStringConvertible {
    public let r: Double, g: Double, b: Double

    public init(_ r: Double, _ g: Double, _ b: Double) { self.r = r; self.g = g; self.b = b }

    /// `0xRRGGBB`.
    public init(hex: UInt32) {
        self.init(Double((hex >> 16) & 0xFF) / 255, Double((hex >> 8) & 0xFF) / 255, Double(hex & 0xFF) / 255)
    }

    /// This colour at `alpha` laid over `background`, as one solid colour.
    public func over(_ background: RGB, alpha: Double) -> RGB {
        RGB(r * alpha + background.r * (1 - alpha),
            g * alpha + background.g * (1 - alpha),
            b * alpha + background.b * (1 - alpha))
    }

    /// WCAG 2.x relative luminance.
    public var luminance: Double {
        func lin(_ c: Double) -> Double { c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    }

    /// WCAG contrast ratio between two colours (1...21).
    public static func contrast(_ a: RGB, _ b: RGB) -> Double {
        let (hi, lo) = (max(a.luminance, b.luminance), min(a.luminance, b.luminance))
        return (hi + 0.05) / (lo + 0.05)
    }

    public var description: String {
        String(format: "#%02X%02X%02X", Int((r * 255).rounded()), Int((g * 255).rounded()), Int((b * 255).rounded()))
    }
}

/// The app's colour tokens. The app is always dark: a night-blue gradient with teal as the accent.
public enum Palette {
    // Background: the night gradient, top to bottom.
    public static let nightTop = RGB(0.10, 0.13, 0.25)
    public static let nightBottom = RGB(0.03, 0.04, 0.10)

    // Surfaces that sit on the night background. Solid, so their contrast is known.
    /// Cards, list rows, timeline rows.
    public static let surface = RGB(hex: 0x222B4C)
    /// Things on a card: badges, chips that are off, text fields, secondary buttons.
    public static let raised = RGB(hex: 0x2D3863)
    /// Rows set back (media and fragments waiting for review).
    public static let sunken = RGB(hex: 0x131932)

    // Accent and status colours (also used as text).
    public static let accent = RGB(0.43, 0.86, 0.90)          // teal
    public static let accentPressed = RGB(hex: 0x58C3CE)
    public static let destructive = RGB(hex: 0xFF8A80)
    public static let star = RGB(hex: 0xFFD54F)
    public static let warning = RGB(hex: 0xFFB45C)
    public static let success = RGB(hex: 0x6EE7A0)
    /// Swipe-action fills. iOS draws their labels white, so these are darker than `star` and `destructive`.
    public static let swipeStar = RGB(hex: 0x8A6500)
    public static let swipeDelete = RGB(hex: 0xC62828)

    // Tinted surfaces: a colour washed over `surface` (or the night top), flattened to one colour.
    /// Selected chips, speaker chips, marked lines.
    public static let accentWash = accent.over(surface, alpha: 0.22)
    /// The transcript line being played.
    public static let playingWash = accent.over(nightTop, alpha: 0.14)
    /// Starred lines and star badges.
    public static let starWash = star.over(surface, alpha: 0.15)
    /// Destructive buttons.
    public static let destructiveWash = destructive.over(surface, alpha: 0.16)

    // Text.
    public static let textPrimary = RGB(hex: 0xF4F6FB)
    public static let textSecondary = RGB(hex: 0xBCC4DB)
    public static let textTertiary = RGB(hex: 0xA0AAC6)
    /// A disabled button's label: grey, not teal, so it reads as off, but still readable (>= 3:1).
    public static let disabledText = RGB(hex: 0x8C95B2)
    /// A disabled button's fill (every kind but tertiary, which stays clear).
    public static let disabledFill = RGB(hex: 0x262F52)
    /// Text on a filled accent (primary buttons).
    public static let onAccent = RGB(hex: 0x06222A)
    /// The reassign sheet's unavailable scope option: its title and its reason, on the list row (`surface`).
    public static let unavailableOption = (title: textTertiary, reason: textSecondary, surface: surface)
    /// A disabled button (the reassign sheet's confirm before a name is picked): its label on its fill.
    public static let disabledButton = (label: disabledText, fill: disabledFill)

    /// What iOS draws on swipe actions.
    public static let systemWhite = RGB(1, 1, 1)

    /// `.disabled`: a disabled control's label. WCAG exempts it; the app still keeps it at 3:1.
    public enum TextSize: String { case body, large, disabled }

    /// A text token drawn on a surface token.
    public struct Pairing: CustomStringConvertible {
        public let text: String, textColor: RGB
        public let surface: String, surfaceColor: RGB
        /// `.large` only where the token is used exclusively for large (>= 18 pt) or bold (>= 14 pt) text.
        public let size: TextSize
        public var ratio: Double { RGB.contrast(textColor, surfaceColor) }
        public var minimum: Double { size == .body ? 4.5 : 3.0 }   // .large and .disabled: 3:1
        public var description: String { "\(text) on \(surface): \(String(format: "%.2f", ratio)):1 (min \(minimum))" }
    }

    public static let surfaces: [String: RGB] = [
        "nightTop": nightTop, "nightBottom": nightBottom, "surface": surface, "raised": raised, "sunken": sunken,
        "accentWash": accentWash, "disabledFill": disabledFill, "playingWash": playingWash, "starWash": starWash, "destructiveWash": destructiveWash,
        "accent": accent, "accentPressed": accentPressed, "swipeStar": swipeStar, "swipeDelete": swipeDelete,
    ]

    public static let texts: [String: RGB] = [
        "textPrimary": textPrimary, "textSecondary": textSecondary, "textTertiary": textTertiary,
        "accent": accent, "destructive": destructive, "star": star, "warning": warning, "success": success,
        "onAccent": onAccent, "systemWhite": systemWhite, "disabledText": disabledText,
    ]

    /// Where each text token appears, from the views (foreground on the background it is drawn over).
    /// Transcript lines sit on `nightTop`/`nightBottom`, or on `starWash` (starred), `accentWash`
    /// (marked) or `playingWash` (playing); their time stamp (teal, or a teal chip on `accentWash`)
    /// and star icon sit on the same. Every pairing here is checked by `PaletteTests`.
    public static let usage: [(text: String, surfaces: [String], size: TextSize)] = [
        // body text, titles, chips that are off, name fields, transcript lines
        ("textPrimary", ["nightTop", "nightBottom", "surface", "raised", "sunken", "accentWash", "playingWash",
                         "starWash", "destructiveWash"], .body),
        // details, previews, section headers, badges (on raised), meta lines
        ("textSecondary", ["nightTop", "nightBottom", "surface", "raised", "sunken", "accentWash", "playingWash",
                           "starWash"], .body),
        // device names, days left, card footers, placeholders, review-row previews
        ("textTertiary", ["nightTop", "nightBottom", "surface", "raised", "sunken"], .body),
        // buttons (secondary on raised, pressed on accentWash, tertiary on any surface), chips that are on,
        // speaker chips, preview names, time stamps on every transcript line background, the picker's
        // circle badges (on accentWash)
        ("accent", ["nightTop", "nightBottom", "surface", "raised", "sunken", "accentWash", "playingWash",
                    "starWash"], .body),
        // destructive button, battery <= 10 %, Forget row, swipe NO badge
        ("destructive", ["nightTop", "nightBottom", "surface", "raised", "destructiveWash"], .body),
        // star badges, notable stars on rows, star icons on transcript lines of every kind
        ("star", ["nightTop", "nightBottom", "surface", "raised", "sunken", "starWash", "accentWash",
                  "playingWash"], .body),
        // errors and memory warnings (on sunken: the reassign confirm bar and the Undo row)
        ("warning", ["nightTop", "nightBottom", "surface", "raised", "sunken"], .body),
        // Voice Scoring's YES badge
        ("success", ["nightTop", "nightBottom", "surface"], .body),
        // primary buttons, highlighted search terms inside a name chip
        ("onAccent", ["accent", "accentPressed"], .body),
        // disabled buttons: on their fill, and tertiary (no fill) on any surface
        ("disabledText", ["disabledFill", "nightTop", "nightBottom", "surface", "raised", "sunken"], .disabled),
        // swipe-action labels (drawn by iOS)
        ("systemWhite", ["swipeStar", "swipeDelete"], .body),
    ]

    public static var pairings: [Pairing] {
        usage.flatMap { u in
            u.surfaces.map { s in
                Pairing(text: u.text, textColor: texts[u.text]!, surface: s, surfaceColor: surfaces[s]!, size: u.size)
            }
        }
    }
}
