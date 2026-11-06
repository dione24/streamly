import AppKit
import Foundation
import Observation
import StreamlyKit

enum AppSection: Hashable, CaseIterable {
    case direct, guide, movies, series, search

    var title: String {
        switch self {
        case .direct: return "Direct"
        case .guide: return "Guide TV"
        case .movies: return "Films"
        case .series: return "Séries"
        case .search: return "Rechercher"
        }
    }

    var icon: String {
        switch self {
        case .direct: return "dot.radiowaves.left.and.right"
        case .guide: return "calendar"
        case .movies: return "film"
        case .series: return "play.rectangle.on.rectangle"
        case .search: return "magnifyingglass"
        }
    }
}

enum SidebarItem: Hashable {
    case all
    case favorites
    case recents
    case category(String)
}

/// Where the picture is shown. The mini window takes it over while the viewer browses.
enum VideoLocation: Equatable {
    case main
    case mini
}

enum SyncState: Equatable {
    case idle
    case running(String)
    case failed(String)
}

/// One catalog screen: everything is computed up front, a provider can list 60,000 titles.
struct CatalogSection {
    var all: [CatalogEntry] = []
    var byID: [String: CatalogEntry] = [:]
    var categories: [ChannelCategory] = []
    var counts: [String: Int] = [:]
    var filter: SidebarItem = .all
    var query = ""
    var visible: [CatalogEntry] = []
    var opened: CatalogEntry?
}

struct GlobalResults: Equatable {
    var channels: [Channel] = []
    var movies: [CatalogEntry] = []
    var series: [CatalogEntry] = []

    var isEmpty: Bool { channels.isEmpty && movies.isEmpty && series.isEmpty }
}

private struct LoadedLibrary: Sendable {
    var channels: [Channel] = []
    var categories: [ChannelCategory] = []
    var counts: [String: Int] = [:]
    var movies = CatalogSeed()
    var series = CatalogSeed()
    var flatEpisodes: [String: [Episode]] = [:]
    var hasCatalogFile = false
}

private struct CatalogSeed: Sendable {
    var entries: [CatalogEntry] = []
    var categories: [ChannelCategory] = []
    var counts: [String: Int] = [:]
}

@MainActor
@Observable
final class AppModel {
    private(set) var playlists: [Playlist] = []
    private(set) var active: Playlist?
    private(set) var categories: [ChannelCategory] = []
    private(set) var channels: [Channel] = []
    private(set) var visibleChannels: [Channel] = []
    private(set) var liveCounts: [String: Int] = [:]
    private(set) var preferences = PlaylistPreferences()
    private(set) var sync: SyncState = .idle
    private(set) var startupError: String?
    private(set) var isLoadingLibrary = false
    private(set) var movies = CatalogSection()
    private(set) var series = CatalogSection()
    private(set) var globalResults = GlobalResults()
    /// Window is in full screen. The sidebar only goes away for the live player.
    private(set) var isFullScreen = false
    private(set) var videoLocation: VideoLocation = .main

    var section: AppSection = .direct
    var sidebar: SidebarItem = .all {
        didSet { if sidebar != oldValue { refreshVisible() } }
    }
    var query = "" {
        didSet { if query != oldValue { refreshVisible() } }
    }
    var globalQuery = "" {
        didSet { if globalQuery != oldValue { scheduleGlobalSearch() } }
    }
    var showingSettings = false
    var showingAddPlaylist = false

    let playback = PlaybackController()

    private let secrets: SecretStore
    private let service = PlaylistService()
    private var store: LocalStore?
    private var epg: EPGService?
    private var client: XtreamClient?
    private var channelsByID: [String: Channel] = [:]
    private var flatEpisodes: [String: [Episode]] = [:]
    private var seriesDetails: [String: SeriesDetail] = [:]
    private var globalSearch: Task<Void, Never>?

