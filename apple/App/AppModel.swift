import Foundation
import Observation
import StreamlyKit

enum SidebarItem: Hashable {
    case all
    case favorites
    case recents
    case category(String)
}

enum SyncState: Equatable {
    case idle
    case running(String)
    case failed(String)
}

@MainActor
@Observable
final class AppModel {
    private(set) var playlists: [Playlist] = []
    private(set) var active: Playlist?
    private(set) var categories: [ChannelCategory] = []
    private(set) var channels: [Channel] = []
    private(set) var visibleChannels: [Channel] = []
    private(set) var preferences = PlaylistPreferences()
    private(set) var sync: SyncState = .idle
    private(set) var startupError: String?

    var sidebar: SidebarItem = .all {
        didSet { if sidebar != oldValue { refreshVisible() } }
    }
    var query = "" {
        didSet { if query != oldValue { refreshVisible() } }
    }
    var showingSettings = false
    var showingAddPlaylist = false

    let playback = PlaybackController()

    private let secrets: SecretStore
    private let service = PlaylistService()
    private var store: LocalStore?
    private var epg: EPGService?
    private var channelsByID: [String: Channel] = [:]

    private static let playlistsFile = "playlists.json"
    private static let activeFile = "active.json"
    private func libraryFile(_ id: UUID) -> String { "library-\(id.uuidString).json" }
    private func preferencesFile(_ id: UUID) -> String { "preferences-\(id.uuidString).json" }

    init(store injected: LocalStore? = nil, secrets: SecretStore = KeychainStore()) {
        self.secrets = secrets
        do {
            store = try injected ?? LocalStore()
        } catch {
            startupError = "Impossible de créer le dossier de l'application : \(error.localizedDescription)"
            return
        }
        playlists = store?.load([Playlist].self, from: Self.playlistsFile) ?? []
        let activeID = store?.load(UUID.self, from: Self.activeFile)
        if let playlist = playlists.first(where: { $0.id == activeID }) ?? playlists.first {
            activate(playlist)
        }
    }

    var hasPlaylist: Bool { !playlists.isEmpty }

    func count(in item: SidebarItem) -> Int {
        switch item {
        case .all: return channels.count
        case .favorites: return preferences.favorites.count
        case .recents: return preferences.recents.count
        case .category(let id): return channels.lazy.filter { $0.categoryIDs.contains(id) }.count
        }
    }

    func isFavorite(_ channel: Channel) -> Bool {
        preferences.favorites.contains(channel.id)
    }

    // MARK: Playlists

    func add(_ input: PlaylistInput) async -> Bool {
        sync = .running("Vérification de l'abonnement…")
        do {
            let result = try await service.add(input) { stage in
                Task { @MainActor [weak self] in
                    self?.sync = .running(stage == .checkingAccount
                        ? "Vérification de l'abonnement…" : "Téléchargement des chaînes…")
                }
            }
            try secrets.set(result.secret, for: result.playlist.id.uuidString)
            try store?.save(result.library, to: libraryFile(result.playlist.id))
            playlists.append(result.playlist)
            try savePlaylists()
            activate(result.playlist)
            sync = .idle
            return true
        } catch {
            sync = .failed(error.localizedDescription)
            return false
        }
    }

    func resync(_ playlist: Playlist) async {
        guard let secret = secrets.get(playlist.id.uuidString) else {
            sync = .failed("Le mot de passe de cet abonnement n'est plus dans le trousseau. Supprimez-le puis ajoutez-le de nouveau.")
            return
        }
        sync = .running("Actualisation de « \(playlist.name) »…")
        do {
            let result = try await service.sync(playlist, secret: secret)
            try store?.save(result.library, to: libraryFile(playlist.id))
            if let index = playlists.firstIndex(where: { $0.id == playlist.id }) {
                playlists[index] = result.playlist
            }
            try savePlaylists()
            if active?.id == playlist.id { activate(result.playlist) }
            sync = .idle
        } catch {
            sync = .failed(error.localizedDescription)
        }
    }

