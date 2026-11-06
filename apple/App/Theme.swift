import StreamlyKit
import SwiftUI

/// Same palette as the web app (`web/app.css`): night blue, violet, a red LIVE badge, no green.
enum Theme {
    static let background = Color(hex: 0x08080F)
    static let surface = Color(hex: 0x11111B)
    static let surface2 = Color(hex: 0x181826)
    static let surface3 = Color(hex: 0x202032)
    static let line = Color.white.opacity(0.08)
    static let text = Color(hex: 0xF4F3FA)
    static let muted = Color(red: 235 / 255, green: 232 / 255, blue: 250 / 255).opacity(0.6)
    static let faint = Color(red: 235 / 255, green: 232 / 255, blue: 250 / 255).opacity(0.38)
    static let accent = Color(hex: 0xA78BFA)
    static let violet = Color(hex: 0x8B5CF6)
    static let violetDeep = Color(hex: 0x7C3AED)
    static let live = Color(hex: 0xFF3B5C)
    static let danger = Color(hex: 0xFF6B81)
    static let gradient = LinearGradient(
        colors: [Color(hex: 0x7C3AED), Color(hex: 0xA855F7)], startPoint: .leading, endPoint: .trailing)
}

/// The interface is in French whatever the Mac's region: mixed-language dates look like a bug.
enum Format {
    private static let french = Locale(identifier: "fr_FR")

    static func date(_ date: Date) -> String {
        date.formatted(.dateTime.day().month(.wide).year().locale(french))
    }

    static func time(_ date: Date) -> String {
        date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits).locale(french))
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255)
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .background(Theme.gradient, in: RoundedRectangle(cornerRadius: 9))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

struct QualityBadge: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 9, weight: .bold))
            .foregroundStyle(Theme.muted)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(Theme.surface3, in: RoundedRectangle(cornerRadius: 4))
    }
}

struct LiveBadge: View {
    var body: some View {
        Text("DIRECT")
            .font(.system(size: 10, weight: .bold))
            .tracking(0.6)
            .foregroundStyle(.white)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(Theme.live, in: RoundedRectangle(cornerRadius: 4))
    }
}

/// Provider logo, or the channel's initial while it loads or when there is none.
struct ChannelLogo: View {
    let channel: Channel
    var size: CGFloat = 36

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 7).fill(Theme.surface3)
            if let logo = channel.logo, let url = URL(string: logo) {
                AsyncImage(url: url) { phase in
                    if case .success(let image) = phase {
                        image.resizable().scaledToFit().padding(3)
                    } else {
                        initial
                    }
                }
            } else {
                initial
            }
        }
        .frame(width: size, height: size)
    }

    private var initial: some View {
        Text(channel.name.prefix(1).uppercased())
            .font(.system(size: size * 0.42, weight: .semibold))
            .foregroundStyle(Theme.muted)
    }
}
