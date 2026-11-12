import SwiftUI

// Tokens and components live in Core/DesignSystem.swift; this file keeps the transcript text helpers.

/// Preview text whose `**Name:**` labels read as chips (inline, so non-interactive: the row opens the conversation).
/// `terms` are search words to highlight in the words.
func chippedPreview(_ text: String, font: Font, terms: [String] = []) -> Text {
    var result = Text("")
    for (n, seg) in Reassign.previewSegments(text).enumerated() {
        if n > 0 { result = result + Text(" ") }
        switch seg {
        case .name(let name):
            var a = highlightTerms(name, terms: terms, font: font)
            a.foregroundColor = Theme.teal
            a.backgroundColor = Theme.accentWash
            a.font = font.weight(.semibold)
            for r in Reassign.highlightRanges(in: name, terms: terms) {
                if let ar = Range(NSRange(r, in: name), in: a) {
                    a[ar].foregroundColor = Theme.onAccent
                    a[ar].backgroundColor = Theme.teal
                }
            }
            result = result + Text(Image(systemName: "person.fill")).foregroundColor(Theme.teal) + Text(" ") + Text(a)
        case .words(let w):
            result = result + Text(highlightTerms(w, terms: terms, font: font))
        }
    }
    return result
}

func highlightTerms(_ s: String, terms: [String], font: Font) -> AttributedString {
    var a = AttributedString(s)
    for r in Reassign.highlightRanges(in: s, terms: terms) {
        if let ar = Range(NSRange(r, in: s), in: a) {
            a[ar].foregroundColor = Theme.teal
            a[ar].font = font.weight(.semibold)
        }
    }
    return a
}