    private static let playlistsFile = "playlists.json"
    private static let activeFile = "active.json"
    private func libraryFile(_ id: UUID) -> String { "library-\(id.uuidString).json" }
    private func catalogFile(_ id: UUID) -> String { "catalog-\(id.uuidString).json" }
    private func preferencesFile(_ id: UUID) -> String { "preferences-\(id.uuidString).json" }

    init(store injected: LocalStore? = nil, secrets: SecretStore = KeychainStore()) {
        self.secrets = secrets
        do {
            store = try injected ?? LocalStore()
        } catch {
            startupError = "Impossible de créer le dossier de l'application : \(error.localizedDescription)"
            return
        }
        playback.onProgress = { [weak self] item, position, duration in
            self?.recordProgress(item, position: position, duration: duration)
        }
        playlists = store?.load([Playlist].self, from: Self.playlistsFile) ?? []
        let activeID = store?.load(UUID.self, from: Self.activeFile)
        if let playlist = playlists.first(where: { $0.id == activeID }) ?? playlists.first {
            Task { await activate(playlist) }
        }
    }

    var hasPlaylist: Bool { !playlists.isEmpty }

    func count(in item: SidebarItem) -> Int {
        switch item {
        case .all: return channels.count
        case .favorites: return preferences.favorites.count
        case .recents: return preferences.recents.count
        case .category(let id): return liveCounts[id] ?? 0
        }
    }

    func isFavorite(_ channel: Channel) -> Bool {
        preferences.favorites.contains(channel.id)
    }

    // MARK: Playlists

    func add(_ input: PlaylistInput) async -> Bool {
        sync = .running("Vérification de l'abonnement…")
        do {
            let result = try await service.add(input, progress: progressReporter())
            try secrets.set(result.secret, for: result.playlist.id.uuidString)
            try await save(result)
            playlists.append(result.playlist)
            try savePlaylists()
            await activate(result.playlist)
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
            let result = try await service.sync(playlist, secret: secret, progress: progressReporter())
            try await save(result)
            if let index = playlists.firstIndex(where: { $0.id == playlist.id }) {
                playlists[index] = result.playlist
            }
            try savePlaylists()
            if active?.id == playlist.id { await activate(result.playlist) }
            sync = .idle
        } catch {
            sync = .failed(error.localizedDescription)
        }
    }

    private func progressReporter() -> @Sendable (PlaylistService.Stage) -> Void {
        { [weak self] stage in
            Task { @MainActor in
                switch stage {
                case .checkingAccount: self?.sync = .running("Vérification de l'abonnement…")
                case .downloadingChannels: self?.sync = .running("Téléchargement des chaînes…")
                case .downloadingCatalog: self?.sync = .running("Téléchargement des films et des séries…")
                }
            }
        }
    }

    private func save(_ result: SyncResult) async throws {
        let (store, libraryFile, catalogFile) = (self.store, libraryFile(result.playlist.id), catalogFile(result.playlist.id))
        try await Task.detached(priority: .userInitiated) {
            try store?.save(result.library, to: libraryFile)
            try store?.save(result.vod, to: catalogFile)
        }.value
    }

