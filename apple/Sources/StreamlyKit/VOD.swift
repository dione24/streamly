import Foundation

public struct Movie: Codable, Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let categoryID: String?
    public let poster: String?
    public let rating: Double?
    public let container: String?
    /// M3U only: the entry's own URL.
    public let url: String?

    public init(id: String, name: String, categoryID: String? = nil, poster: String? = nil,
                rating: Double? = nil, container: String? = nil, url: String? = nil) {
        self.id = id
        self.name = name
        self.categoryID = categoryID
        self.poster = poster
        self.rating = rating
        self.container = container
        self.url = url
    }
}

public struct Series: Codable, Identifiable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let categoryID: String?
    public let poster: String?
    public let rating: Double?
    public let plot: String?

    public init(id: String, name: String, categoryID: String? = nil, poster: String? = nil,
                rating: Double? = nil, plot: String? = nil) {
        self.id = id
        self.name = name
        self.categoryID = categoryID
        self.poster = poster
        self.rating = rating
        self.plot = plot
    }
}

public struct Episode: Codable, Identifiable, Hashable, Sendable {
    public let id: String
    public let season: Int
    public let number: Int
    public let title: String
    public let container: String
    public let plot: String?
    public let durationSeconds: Int?
    public let still: String?
    /// M3U only: the entry's own URL.
    public let url: String?

    public init(id: String, season: Int, number: Int, title: String, container: String = "mp4",
                plot: String? = nil, durationSeconds: Int? = nil, still: String? = nil, url: String? = nil) {
        self.id = id
        self.season = season
        self.number = number
        self.title = title
        self.container = container
        self.plot = plot
        self.durationSeconds = durationSeconds
        self.still = still
        self.url = url
    }

    public var label: String { "S\(season) · É\(number)" }
}

public struct MovieDetail: Sendable, Equatable {
    public var plot: String?
    public var cast: String?
    public var director: String?
    public var genre: String?
    public var year: String?
    public var durationSeconds: Int?
    public var rating: Double?
    public var backdrop: String?
    public var container: String?

    public init() {}
}

public struct SeriesDetail: Sendable, Equatable {
    public var plot: String?
    public var cast: String?
    public var genre: String?
    public var year: String?
    public var backdrop: String?
    public var episodes: [Episode] = []

    public init() {}

    public var seasons: [Int] {
        Array(Set(episodes.map(\.season))).sorted()
    }

    public func episodes(inSeason season: Int) -> [Episode] {
        episodes.filter { $0.season == season }
    }

    /// The episode after this one, crossing into the next season if needed.
    public func episode(after episode: Episode) -> Episode? {
        guard let index = episodes.firstIndex(where: { $0.id == episode.id }),
              episodes.indices.contains(index + 1) else { return nil }
        return episodes[index + 1]
    }
}

public struct VODLibrary: Codable, Sendable {
    public var movieCategories: [ChannelCategory]
    public var seriesCategories: [ChannelCategory]
    public var movies: [Movie]
    public var series: [Series]
    /// M3U only: a flat playlist has no series endpoint, its episodes are kept here.
    public var flatEpisodes: [String: [Episode]]

    public init(movieCategories: [ChannelCategory] = [], seriesCategories: [ChannelCategory] = [],
                movies: [Movie] = [], series: [Series] = [], flatEpisodes: [String: [Episode]] = [:]) {
        self.movieCategories = movieCategories
        self.seriesCategories = seriesCategories
        self.movies = movies
        self.series = series
        self.flatEpisodes = flatEpisodes
    }
}

/// A movie or a series as the catalog screens see it: cleaned title and search key computed once,
/// because a provider can list 60,000 of them.
public struct CatalogEntry: Identifiable, Hashable, Sendable {
    public enum Kind: String, Sendable { case movie, series }

    public let id: String
    public let kind: Kind
    public let remoteID: String
    public let title: String
    public let poster: String?
    public let rating: Double?
    public let categoryID: String?
    public let searchKey: String
    public let movie: Movie?
    public let series: Series?

    public static func == (lhs: CatalogEntry, rhs: CatalogEntry) -> Bool { lhs.id == rhs.id }
    public func hash(into hasher: inout Hasher) { hasher.combine(id) }

    public init(movie: Movie) {
        let title = Self.clean(movie.name)
        id = "movie:\(movie.id)"
        kind = .movie
        remoteID = movie.id
        self.title = title
        poster = movie.poster
        rating = movie.rating
        categoryID = movie.categoryID
        searchKey = ChannelGrouper.searchKey(title)
        self.movie = movie
        series = nil
    }

