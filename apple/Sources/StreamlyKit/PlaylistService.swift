import Foundation
import Security

public protocol SecretStore: Sendable {
    func set(_ secret: String, for key: String) throws
    func get(_ key: String) -> String?
    func delete(_ key: String)
}

public struct KeychainStore: SecretStore {
    public struct Failure: LocalizedError {
        public let status: OSStatus
        public var errorDescription: String? { "Le trousseau a refusé l'enregistrement (code \(status))." }
    }

    private let service: String

    public init(service: String = "com.streamly.mac.playlists") {
        self.service = service
    }

    private func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: key]
    }

    public func set(_ secret: String, for key: String) throws {
        let data = Data(secret.utf8)
        let status = SecItemUpdate(query(key) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw Failure(status: status) }
        var attributes = query(key)
        attributes[kSecValueData as String] = data
        let added = SecItemAdd(attributes as CFDictionary, nil)
        guard added == errSecSuccess else { throw Failure(status: added) }
    }

    public func get(_ key: String) -> String? {
        var search = query(key)
        search[kSecReturnData as String] = true
        search[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(search as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func delete(_ key: String) {
        SecItemDelete(query(key) as CFDictionary)
    }
}

public enum ListOrder: String, Codable, CaseIterable, Sendable {
    case provider, ascending, descending

    public func apply<Item>(_ items: [Item], name: (Item) -> String) -> [Item] {
        guard self != .provider else { return items }
        let keyed = items.map { (name($0), $0) }
        let wanted: ComparisonResult = self == .ascending ? .orderedAscending : .orderedDescending
        return keyed.sorted { $0.0.localizedStandardCompare($1.0) == wanted }.map(\.1)
    }
}

public struct PlaylistPreferences: Codable, Sendable, Equatable {
    public var favorites: [String] = []
    public var recents: [String] = []
    public var lastChannel: String?
    public var maxHeight: Int?
    public var order: ListOrder = .provider
    /// Names the user gave to the provider's categories, by category id. They survive a refresh.
    public var categoryNames: [String: String] = [:]

    public init() {}

    public static let recentsLimit = 30

    public mutating func rename(category id: String, to name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        categoryNames[id] = trimmed.isEmpty ? nil : trimmed
    }

    public mutating func toggleFavorite(_ id: String) {
        if let index = favorites.firstIndex(of: id) {
            favorites.remove(at: index)
        } else {
            favorites.append(id)
        }
    }

    public mutating func markPlayed(_ id: String) {
        recents.removeAll { $0 == id }
        recents.insert(id, at: 0)
        if recents.count > Self.recentsLimit { recents.removeLast(recents.count - Self.recentsLimit) }
        lastChannel = id
    }
}

/// Plain files in Application Support, readable by the owner only. M3U entries can embed
/// credentials in their URLs, so the library cache is treated as sensitive.
public struct LocalStore: Sendable {
    public let directory: URL

    public init(directory: URL? = nil) throws {
        let base = try directory ?? FileManager.default
            .url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("Streamly", isDirectory: true)
        try FileManager.default.createDirectory(
            at: base, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        self.directory = base
    }

    private func file(_ name: String) -> URL { directory.appendingPathComponent(name) }

    public func load<T: Decodable>(_ type: T.Type, from name: String) -> T? {
        guard let data = try? Data(contentsOf: file(name)) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .secondsSince1970
        return try? decoder.decode(type, from: data)
    }

    public func save<T: Encodable>(_ value: T, to name: String) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .secondsSince1970
        let url = file(name)
        try encoder.encode(value).write(to: url, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    public func remove(_ name: String) {
        try? FileManager.default.removeItem(at: file(name))
    }
}

/// What the user typed in the "add a subscription" form.
public enum PlaylistInput: Sendable {
    case xtream(name: String, host: String, username: String, password: String)
    case m3u(name: String, link: String)
}

public struct SyncResult: Sendable {
    public let playlist: Playlist
    public let secret: String
    public let library: Library
}

public struct PlaylistService: Sendable {
    public enum Stage: Sendable {
        case checkingAccount, downloadingChannels
    }

    private let session: URLSession

    public init(session: URLSession = .shared) {
        self.session = session
    }

    /// Checks the account and downloads the channels. Saves nothing: the caller stores the result
    /// only once it succeeded, so a refused password never ends up in the Keychain.
    public func add(_ input: PlaylistInput, progress: @Sendable @escaping (Stage) -> Void = { _ in }) async throws -> SyncResult {
        switch input {
        case let .xtream(name, host, username, password):
            let cleanHost = Self.normalizeHost(host)
            let playlist = Playlist(
                name: name.isEmpty ? (URL(string: cleanHost)?.host ?? "Abonnement") : name,
                kind: .xtream(host: cleanHost, username: username.trimmingCharacters(in: .whitespaces)))
            return try await sync(playlist, secret: password, progress: progress)

        case let .m3u(name, link):
            let trimmed = link.trimmingCharacters(in: .whitespacesAndNewlines)
            if let account = XtreamCredentials.fromM3ULink(trimmed) {
                return try await add(.xtream(name: name, host: account.host, username: account.username,
                                             password: account.password), progress: progress)
            }
            guard let url = URL(string: trimmed), let scheme = url.scheme?.lowercased(),
                  scheme == "http" || scheme == "https", let host = url.host else {
                throw ProviderError.invalidAddress
            }
            let playlist = Playlist(name: name.isEmpty ? host : name, kind: .m3u)
            return try await sync(playlist, secret: trimmed, progress: progress)
        }
    }

    public func sync(_ original: Playlist, secret: String,
                     progress: @Sendable @escaping (Stage) -> Void = { _ in }) async throws -> SyncResult {
        var playlist = original
        let library: Library

        switch playlist.kind {
        case let .xtream(host, username):
            let client = XtreamClient(host: host, username: username, password: secret,
                                      userAgent: playlist.userAgent, session: session)
            progress(.checkingAccount)
            playlist.account = try await client.account()
            progress(.downloadingChannels)
            library = try await client.library()

        case .m3u:
            guard let url = URL(string: secret) else { throw ProviderError.invalidAddress }
            progress(.downloadingChannels)
            let data = try await HTTP.get(url, userAgent: playlist.userAgent, session: session)
            library = M3UParser.library(from: M3UParser.parse(M3UParser.decode(data)))
        }

        guard !library.streams.isEmpty else { throw ProviderError.emptyPlaylist }
        playlist.lastSync = Date()
        return SyncResult(playlist: playlist, secret: secret, library: library)
    }

    public static func normalizeHost(_ raw: String) -> String {
        var host = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while host.hasSuffix("/") { host.removeLast() }
        if !host.lowercased().hasPrefix("http://"), !host.lowercased().hasPrefix("https://"), !host.isEmpty {
            host = "http://" + host
        }
        return host
    }
}

/// Short guide on demand, with a cache and at most a few requests at once: a scrolling list must
/// not flood the provider.
public actor EPGService {
    private struct Entry {
        let programmes: [Programme]
        let fetchedAt: Date
    }

    private let client: XtreamClient
    private var cache: [String: Entry] = [:]
    private var inFlight: [String: Task<[Programme], Never>] = [:]
    private var running = 0
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private let maxConcurrent = 4
    private let lifetime: TimeInterval = 15 * 60

    public init(client: XtreamClient) {
        self.client = client
    }

    public func programmes(streamID: String, now: Date = Date()) async -> [Programme] {
        if let entry = cache[streamID], now.timeIntervalSince(entry.fetchedAt) < lifetime,
           entry.programmes.contains(where: { $0.stop > now }) || entry.programmes.isEmpty {
            return entry.programmes
        }
        if let task = inFlight[streamID] { return await task.value }

        let task = Task<[Programme], Never> { [client] in
            await self.acquire()
            let result = (try? await client.shortEPG(streamID: streamID)) ?? []
            self.release()
            return result
        }
        inFlight[streamID] = task
        let result = await task.value
        inFlight[streamID] = nil
        cache[streamID] = Entry(programmes: result, fetchedAt: now)
        return result
    }

    private func acquire() async {
        if running < maxConcurrent {
            running += 1
            return
        }
        await withCheckedContinuation { waiters.append($0) }
    }

    private func release() {
        if waiters.isEmpty {
            running -= 1
        } else {
            waiters.removeFirst().resume()
        }
    }
}
