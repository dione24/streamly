import Foundation

public enum PlaylistKind: Codable, Hashable, Sendable {
    case xtream(host: String, username: String)
    case m3u
}

public struct AccountInfo: Codable, Hashable, Sendable {
    public var status: String?
    public var expiresAt: Date?
    public var maxConnections: Int?
    public var activeConnections: Int?
    public var serverTimeZone: String?

    public init(status: String? = nil, expiresAt: Date? = nil,
                maxConnections: Int? = nil, activeConnections: Int? = nil, serverTimeZone: String? = nil) {
        self.status = status
        self.expiresAt = expiresAt
        self.maxConnections = maxConnections
        self.activeConnections = activeConnections
        self.serverTimeZone = serverTimeZone
    }
}

/// A subscription. The password (Xtream) or link (M3U) lives in the Keychain, never here.
public struct Playlist: Codable, Identifiable, Hashable, Sendable {
    public static let defaultUserAgent = "VLC/3.0.20 LibVLC/3.0.20"

    public var id: UUID
    public var name: String
    public var kind: PlaylistKind
    public var userAgent: String
    public var lastSync: Date?
    public var account: AccountInfo?

    public init(id: UUID = UUID(), name: String, kind: PlaylistKind,
                userAgent: String = Playlist.defaultUserAgent) {
        self.id = id
        self.name = name
        self.kind = kind
        self.userAgent = userAgent
    }
}

public struct ChannelCategory: Codable, Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String

    public init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

/// One provider entry: a channel usually has several (SD, HD, FHD, [BK]).
public struct LiveStream: Codable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let categoryID: String?
    public let logo: String?
    public let epgID: String?
    public let number: Int?
    /// M3U only: the entry's own URL. Xtream URLs are rebuilt so the password is never cached.
    public let url: String?
    /// Set when the provider keeps past programmes of this channel: how many days back.
    public let archiveDays: Int?

    public init(id: String, name: String, categoryID: String? = nil, logo: String? = nil,
                epgID: String? = nil, number: Int? = nil, url: String? = nil, archiveDays: Int? = nil) {
        self.id = id
        self.name = name
        self.categoryID = categoryID
        self.logo = logo
        self.epgID = epgID
        self.number = number
        self.url = url
        self.archiveDays = archiveDays
    }
}

public struct Library: Codable, Sendable {
    public var categories: [ChannelCategory]
    public var streams: [LiveStream]

    public init(categories: [ChannelCategory], streams: [LiveStream]) {
        self.categories = categories
        self.streams = streams
    }
}

public struct ChannelVariant: Hashable, Sendable {
    public let stream: LiveStream
    public let quality: String?
    public let height: Int
    public let isBackup: Bool
}

public struct Channel: Identifiable, Hashable, Sendable {
    /// "lang|canonical", the same key the engine uses for favorites.
    public let id: String
    public let name: String
    public let lang: String?
    public let logo: String?
    public let number: Int?
    public let categoryIDs: Set<String>
    public let variants: [ChannelVariant]
    public let searchKey: String

    public var bestQuality: String? {
        variants.first(where: { !$0.isBackup })?.quality
    }

    public var primaryStream: LiveStream? { variants.first?.stream }

    /// The variant that can replay past programmes, if any.
    public var archiveStream: LiveStream? {
        variants.first { $0.stream.archiveDays != nil }?.stream
    }

    public static func == (lhs: Channel, rhs: Channel) -> Bool { lhs.id == rhs.id }
    public func hash(into hasher: inout Hasher) { hasher.combine(id) }
}

public struct Programme: Codable, Hashable, Sendable {
    public let title: String
    public let summary: String?
    public let start: Date
    public let stop: Date

    public init(title: String, summary: String? = nil, start: Date, stop: Date) {
        self.title = title
        self.summary = summary
        self.start = start
        self.stop = stop
    }

    public func progress(at now: Date) -> Double {
        let length = stop.timeIntervalSince(start)
        guard length > 0 else { return 0 }
        return min(max(now.timeIntervalSince(start) / length, 0), 1)
    }
}

public struct NowNext: Hashable, Sendable {
    public let now: Programme?
    public let next: Programme?

    public init(programmes: [Programme], at date: Date) {
        let sorted = programmes.sorted { $0.start < $1.start }
        now = sorted.first { $0.start <= date && date < $0.stop }
        next = sorted.first { $0.start > date }
    }
}
