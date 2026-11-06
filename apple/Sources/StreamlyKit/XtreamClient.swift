import Foundation

public enum ProviderError: LocalizedError, Equatable, Sendable {
    case invalidAddress
    case unreachable
    case timedOut
    case credentialsRefused
    case accountExpired
    case accountDisabled(String)
    case badResponse
    case emptyPlaylist

    // Messages never include a URL: they would carry the subscription's credentials.
    public var errorDescription: String? {
        switch self {
        case .invalidAddress: return "Cette adresse n'est pas valide. Elle doit commencer par http:// ou https://."
        case .unreachable: return "Le serveur de l'abonnement est injoignable. Vérifiez l'adresse et votre connexion."
        case .timedOut: return "Le serveur de l'abonnement met trop de temps à répondre. Réessayez dans un instant."
        case .credentialsRefused: return "L'identifiant ou le mot de passe est refusé par le serveur."
        case .accountExpired: return "Cet abonnement a expiré."
        case .accountDisabled(let status): return "Cet abonnement n'est pas actif (état : \(status))."
        case .badResponse: return "Le serveur a envoyé une réponse illisible. Ce n'est peut-être pas un serveur Xtream."
        case .emptyPlaylist: return "Cette playlist ne contient ni chaîne, ni film, ni série."
        }
    }
}

public struct XtreamClient: Sendable {
    public let host: String
    public let username: String
    public let password: String
    public let userAgent: String
    private let session: URLSession

    public init(host: String, username: String, password: String,
                userAgent: String = Playlist.defaultUserAgent, session: URLSession = .shared) {
        self.host = host.hasSuffix("/") ? String(host.dropLast()) : host
        self.username = username
        self.password = password
        self.userAgent = userAgent
        self.session = session
    }

    // MARK: Endpoints

    public func account() async throws -> AccountInfo {
        guard let root = try await api() as? [String: Any],
              let info = root["user_info"] as? [String: Any] else {
            throw ProviderError.credentialsRefused
        }
        guard Lenient.string(info["auth"]) == "1" else { throw ProviderError.credentialsRefused }

        let account = AccountInfo(
            status: Lenient.string(info["status"]),
            expiresAt: Lenient.double(info["exp_date"]).flatMap { $0 > 0 ? Date(timeIntervalSince1970: $0) : nil },
            maxConnections: Lenient.int(info["max_connections"]),
            activeConnections: Lenient.int(info["active_cons"]),
            serverTimeZone: Lenient.string((root["server_info"] as? [String: Any])?["timezone"]))

        if let status = account.status?.lowercased(), status != "active" {
            if status == "expired" { throw ProviderError.accountExpired }
            throw ProviderError.accountDisabled(account.status ?? status)
        }
        return account
    }

    public func library() async throws -> Library {
        // The stream list can weigh 8 MB and be slow to generate; categories are small.
        let rawStreams = (try await api(action: "get_live_streams") as? [Any]) ?? []
        let rawCategories = (try? await api(action: "get_live_categories") as? [Any]) ?? []

        let streams = rawStreams.compactMap { item -> LiveStream? in
            guard let row = item as? [String: Any],
                  let id = Lenient.string(row["stream_id"]),
                  let name = Lenient.string(row["name"]), !name.isEmpty else { return nil }
            return LiveStream(
                id: id, name: name,
                categoryID: Lenient.string(row["category_id"]),
                logo: Lenient.string(row["stream_icon"]),
                epgID: Lenient.string(row["epg_channel_id"]),
                number: Lenient.int(row["num"]),
                archiveDays: Lenient.int(row["tv_archive"]) == 1 ? Lenient.int(row["tv_archive_duration"]) ?? 1 : nil)
        }

        var categories = rawCategories.compactMap { item -> ChannelCategory? in
            guard let row = item as? [String: Any],
                  let id = Lenient.string(row["category_id"]),
                  let name = Lenient.string(row["category_name"]), !name.isEmpty else { return nil }
            return ChannelCategory(id: id, name: name)
        }
        if categories.isEmpty, !streams.isEmpty {
            categories = await categoriesFromExport(streams)
        }
        return Library(categories: categories, streams: streams)
    }

