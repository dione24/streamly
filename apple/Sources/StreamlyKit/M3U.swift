import CryptoKit
import Foundation

public struct M3UEntry: Equatable, Sendable {
    public let name: String
    public let url: String
    public let group: String
    public let logo: String?
    public let epgID: String?

    /// A file or a /movie/ path is not a live channel: listing it as one gives a stream that stops.
    public var isVOD: Bool {
        let lower = url.lowercased()
        if lower.contains("/movie/") || lower.contains("/series/") { return true }
        let path = lower.split(separator: "?", maxSplits: 1).first.map(String.init) ?? lower
        return [".mp4", ".mkv", ".avi", ".m4v"].contains { path.hasSuffix($0) }
    }
}

public enum M3UParser {
    public static func decode(_ data: Data) -> String {
        var bytes = data
        if bytes.starts(with: [0xEF, 0xBB, 0xBF]) { bytes.removeFirst(3) }
        return String(data: bytes, encoding: .utf8)
            ?? String(data: bytes, encoding: .isoLatin1)
            ?? ""
    }

    public static func parse(_ text: String) -> [M3UEntry] {
        var entries: [M3UEntry] = []
        var pending: (name: String, group: String, logo: String?, epg: String?)?
        var groupOverride: String?

        for rawLine in text.split(whereSeparator: \.isNewline) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { continue }
            if line.hasPrefix("#EXTINF:") {
                pending = parseInfo(line)
                groupOverride = nil
            } else if line.hasPrefix("#EXTGRP:") {
                groupOverride = String(line.dropFirst("#EXTGRP:".count))
                    .trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("#") {
                continue
            } else if let info = pending {
                let group = info.group.isEmpty ? (groupOverride ?? "") : info.group
                entries.append(M3UEntry(name: info.name, url: line, group: group,
                                        logo: info.logo, epgID: info.epg))
                pending = nil
            }
        }
        return entries
    }

    /// `#EXTINF:-1 tvg-id="a" group-title="News, FR",Name`: the name starts after the first comma
    /// outside quotes, and values may be unquoted.
    private static func parseInfo(_ line: String) -> (name: String, group: String, logo: String?, epg: String?) {
        var attributes: [String: String] = [:]
        var key = ""
        var value = ""
        var inKey = true
        var quote = false
        var isValue = false
        var name = ""

        func flush() {
            if isValue, !key.isEmpty { attributes[key.lowercased()] = value }
            key = ""
            value = ""
            inKey = true
            isValue = false
        }

        let body = line.dropFirst("#EXTINF:".count)
        var cursor = body.startIndex
        while cursor < body.endIndex {
            let character = body[cursor]
            if quote {
                if character == "\"" { quote = false } else { value.append(character) }
            } else if character == "\"" {
                quote = true
            } else if character == "," {
                flush()
                name = String(body[body.index(after: cursor)...])
                break
            } else if character == " " || character == "\t" {
                flush()
            } else if character == "=", inKey {
                inKey = false
                isValue = true
            } else if inKey {
                key.append(character)
            } else {
                value.append(character)
            }
            cursor = body.index(after: cursor)
        }
        flush()

        name = name.trimmingCharacters(in: .whitespaces)
        if name.isEmpty { name = attributes["tvg-name"] ?? "" }
        return (name, attributes["group-title"] ?? "", nonEmpty(attributes["tvg-logo"]), nonEmpty(attributes["tvg-id"]))
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    /// Builds a library from a flat playlist. Categories come from `group-title`.
    public static func library(from entries: [M3UEntry]) -> Library {
        var categories: [ChannelCategory] = []
        var known = Set<String>()
        var streams: [LiveStream] = []
        streams.reserveCapacity(entries.count)

        for entry in entries where !entry.isVOD {
            var categoryID: String?
            if !entry.group.isEmpty {
                categoryID = entry.group
                if known.insert(entry.group).inserted {
                    categories.append(ChannelCategory(id: entry.group, name: entry.group))
                }
            }
            streams.append(LiveStream(
                id: stableID(entry.url), name: entry.name, categoryID: categoryID,
                logo: entry.logo, epgID: entry.epgID, url: entry.url))
        }
        return Library(categories: categories, streams: streams)
    }

    private static func stableID(_ url: String) -> String {
        SHA256.hash(data: Data(url.utf8)).prefix(10).map { String(format: "%02x", $0) }.joined()
    }
}

public struct XtreamCredentials: Equatable, Sendable {
    public let host: String
    public let username: String
    public let password: String

    public init(host: String, username: String, password: String) {
        self.host = host
        self.username = username
        self.password = password
    }

    /// `…/get.php?username=U&password=P…` is a Xtream account in disguise: treating it as a flat
    /// playlist would lose the guide, movies and series.
    public static func fromM3ULink(_ link: String) -> XtreamCredentials? {
        guard let components = URLComponents(string: link.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = components.scheme, let host = components.host,
              components.path.lowercased().hasSuffix("/get.php") else { return nil }
        let items = components.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first { $0.name.lowercased() == name }?.value
        }
        guard let username = value("username"), let password = value("password"),
              !username.isEmpty, !password.isEmpty else { return nil }
        let port = components.port.map { ":\($0)" } ?? ""
        return XtreamCredentials(host: "\(scheme)://\(host)\(port)", username: username, password: password)
    }
}
