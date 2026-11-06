import Foundation

public enum ChannelGrouper {
    /// Merges a provider's entries into one channel per (language, canonical name), in provider
    /// order. Same rule as the engine's catalog: nameless entries and channels that only exist as
    /// `[BK]` backups are not listed.
    public static func group(_ streams: [LiveStream]) -> [Channel] {
        struct Bucket {
            var lang: String?
            var variants: [ChannelVariant] = []
        }
        var buckets: [String: Bucket] = [:]
        var order: [String] = []
        buckets.reserveCapacity(streams.count)

        for stream in streams {
            let parsed = ChannelName.parse(stream.name)
            guard !parsed.canonical.isEmpty else { continue }
            let key = (parsed.lang ?? "") + "|" + parsed.canonical
            if buckets[key] == nil {
                buckets[key] = Bucket(lang: parsed.lang)
                order.append(key)
            }
            buckets[key]?.variants.append(ChannelVariant(
                stream: stream,
                quality: parsed.quality,
                height: ChannelName.height(ofQuality: parsed.quality),
                isBackup: ChannelName.isBackup(stream.name)))
        }

        return order.compactMap { key in
            guard let bucket = buckets[key] else { return nil }
            let primaries = bucket.variants.filter { !$0.isBackup }
            guard let first = primaries.first else { return nil }
            let ordered = sorted(bucket.variants)
            let label = ChannelName.displayName(first.stream.name)
            let name = label.isEmpty ? first.stream.name : label
            return Channel(
                id: key,
                name: name,
                lang: bucket.lang,
                logo: primaries.lazy.compactMap(\.stream.logo).first { !$0.isEmpty },
                number: primaries.lazy.compactMap(\.stream.number).min(),
                categoryIDs: Set(primaries.compactMap(\.stream.categoryID)),
                variants: ordered,
                searchKey: searchKey(name))
        }
    }

    /// Best quality first, unlabelled next, backups last.
    public static func sorted(_ variants: [ChannelVariant], maxHeight: Int? = nil) -> [ChannelVariant] {
        func allowed(_ variant: ChannelVariant) -> Bool {
            guard let maxHeight else { return true }
            return variant.height <= maxHeight
        }
        return variants.enumerated().sorted { lhs, rhs in
            let (a, b) = (lhs.element, rhs.element)
            if a.isBackup != b.isBackup { return !a.isBackup }
            if allowed(a) != allowed(b) { return allowed(a) }
            if a.height != b.height {
                // Above the ceiling, the closest to it comes first.
                return allowed(a) ? a.height > b.height : a.height < b.height
            }
            return lhs.offset < rhs.offset
        }.map(\.element)
    }

    public static func searchKey(_ text: String) -> String {
        text.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: nil)
    }

    /// Every word of the query must appear, regardless of case and accents.
    public static func filter(_ channels: [Channel], query: String) -> [Channel] {
        let words = searchKey(query).split(whereSeparator: \.isWhitespace)
        guard !words.isEmpty else { return channels }
        return channels.filter { channel in
            words.allSatisfy { channel.searchKey.contains($0) }
        }
    }
}