    /// Reading and grouping a large bouquet takes seconds: never on the main thread.
    func activate(_ playlist: Playlist) async {
        if active?.id != playlist.id { playback.stop() }
        active = playlist
        try? store?.save(playlist.id, to: Self.activeFile)
        preferences = store?.load(PlaylistPreferences.self, from: preferencesFile(playlist.id)) ?? PlaylistPreferences()
        seriesDetails = [:]
        isLoadingLibrary = true

        let (store, libraryFile, catalogFile) = (self.store, libraryFile(playlist.id), catalogFile(playlist.id))
        let loaded = await Task.detached(priority: .userInitiated) { () -> LoadedLibrary in
            Self.load(store: store, libraryFile: libraryFile, catalogFile: catalogFile)
        }.value
        guard active?.id == playlist.id else { return }

        channels = loaded.channels
        categories = loaded.categories
        liveCounts = loaded.counts
        channelsByID = Dictionary(channels.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        flatEpisodes = loaded.flatEpisodes
        movies = makeSection(loaded.movies, keeping: movies)
        series = makeSection(loaded.series, keeping: series)

        if case .xtream(let host, let username) = playlist.kind, let password = secrets.get(playlist.id.uuidString) {
            let client = XtreamClient(host: host, username: username, password: password, userAgent: playlist.userAgent)
            self.client = client
            epg = EPGService(client: client)
        } else {
            client = nil
            epg = nil
        }
        if case .category(let id) = sidebar, liveCounts[id] == nil { sidebar = .all }
        refreshVisible()
        refreshCatalog(.movie)
        refreshCatalog(.series)
        isLoadingLibrary = false

        // A subscription added before films and series existed has no catalog on disk yet.
        if !loaded.hasCatalogFile, sync == .idle {
            Task { await resync(playlist) }
        }
    }

    private nonisolated static func load(store: LocalStore?, libraryFile: String, catalogFile: String) -> LoadedLibrary {
        var loaded = LoadedLibrary()
        let library = store?.load(Library.self, from: libraryFile)
        loaded.channels = ChannelGrouper.group(library?.streams ?? [])
        for channel in loaded.channels {
            for category in channel.categoryIDs { loaded.counts[category, default: 0] += 1 }
        }
        // Keep only the categories that actually list a channel.
        loaded.categories = (library?.categories ?? []).filter { loaded.counts[$0.id] != nil }

        guard let catalog = store?.load(VODLibrary.self, from: catalogFile) else { return loaded }
        loaded.hasCatalogFile = true
        loaded.flatEpisodes = catalog.flatEpisodes
        loaded.movies = seed(catalog.movies.map(CatalogEntry.init(movie:)), categories: catalog.movieCategories)
        loaded.series = seed(catalog.series.map(CatalogEntry.init(series:)), categories: catalog.seriesCategories)
        return loaded
    }

    private nonisolated static func seed(_ entries: [CatalogEntry], categories: [ChannelCategory]) -> CatalogSeed {
        var counts: [String: Int] = [:]
        for entry in entries {
            if let category = entry.categoryID { counts[category, default: 0] += 1 }
        }
        return CatalogSeed(entries: entries, categories: categories.filter { counts[$0.id] != nil }, counts: counts)
    }

    private func makeSection(_ seed: CatalogSeed, keeping previous: CatalogSection) -> CatalogSection {
        var section = CatalogSection()
        section.all = seed.entries
        section.byID = Dictionary(seed.entries.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        section.categories = seed.categories
        section.counts = seed.counts
        section.query = previous.query
        if case .category(let id) = previous.filter, seed.counts[id] == nil {
            section.filter = .all
        } else {
            section.filter = previous.filter
        }
        return section
    }

    func remove(_ playlist: Playlist) async {
        if active?.id == playlist.id { playback.stop() }
        secrets.delete(playlist.id.uuidString)
        store?.remove(libraryFile(playlist.id))
        store?.remove(catalogFile(playlist.id))
        store?.remove(preferencesFile(playlist.id))
        playlists.removeAll { $0.id == playlist.id }
        try? savePlaylists()

        if active?.id == playlist.id {
            active = nil
            channels = []
            categories = []
            liveCounts = [:]
            channelsByID = [:]
            flatEpisodes = [:]
            movies = CatalogSection()
            series = CatalogSection()
            preferences = PlaylistPreferences()
            epg = nil
            client = nil
            refreshVisible()
            if let next = playlists.first { await activate(next) }
        }
    }

    private func savePlaylists() throws {
        try store?.save(playlists, to: Self.playlistsFile)
    }

    private func savePreferences() {
        guard let active else { return }
        try? store?.save(preferences, to: preferencesFile(active.id))
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
        refreshCatalog(.movie)
        refreshCatalog(.series)
    }

    func name(of category: ChannelCategory, in section: String = "live") -> String {
        preferences.categoryNames["\(section):\(category.id)"] ?? category.name
    }

    var orderedCategories: [ChannelCategory] {
        preferences.order.apply(categories) { name(of: $0) }
    }

    func rename(_ category: ChannelCategory, in section: String = "live", to name: String) {
        preferences.rename(category: "\(section):\(category.id)", to: name)
        savePreferences()
    }

    func toggleFavorite(_ channel: Channel) {
        preferences.toggleFavorite(channel.id)
        savePreferences()
        if sidebar == .favorites { refreshVisible() }
    }

    func programmes(for channel: Channel) async -> [Programme] {
        guard let epg, let stream = channel.variants.first(where: { !$0.isBackup })?.stream else { return [] }
        return await epg.programmes(streamID: stream.id)
    }

    func nowNext(for channel: Channel) async -> NowNext? {
        let programmes = await programmes(for: channel)
        return programmes.isEmpty ? nil : NowNext(programmes: programmes, at: Date())
    }

    // MARK: Live playback

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

    func setUserAgent(_ userAgent: String, for playlist: Playlist) async {
        guard let index = playlists.firstIndex(where: { $0.id == playlist.id }) else { return }
        playlists[index].userAgent = userAgent.isEmpty ? Playlist.defaultUserAgent : userAgent
        try? savePlaylists()
        if active?.id == playlist.id { await activate(playlists[index]) }
    }

    func clearSyncError() {
        if case .failed = sync { sync = .idle }
    }

    // MARK: Catch-up

    func archive(for channel: Channel) async -> [Programme] {
        guard let client, let stream = channel.archiveStream else { return [] }
        return (try? await client.archive(streamID: stream.id)) ?? []
    }

    func replay(_ channel: Channel, programme: Programme) {
        guard let active, case .xtream(let host, let username) = active.kind,
              let stream = channel.archiveStream, let password = secrets.get(active.id.uuidString) else { return }
        let timeZone = active.account?.serverTimeZone.flatMap(TimeZone.init(identifier:))
        let item = OnDemandItem(
            progressKey: nil,
            title: programme.title,
            subtitle: "\(channel.name) · \(Format.date(programme.start)) à \(Format.time(programme.start))",
            poster: channel.logo,
            url: StreamURL.catchUp(host: host, username: username, password: password, streamID: stream.id,
                                   programme: programme, timeZone: timeZone))
        playback.play(item: item, userAgent: active.userAgent)
    }

    // MARK: Catalog

    func catalog(_ kind: CatalogEntry.Kind) -> CatalogSection {
        kind == .movie ? movies : series
    }

    private func updateCatalog(_ kind: CatalogEntry.Kind, _ change: (inout CatalogSection) -> Void) {
        if kind == .movie { change(&movies) } else { change(&series) }
    }

    func setFilter(_ item: SidebarItem, for kind: CatalogEntry.Kind) {
        updateCatalog(kind) {
            $0.filter = item
            $0.opened = nil
        }
        refreshCatalog(kind)
    }

    func setCatalogQuery(_ text: String, for kind: CatalogEntry.Kind) {
        updateCatalog(kind) { $0.query = text }
        refreshCatalog(kind)
    }

    func open(_ entry: CatalogEntry?, kind: CatalogEntry.Kind) {
        updateCatalog(kind) { $0.opened = entry }
    }

    func orderedCategories(_ kind: CatalogEntry.Kind) -> [ChannelCategory] {
        preferences.order.apply(catalog(kind).categories) { name(of: $0, in: kind.rawValue) }
    }

    func catalogCount(in item: SidebarItem, kind: CatalogEntry.Kind) -> Int {
        let section = catalog(kind)
        switch item {
        case .all: return section.all.count
        case .favorites: return savedEntries(kind).count
        case .recents: return continueWatching(kind).count
        case .category(let id): return section.counts[id] ?? 0
        }
    }

    private func savedEntries(_ kind: CatalogEntry.Kind) -> [CatalogEntry] {
        let section = catalog(kind)
        return preferences.savedEntries.compactMap { section.byID[$0] }
    }

    private func refreshCatalog(_ kind: CatalogEntry.Kind) {
        let section = catalog(kind)
        let base: [CatalogEntry]
        switch section.filter {
        case .all: base = section.all
        case .favorites: base = savedEntries(kind)
        case .recents: base = continueWatching(kind).compactMap { entry(for: $0) }
        case .category(let id): base = section.all.filter { $0.categoryID == id }
        }
        let filtered = CatalogEntry.filter(base, query: section.query)
        let visible = section.filter == .recents ? filtered : preferences.order.apply(filtered, name: \.title)
        updateCatalog(kind) { $0.visible = visible }
    }

    func isSaved(_ entry: CatalogEntry) -> Bool {
        preferences.savedEntries.contains(entry.id)
    }

    func toggleSaved(_ entry: CatalogEntry) {
        preferences.toggleSaved(entry.id)
        savePreferences()
        refreshCatalog(entry.kind)
    }

    func progress(for entry: CatalogEntry) -> WatchProgress? {
        guard entry.kind == .movie else { return nil }
        return preferences.progress[WatchProgress.movieKey(entry.remoteID)]
    }

    func progress(for episode: Episode) -> WatchProgress? {
        preferences.progress[WatchProgress.episodeKey(episode.id)]
    }

    func setWatched(_ watched: Bool, entry: CatalogEntry) {
        preferences.setWatched(watched, template: WatchProgress(
            key: WatchProgress.movieKey(entry.remoteID), title: entry.title, poster: entry.poster,
            position: 0, duration: 1))
        savePreferences()
        refreshCatalog(entry.kind)
    }

    func setWatched(_ watched: Bool, episode: Episode, of entry: CatalogEntry) {
        preferences.setWatched(watched, template: WatchProgress(
            key: WatchProgress.episodeKey(episode.id), title: entry.title, subtitle: episode.label,
            poster: entry.poster, position: 0, duration: 1, seriesID: entry.remoteID))
        savePreferences()
    }

    func continueWatching(_ kind: CatalogEntry.Kind) -> [WatchProgress] {
        preferences.continueWatching.filter { (kind == .series) == ($0.seriesID != nil) }
    }

    /// The poster to open for a "continue watching" item.
    func entry(for progress: WatchProgress) -> CatalogEntry? {
        if let seriesID = progress.seriesID { return series.byID["series:\(seriesID)"] }
        return movies.byID[progress.key]
    }

    func movieDetail(_ entry: CatalogEntry) async -> MovieDetail? {
        guard let client else { return nil }
        return try? await client.movieDetail(id: entry.remoteID)
    }

    func seriesDetail(_ entry: CatalogEntry) async -> SeriesDetail? {
        if let cached = seriesDetails[entry.remoteID] { return cached }
        var detail: SeriesDetail?
        if let flat = flatEpisodes[entry.remoteID] {
            var built = SeriesDetail()
            built.plot = entry.series?.plot
            built.episodes = flat.sorted { ($0.season, $0.number) < ($1.season, $1.number) }
            detail = built
        } else if let client {
            detail = try? await client.seriesDetail(id: entry.remoteID)
        }
        if let detail { seriesDetails[entry.remoteID] = detail }
        return detail
    }

    func play(movie entry: CatalogEntry, detail: MovieDetail?, fromStart: Bool = false) {
        guard let active, let movie = entry.movie else { return }
        let url: String?
        if let own = movie.url {
            url = own
        } else if case .xtream(let host, let username) = active.kind, let password = secrets.get(active.id.uuidString) {
            url = StreamURL.movie(host: host, username: username, password: password, id: movie.id,
                                  container: detail?.container ?? movie.container)
        } else {
            url = nil
        }
        guard let url else { return }
        let key = WatchProgress.movieKey(movie.id)
        let subtitle = [detail?.year, detail?.genre].compactMap { $0 }.joined(separator: " · ")
        playback.play(item: OnDemandItem(
            progressKey: key,
            title: entry.title,
            subtitle: subtitle.isEmpty ? nil : subtitle,
            poster: entry.poster,
            url: url,
            resumeAt: fromStart ? nil : preferences.progress[key]?.resumePosition), userAgent: active.userAgent)
    }

    func play(episode: Episode, of entry: CatalogEntry, fromStart: Bool = false) {
        guard let active else { return }
        let url: String?
        if let own = episode.url {
            url = own
        } else if case .xtream(let host, let username) = active.kind, let password = secrets.get(active.id.uuidString) {
            url = StreamURL.episode(host: host, username: username, password: password, id: episode.id,
                                    container: episode.container)
        } else {
            url = nil
        }
        guard let url else { return }
        let key = WatchProgress.episodeKey(episode.id)
        playback.play(item: OnDemandItem(
            progressKey: key,
            title: entry.title,
            subtitle: "\(episode.label) · \(episode.title)",
            poster: entry.poster,
            url: url,
            resumeAt: fromStart ? nil : preferences.progress[key]?.resumePosition,
            seriesID: entry.remoteID,
            episode: episode), userAgent: active.userAgent)
    }

    var nextEpisode: Episode? {
        guard let item = playback.onDemand, let seriesID = item.seriesID, let episode = item.episode else { return nil }
        return seriesDetails[seriesID]?.episode(after: episode)
    }

    func playNextEpisode() {
        guard let next = nextEpisode, let seriesID = playback.onDemand?.seriesID,
              let entry = series.byID["series:\(seriesID)"] else { return }
        play(episode: next, of: entry)
    }

    /// Opens a "continue watching" item where it stopped.
    func resume(_ progress: WatchProgress) async {
        guard let entry = entry(for: progress) else { return }
        if entry.kind == .movie {
            play(movie: entry, detail: await movieDetail(entry))
        } else if let detail = await seriesDetail(entry),
                  let episode = detail.episodes.first(where: { WatchProgress.episodeKey($0.id) == progress.key }) {
            play(episode: episode, of: entry)
        }
    }

    func closeOnDemand() {
        playback.stop()
        setFullScreen(false)
    }

    private func recordProgress(_ item: OnDemandItem, position: Double, duration: Double) {
        guard let key = item.progressKey else { return }
        preferences.progress[key] = WatchProgress(
            key: key, title: item.title, subtitle: item.subtitle, poster: item.poster,
            position: position, duration: duration, seriesID: item.seriesID)
        savePreferences()
    }

    // MARK: Global search

    private func scheduleGlobalSearch() {
        globalSearch?.cancel()
        let text = globalQuery
        guard !text.trimmingCharacters(in: .whitespaces).isEmpty else {
            globalResults = GlobalResults()
            return
        }
        let (channels, movies, series) = (self.channels, self.movies.all, self.series.all)
        globalSearch = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(150))
            guard !Task.isCancelled else { return }
            let results = await Task.detached(priority: .userInitiated) {
                GlobalResults(
                    channels: Array(ChannelGrouper.filter(channels, query: text).prefix(30)),
                    movies: Array(CatalogEntry.filter(movies, query: text).prefix(30)),
                    series: Array(CatalogEntry.filter(series, query: text).prefix(30)))
            }.value
            guard !Task.isCancelled else { return }
            self?.globalResults = results
        }
    }

    // MARK: Full screen

    func setFullScreen(_ value: Bool) {
        guard let window = NSApp.windows.first(where: { $0.isVisible && $0.canBecomeMain }) else { return }
        if window.styleMask.contains(.fullScreen) != value {
            window.toggleFullScreen(nil)
        }
    }

    func noteFullScreen(_ value: Bool) {
        isFullScreen = value
    }
}