    public init(series: Series) {
        let title = Self.clean(series.name)
        id = "series:\(series.id)"
        kind = .series
        remoteID = series.id
        self.title = title
        poster = series.poster
        rating = series.rating
        categoryID = series.categoryID
        searchKey = ChannelGrouper.searchKey(title)
        self.series = series
        movie = nil
    }

    private static func clean(_ name: String) -> String {
        let label = ChannelName.displayName(name)
        return label.isEmpty ? name : label
    }

    public static func filter(_ entries: [CatalogEntry], query: String) -> [CatalogEntry] {
        let words = ChannelGrouper.searchKey(query).split(whereSeparator: \.isWhitespace)
        guard !words.isEmpty else { return entries }
        return entries.filter { entry in
            words.allSatisfy { entry.searchKey.contains($0) }
        }
    }
}

/// Where the viewer stopped in a movie or an episode.
public struct WatchProgress: Codable, Hashable, Sendable {
    /// Below this, nothing worth resuming; above the ratio, the credits are rolling.
    public static let minimumResume: Double = 30
    public static let finishedRatio = 0.92

    public let key: String
    public var title: String
    public var subtitle: String?
    public var poster: String?
    public var position: Double
    public var duration: Double
    public var updatedAt: Date
    /// Set on episodes: lets "continue watching" reopen the series.
    public var seriesID: String?

    public init(key: String, title: String, subtitle: String? = nil, poster: String? = nil,
                position: Double, duration: Double, updatedAt: Date = Date(), seriesID: String? = nil) {
        self.key = key
        self.title = title
        self.subtitle = subtitle
        self.poster = poster
        self.position = position
        self.duration = duration
        self.updatedAt = updatedAt
        self.seriesID = seriesID
    }

    public var isFinished: Bool {
        duration > 0 && position / duration >= Self.finishedRatio
    }

    public var fraction: Double {
        duration > 0 ? min(max(position / duration, 0), 1) : 0
    }

    public var resumePosition: Double? {
        guard !isFinished, position >= Self.minimumResume else { return nil }
        return position
    }

    public static func movieKey(_ id: String) -> String { "movie:\(id)" }
    public static func episodeKey(_ id: String) -> String { "episode:\(id)" }
}

public enum Duration {
    /// Panels give "5400", 5400 or "01:30:00".
    public static func seconds(_ value: Any?) -> Int? {
        if let number = Lenient.int(value), number > 0 { return number }
        guard let text = Lenient.string(value) else { return nil }
        let parts = text.split(separator: ":").compactMap { Int($0) }
        guard parts.count >= 2 else { return nil }
        let total = parts.reduce(0) { $0 * 60 + $1 }
        return total > 0 ? total : nil
    }

