import Foundation

public struct PlaybackAttempt: Equatable, Sendable {
    public let url: String
    public let label: String

    public init(url: String, label: String) {
        self.url = url
        self.label = label
    }
}

/// What to do when a stream fails or ends: retry, move to the next variant, then give up.
///
/// Panels usually allow one connection and refuse (401) a reconnection for 1 to 2 seconds after the
/// previous one closed, so every step waits. A live stream never ends on its own: a drop after
/// real playback is a network hiccup and starts over on the same variant.
public struct PlaybackPlan: Sendable {
    public enum Step: Equatable, Sendable {
        case attempt(PlaybackAttempt, after: TimeInterval)
        case exhausted
    }

    public static let reconnectDelay: TimeInterval = 2
    public static let stablePlayback: TimeInterval = 20

    public let attempts: [PlaybackAttempt]
    private let retriesPerVariant: Int
    public private(set) var index = 0
    private var failuresOnCurrent = 0

    public init(attempts: [PlaybackAttempt], retriesPerVariant: Int = 1) {
        self.attempts = attempts
        self.retriesPerVariant = retriesPerVariant
    }

    public var current: PlaybackAttempt? {
        attempts.indices.contains(index) ? attempts[index] : nil
    }

    public mutating func failed(afterPlaying played: TimeInterval = 0) -> Step {
        guard let current else { return .exhausted }
        if played >= Self.stablePlayback {
            failuresOnCurrent = 0
            return .attempt(current, after: Self.reconnectDelay)
        }
        failuresOnCurrent += 1
        if failuresOnCurrent <= retriesPerVariant {
            return .attempt(current, after: Self.reconnectDelay)
        }
        index += 1
        failuresOnCurrent = 0
        guard let next = self.current else { return .exhausted }
        return .attempt(next, after: Self.reconnectDelay)
    }
}

public enum StreamURL {
    /// Raw MPEG-TS over HTTP, what the panels serve most reliably.
    public static func xtreamLive(host: String, username: String, password: String, streamID: String) -> String {
        let trimmed = host.hasSuffix("/") ? String(host.dropLast()) : host
        return "\(trimmed)/\(encode(username))/\(encode(password))/\(encode(streamID))"
    }

    public static func attempts(for channel: Channel, playlist: Playlist, secret: String,
                                maxHeight: Int? = nil) -> [PlaybackAttempt] {
        ChannelGrouper.sorted(channel.variants, maxHeight: maxHeight).compactMap { variant in
            let label = variant.isBackup ? "secours" : (variant.quality ?? "auto")
            switch playlist.kind {
            case .xtream(let host, let username):
                return PlaybackAttempt(
                    url: xtreamLive(host: host, username: username, password: secret,
                                    streamID: variant.stream.id),
                    label: label)
            case .m3u:
                return variant.stream.url.map { PlaybackAttempt(url: $0, label: label) }
            }
        }
    }

    static func encode(_ component: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#")
        return component.addingPercentEncoding(withAllowedCharacters: allowed) ?? component
    }
}

/// Hides subscription credentials before a URL reaches a log or a message.
public enum Redact {
    private static let query = try! NSRegularExpression(
        pattern: "((?:username|password|user|pass|token)=)[^&\\s]+", options: .caseInsensitive)
    private static let xtreamPath = try! NSRegularExpression(
        pattern: "(https?://[^/\\s]+/(?:live/|movie/|series/|timeshift/)?)[^/\\s]+/[^/\\s]+/([^/\\s]+)")

    public static func secrets(in text: String) -> String {
        var out = query.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "$1***")
        out = xtreamPath.stringByReplacingMatches(
            in: out, range: NSRange(out.startIndex..., in: out), withTemplate: "$1***/***/$2")
        return out
    }
}