    public func shortEPG(streamID: String, limit: Int = 12) async throws -> [Programme] {
        let payload = try await api(action: "get_short_epg", extra: [
            URLQueryItem(name: "stream_id", value: streamID),
            URLQueryItem(name: "limit", value: String(limit)),
        ])
        let root = payload as? [String: Any]
        let rows = (root?["epg_listings"] as? [Any]) ?? (payload as? [Any]) ?? []
        return rows.compactMap { item in
            guard let row = item as? [String: Any],
                  let start = Lenient.double(row["start_timestamp"]),
                  let stop = Lenient.double(row["stop_timestamp"] ?? row["end_timestamp"]),
                  let title = Lenient.string(row["title"]).map(Lenient.decodeBase64) else { return nil }
            return Programme(
                title: title,
                summary: Lenient.string(row["description"]).map(Lenient.decodeBase64),
                start: Date(timeIntervalSince1970: start),
                stop: Date(timeIntervalSince1970: stop))
        }
    }

    // MARK: Network

    /// Some panels return an empty category list while streams do carry a `category_id`. The
    /// labels are then rebuilt by majority vote from the `group-title` of the M3U export.
    private func categoriesFromExport(_ streams: [LiveStream]) async -> [ChannelCategory] {
        let ids = Set(streams.compactMap(\.categoryID))
        var groups: [String: String] = [:]
        if let text = try? await m3uExport() {
            for entry in M3UParser.parse(text) {
                if let last = entry.url.split(separator: "/").last {
                    let id = last.split(separator: ".").first.map(String.init) ?? String(last)
                    groups[id] = entry.group
                }
            }
        }
        var votes: [String: [String: Int]] = [:]
        for stream in streams {
            guard let category = stream.categoryID, let group = groups[stream.id], !group.isEmpty else { continue }
            votes[category, default: [:]][group, default: 0] += 1
        }
        return ids.sorted().map { id in
            let winner = votes[id]?.max { $0.value < $1.value }?.key
            return ChannelCategory(id: id, name: winner ?? "Catégorie \(id)")
        }
    }

    private func m3uExport() async throws -> String {
        guard var components = URLComponents(string: host + "/get.php") else { throw ProviderError.invalidAddress }
        components.queryItems = [
            URLQueryItem(name: "username", value: username),
            URLQueryItem(name: "password", value: password),
            URLQueryItem(name: "type", value: "m3u_plus"),
            URLQueryItem(name: "output", value: "mpegts"),
        ]
        guard let url = components.url else { throw ProviderError.invalidAddress }
        return M3UParser.decode(try await HTTP.get(url, userAgent: userAgent, session: session))
    }

    func api(action: String? = nil, extra: [URLQueryItem] = []) async throws -> Any {
        guard var components = URLComponents(string: host + "/player_api.php"),
              let scheme = components.scheme?.lowercased(), scheme == "http" || scheme == "https",
              components.host?.isEmpty == false else { throw ProviderError.invalidAddress }
        var items = [URLQueryItem(name: "username", value: username),
                     URLQueryItem(name: "password", value: password)]
        if let action { items.append(URLQueryItem(name: "action", value: action)) }
        components.queryItems = items + extra
        guard let url = components.url else { throw ProviderError.invalidAddress }

        let data = try await HTTP.get(url, userAgent: userAgent, session: session)
        do {
            return try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw ProviderError.badResponse
        }
    }
}

public enum HTTP {
    public static func get(_ url: URL, userAgent: String, session: URLSession = .shared) async throws -> Data {
        var request = URLRequest(url: url, timeoutInterval: 120)
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        do {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse {
                if http.statusCode == 401 || http.statusCode == 403 { throw ProviderError.credentialsRefused }
                if !(200...299).contains(http.statusCode) { throw ProviderError.badResponse }
            }
            return data
        } catch let error as URLError {
            // URLError's description contains the full URL: map it, never forward it.
            if error.code == .cancelled { throw CancellationError() }
            throw error.code == .timedOut ? ProviderError.timedOut : ProviderError.unreachable
        }
    }
}

/// Panels return numbers as strings, nulls, and empty lists in place of objects.
enum Lenient {
    static func string(_ value: Any?) -> String? {
        switch value {
        case let text as String: return text.isEmpty ? nil : text
        case let number as NSNumber: return number.stringValue
        default: return nil
        }
    }

    static func int(_ value: Any?) -> Int? {
        switch value {
        case let number as NSNumber: return number.intValue
        case let text as String: return Int(text)
        default: return nil
        }
    }

    static func double(_ value: Any?) -> Double? {
        switch value {
        case let number as NSNumber: return number.doubleValue
        case let text as String: return Double(text)
        default: return nil
        }
    }

    /// `get_short_epg` titles are base64; some panels send plain text instead.
    static func decodeBase64(_ text: String) -> String {
        guard let data = Data(base64Encoded: text),
              let decoded = String(data: data, encoding: .utf8), !decoded.isEmpty else { return text }
        return decoded
    }
}