    /// "1 h 30", "42 min".
    public static func label(seconds: Int) -> String {
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes) min" }
        return String(format: "%d h %02d", minutes / 60, minutes % 60)
    }

    /// "1:02:03", "12:34".
    public static func clock(_ value: Double) -> String {
        let total = max(Int(value.isFinite ? value : 0), 0)
        let (h, m, s) = (total / 3600, total % 3600 / 60, total % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}

extension XtreamClient {
    /// Movies and series in one go. A panel may offer neither: each failure just leaves that part empty.
    public func vodLibrary() async -> VODLibrary {
        async let movieCategories = categories(action: "get_vod_categories")
        async let seriesCategories = categories(action: "get_series_categories")
        let rawMovies = (try? await api(action: "get_vod_streams") as? [Any]) ?? []
        let rawSeries = (try? await api(action: "get_series") as? [Any]) ?? []

        let movies = rawMovies.compactMap { item -> Movie? in
            guard let row = item as? [String: Any],
                  let id = Lenient.string(row["stream_id"]),
                  let name = Lenient.string(row["name"]) else { return nil }
            return Movie(id: id, name: name,
                         categoryID: Lenient.string(row["category_id"]),
                         poster: Lenient.string(row["stream_icon"]),
                         rating: positive(Lenient.double(row["rating"])),
                         container: Lenient.string(row["container_extension"]))
        }
        let series = rawSeries.compactMap { item -> Series? in
            guard let row = item as? [String: Any],
                  let id = Lenient.string(row["series_id"]),
                  let name = Lenient.string(row["name"]) else { return nil }
            return Series(id: id, name: name,
                          categoryID: Lenient.string(row["category_id"]),
                          poster: Lenient.string(row["cover"]),
                          rating: positive(Lenient.double(row["rating"])),
                          plot: Lenient.string(row["plot"]))
        }
        return VODLibrary(movieCategories: await movieCategories, seriesCategories: await seriesCategories,
                          movies: movies, series: series)
    }

    public func movieDetail(id: String) async throws -> MovieDetail {
        let payload = try await api(action: "get_vod_info",
                                    extra: [URLQueryItem(name: "vod_id", value: id)]) as? [String: Any]
        let info = payload?["info"] as? [String: Any] ?? [:]
        let data = payload?["movie_data"] as? [String: Any] ?? [:]
        var detail = MovieDetail()
        detail.plot = Lenient.string(info["plot"]) ?? Lenient.string(info["description"])
        detail.cast = Lenient.string(info["cast"]) ?? Lenient.string(info["actors"])
        detail.director = Lenient.string(info["director"])
        detail.genre = Lenient.string(info["genre"])
        detail.year = Self.year(info["releasedate"] ?? info["release_date"])
        detail.durationSeconds = Duration.seconds(info["duration_secs"]) ?? Duration.seconds(info["duration"])
        detail.rating = positive(Lenient.double(info["rating"]))
        detail.backdrop = Self.firstString(info["backdrop_path"])
        detail.container = Lenient.string(data["container_extension"])
        return detail
    }

    public func seriesDetail(id: String) async throws -> SeriesDetail {
        let payload = try await api(action: "get_series_info",
                                    extra: [URLQueryItem(name: "series_id", value: id)])
        return Self.parseSeries(payload)
    }

    /// `episodes` has no stable shape: usually an object keyed by season, sometimes a list of lists,
    /// and the season number lives either in the episode or only in the key.
    static func parseSeries(_ payload: Any) -> SeriesDetail {
        var detail = SeriesDetail()
        guard let root = payload as? [String: Any] else { return detail }
        let info = root["info"] as? [String: Any] ?? [:]
        detail.plot = Lenient.string(info["plot"]) ?? Lenient.string(info["description"])
        detail.cast = Lenient.string(info["cast"])
        detail.genre = Lenient.string(info["genre"])
        detail.year = year(info["releaseDate"] ?? info["releasedate"] ?? info["release_date"])
        detail.backdrop = firstString(info["backdrop_path"])

        var buckets: [(String, [Any])] = []
        if let byKey = root["episodes"] as? [String: Any] {
            buckets = byKey.compactMap { key, value in (value as? [Any]).map { (key, $0) } }
        } else if let lists = root["episodes"] as? [Any] {
            buckets = lists.enumerated().compactMap { index, value in
                (value as? [Any]).map { (String(index + 1), $0) }
            }
        }

        var episodes: [Episode] = []
        for (key, group) in buckets {
            for item in group {
                guard let row = item as? [String: Any], let id = Lenient.string(row["id"]) else { continue }
                let meta = row["info"] as? [String: Any] ?? [:]
                let number = Lenient.int(row["episode_num"]) ?? 0
                let extensionName = Lenient.string(row["container_extension"]) ?? "mp4"
                episodes.append(Episode(
                    id: id,
                    season: Lenient.int(row["season"]) ?? Int(key) ?? 0,
                    number: number,
                    title: Lenient.string(row["title"]).map { $0.trimmingCharacters(in: .whitespaces) }
                        ?? "Épisode \(number)",
                    // Without the exact extension the URL answers 404: mp4 is only the common default.
                    container: extensionName.hasPrefix(".") ? String(extensionName.dropFirst()) : extensionName,
                    plot: Lenient.string(meta["plot"]) ?? Lenient.string(meta["description"]),
                    durationSeconds: Duration.seconds(meta["duration_secs"]) ?? Duration.seconds(meta["duration"]),
                    still: Lenient.string(meta["movie_image"])))
            }
        }
        detail.episodes = episodes.sorted { ($0.season, $0.number) < ($1.season, $1.number) }
        return detail
    }

    /// Past programmes this channel can replay.
    public func archive(streamID: String) async throws -> [Programme] {
        let payload = try await api(action: "get_simple_data_table",
                                    extra: [URLQueryItem(name: "stream_id", value: streamID)])
        let rows = ((payload as? [String: Any])?["epg_listings"] as? [Any]) ?? []
        let now = Date()
        return rows.compactMap { item -> Programme? in
            guard let row = item as? [String: Any], Lenient.string(row["has_archive"]) == "1",
                  let start = Lenient.double(row["start_timestamp"]),
                  let stop = Lenient.double(row["stop_timestamp"] ?? row["end_timestamp"]),
                  let title = Lenient.string(row["title"]).map(Lenient.decodeBase64) else { return nil }
            let programme = Programme(
                title: title, summary: Lenient.string(row["description"]).map(Lenient.decodeBase64),
                start: Date(timeIntervalSince1970: start), stop: Date(timeIntervalSince1970: stop))
            return programme.stop <= now ? programme : nil
        }.sorted { $0.start > $1.start }
    }

    private func categories(action: String) async -> [ChannelCategory] {
        let rows = (try? await api(action: action) as? [Any]) ?? []
        return rows.compactMap { item in
            guard let row = item as? [String: Any],
                  let id = Lenient.string(row["category_id"]),
                  let name = Lenient.string(row["category_name"]), !name.isEmpty else { return nil }
            return ChannelCategory(id: id, name: name)
        }
    }

    private func positive(_ value: Double?) -> Double? {
        guard let value, value > 0 else { return nil }
        return value
    }

    private static func year(_ value: Any?) -> String? {
        guard let text = Lenient.string(value), text.count >= 4 else { return nil }
        return String(text.prefix(4))
    }

    /// `backdrop_path` is a string on some panels and a list on others.
    private static func firstString(_ value: Any?) -> String? {
        if let list = value as? [Any] { return list.lazy.compactMap { Lenient.string($0) }.first }
        return Lenient.string(value)
    }
}

extension StreamURL {
    public static func movie(host: String, username: String, password: String, id: String, container: String?) -> String {
        file(section: "movie", host: host, username: username, password: password, id: id, container: container)
    }

    /// Episodes are not served under /movie/: a panel answers 404 there, without saying why.
    public static func episode(host: String, username: String, password: String, id: String, container: String?) -> String {
        file(section: "series", host: host, username: username, password: password, id: id, container: container)
    }

    /// The start time is read by the panel in its own time zone, not the viewer's.
    public static func catchUp(host: String, username: String, password: String, streamID: String,
                               programme: Programme, timeZone: TimeZone?) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone ?? TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd:HH-mm"
        let minutes = max(Int((programme.stop.timeIntervalSince(programme.start) / 60).rounded(.up)), 1)
        let trimmed = host.hasSuffix("/") ? String(host.dropLast()) : host
        return "\(trimmed)/timeshift/\(encode(username))/\(encode(password))/\(minutes)/\(formatter.string(from: programme.start))/\(encode(streamID)).ts"
    }

    private static func file(section: String, host: String, username: String, password: String,
                             id: String, container: String?) -> String {
        let trimmed = host.hasSuffix("/") ? String(host.dropLast()) : host
        let ext = (container?.isEmpty == false ? container! : "mp4")
        return "\(trimmed)/\(section)/\(encode(username))/\(encode(password))/\(encode(id)).\(encode(ext))"
    }
}