    func activate(_ playlist: Playlist) {
        if active?.id != playlist.id { playback.stop() }
        active = playlist
        try? store?.save(playlist.id, to: Self.activeFile)
        preferences = store?.load(PlaylistPreferences.self, from: preferencesFile(playlist.id)) ?? PlaylistPreferences()

        let library = store?.load(Library.self, from: libraryFile(playlist.id))
        categories = library?.categories ?? []
        channels = ChannelGrouper.group(library?.streams ?? [])
        channelsByID = Dictionary(channels.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        // Keep only the categories that actually list a channel.
        let used = Set(channels.flatMap(\.categoryIDs))
        categories = categories.filter { used.contains($0.id) }

        if case .xtream(let host, let username) = playlist.kind, let password = secrets.get(playlist.id.uuidString) {
            epg = EPGService(client: XtreamClient(host: host, username: username, password: password,
                                                  userAgent: playlist.userAgent))
        } else {
            epg = nil
        }
        if case .category(let id) = sidebar, !used.contains(id) { sidebar = .all }
        refreshVisible()
    }

    func remove(_ playlist: Playlist) {
        if active?.id == playlist.id { playback.stop() }
        secrets.delete(playlist.id.uuidString)
        store?.remove(libraryFile(playlist.id))
        store?.remove(preferencesFile(playlist.id))
        playlists.removeAll { $0.id == playlist.id }
        try? savePlaylists()

        if active?.id == playlist.id {
            active = nil
            channels = []
            categories = []
            channelsByID = [:]
            preferences = PlaylistPreferences()
            epg = nil
            refreshVisible()
            if let next = playlists.first { activate(next) }
        }
    }

    private func savePlaylists() throws {
        try store?.save(playlists, to: Self.playlistsFile)
    }

    // MARK: Channels

    private func refreshVisible() {
        let base: [Channel]
        switch sidebar {
        case .all: base = channels
        case .favorites: base = preferences.favorites.compactMap { channelsByID[$0] }
        case .recents: base = preferences.recents.compactMap { channelsByID[$0] }
        case .category(let id): base = channels.filter { $0.categoryIDs.contains(id) }
        }
        let filtered = ChannelGrouper.filter(base, query: query)
        // Recents keep their own meaning: the last one watched comes first.
        visibleChannels = sidebar == .recents ? filtered : preferences.order.apply(filtered, name: \.name)
    }

    func setOrder(_ order: ListOrder) {
        preferences.order = order
        savePreferences()
        refreshVisible()
    }

    func name(of category: ChannelCategory) -> String {
        preferences.categoryNames[category.id] ?? category.name
    }

    var orderedCategories: [ChannelCategory] {
        preferences.order.apply(categories, name: name(of:))
    }

    func rename(_ category: ChannelCategory, to name: String) {
        preferences.rename(category: category.id, to: name)
        savePreferences()
    }

    func toggleFavorite(_ channel: Channel) {
        preferences.toggleFavorite(channel.id)
        savePreferences()
        if sidebar == .favorites { refreshVisible() }
    }

    private func savePreferences() {
        guard let active else { return }
        try? store?.save(preferences, to: preferencesFile(active.id))
    }

    func nowNext(for channel: Channel) async -> NowNext? {
        guard let epg, let stream = channel.variants.first(where: { !$0.isBackup })?.stream else { return nil }
        let programmes = await epg.programmes(streamID: stream.id)
        return programmes.isEmpty ? nil : NowNext(programmes: programmes, at: Date())
    }

    // MARK: Playback

    func play(_ channel: Channel) {
        guard let active else { return }
        let secret = secrets.get(active.id.uuidString) ?? ""
        let attempts = StreamURL.attempts(for: channel, playlist: active, secret: secret,
                                          maxHeight: preferences.maxHeight)
        preferences.markPlayed(channel.id)
        savePreferences()
        if sidebar == .recents { refreshVisible() }
        playback.play(channel: channel, attempts: attempts, userAgent: active.userAgent)
    }

    /// Next or previous channel in the current list, wrapping around.
    func zap(by offset: Int) {
        let list = visibleChannels
        guard !list.isEmpty else { return }
        guard let current = playback.channel, let index = list.firstIndex(of: current) else {
            play(list[0])
            return
        }
        let next = (index + offset + list.count) % list.count
        playback.zapDebounced { [weak self] in self?.play(list[next]) }
        playback.announce(list[next])
    }

    func playPrevious() {
        guard preferences.recents.count > 1, let channel = channelsByID[preferences.recents[1]] else { return }
        play(channel)
    }

    func resumeLastChannel() {
        guard let id = preferences.lastChannel, let channel = channelsByID[id] else { return }
        play(channel)
    }

    func setMaxHeight(_ height: Int?) {
        preferences.maxHeight = height
        savePreferences()
    }

    func setUserAgent(_ userAgent: String, for playlist: Playlist) {
        guard let index = playlists.firstIndex(where: { $0.id == playlist.id }) else { return }
        playlists[index].userAgent = userAgent.isEmpty ? Playlist.defaultUserAgent : userAgent
        try? savePlaylists()
        if active?.id == playlist.id { activate(playlists[index]) }
    }

    func clearSyncError() {
        if case .failed = sync { sync = .idle }
    }
}
