import SwiftUI

/// The pendant, drawn like the app icon, reflecting what the app is doing:
/// breathing teal LED and sound rings while listening, a sweeping ring while
/// syncing flash, dimmed while waiting for a pendant.
struct PendantOrb: View {
    enum Activity { case listening, quiet, syncing, connecting, waiting }
    let activity: Activity
    @State private var phase = false

    var body: some View {
        ZStack {
            if activity == .listening {
                ForEach(0..<3) { i in
                    Circle()
                        .stroke(Theme.teal.opacity(0.35 - Double(i) * 0.1), lineWidth: 2)
                        .scaleEffect(phase ? 1.45 + Double(i) * 0.25 : 1.0)
                        .opacity(phase ? 0 : 1)
                        .animation(.easeOut(duration: 2.4).repeatForever(autoreverses: false)
                                    .delay(Double(i) * 0.8), value: phase)
                }
            }
            if activity == .syncing {
                Circle()
                    .trim(from: 0, to: 0.28)
                    .stroke(Theme.teal, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                    .frame(width: 168, height: 168)
                    .rotationEffect(.degrees(phase ? 360 : 0))
                    .animation(.linear(duration: 1.1).repeatForever(autoreverses: false), value: phase)
            }
            Circle()
                .fill(RadialGradient(colors: [Theme.metalLight, Theme.metalDark],
                                     center: UnitPoint(x: 0.35, y: 0.3), startRadius: 4, endRadius: 110))
                .overlay(Circle().stroke(.white.opacity(0.6), lineWidth: 1.5).padding(1))
                .overlay(Circle().stroke(.black.opacity(0.15), lineWidth: 1).padding(14))
                .frame(width: 140, height: 140)
                .shadow(color: .black.opacity(0.5), radius: 18, y: 10)
                .saturation(activity == .waiting || activity == .quiet ? 0.3 : 1)
                .opacity(activity == .waiting ? 0.6 : 1)
            Circle()
                .fill(activity == .waiting ? Color.gray : Theme.teal)
                .frame(width: 10, height: 10)
                .shadow(color: Theme.teal.opacity(activity == .waiting ? 0 : 0.9), radius: phase ? 10 : 4)
                .offset(y: 38)
                .animation(.easeInOut(duration: 1.6).repeatForever(), value: phase)
        }
        .frame(width: 220, height: 220)
        .onAppear { phase = true }
        .accessibilityLabel(Text(label))
    }

    var label: String {
        switch activity {
        case .listening: "Listening"
        case .quiet: "Connected, no audio"
        case .syncing: "Syncing pendant memory"
        case .connecting: "Connecting"
        case .waiting: "Waiting for a pendant"
        }
    }
}