extension M3UParser {
    private static let episodePattern = try! NSRegularExpression(
        pattern: #"^(.*?)[\s._-]*S(\d{1,2})[\s._-]*E(\d{1,3})\b"#, options: .caseInsensitive)

    /// Movies and series of a flat playlist. "Name S01 E02" entries are gathered into a series.
    public static func vodLibrary(from entries: [M3UEntry]) -> VODLibrary {
        var library = VODLibrary()
        var movieGroups = Set<String>()
        var seriesGroups = Set<String>()
        var seriesIndex: [String: Int] = [:]

        for entry in entries where entry.isVOD {
            let path = entry.url.split(separator: "?", maxSplits: 1).first.map(String.init) ?? entry.url
            let ext = path.split(separator: "/").last?.split(separator: ".").dropFirst().last.map(String.init)
            let group = entry.group.isEmpty ? nil : entry.group

            let range = NSRange(entry.name.startIndex..., in: entry.name)
            if let match = episodePattern.firstMatch(in: entry.name, range: range),
               let nameRange = Range(match.range(at: 1), in: entry.name),
               let seasonRange = Range(match.range(at: 2), in: entry.name),
               let numberRange = Range(match.range(at: 3), in: entry.name),
               !entry.name[nameRange].isEmpty {
                let seriesName = String(entry.name[nameRange])
                let id = stableID(seriesName)
                if seriesIndex[id] == nil {
                    seriesIndex[id] = library.series.count
                    library.series.append(Series(id: id, name: seriesName, categoryID: group, poster: entry.logo))
                    if let group, seriesGroups.insert(group).inserted {
                        library.seriesCategories.append(ChannelCategory(id: group, name: group))
                    }
                }
                library.flatEpisodes[id, default: []].append(Episode(
                    id: stableID(entry.url),
                    season: Int(entry.name[seasonRange]) ?? 1,
                    number: Int(entry.name[numberRange]) ?? 0,
                    title: entry.name,
                    container: ext ?? "mp4",
                    still: entry.logo,
                    url: entry.url))
            } else {
                library.movies.append(Movie(id: stableID(entry.url), name: entry.name, categoryID: group,
                                            poster: entry.logo, container: ext, url: entry.url))
                if let group, movieGroups.insert(group).inserted {
                    library.movieCategories.append(ChannelCategory(id: group, name: group))
                }
            }
        }
        return library
    }
}
