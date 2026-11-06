import Foundation

/// Name analysis ported from `server/streamly/xtream.py` (`parse_name`, `quality_height`, `is_backup`).
/// Keep both in sync: the engine and the app must group a provider's channels the same way.
public struct ParsedName: Equatable, Sendable {
    public let lang: String?
    public let canonical: String
    public let quality: String?

    public init(lang: String?, canonical: String, quality: String?) {
        self.lang = lang
        self.canonical = canonical
        self.quality = quality
    }
}

public enum ChannelName {
    private static let qualityTokens =
        #"\b(4K|UHD|FHD|FULLHD|1080P?|720P?|576P|480P|360P|HD|SD|HEVC|H265|LQ|MQ|HQ|RAW)\b"#
    private static let qualityRegex = try! NSRegularExpression(pattern: qualityTokens)
    private static let qualityRegexAnyCase = try! NSRegularExpression(
        pattern: qualityTokens, options: .caseInsensitive)
    private static let decorationRegex = try! NSRegularExpression(pattern: "[#*=~_·•▬═■□◆♦]+")
    private static let prefixRegex = try! NSRegularExpression(pattern: #"^\s*([A-Z]{2,4})\s*[-|:]\s*"#)
    private static let prefixRegexAnyCase = try! NSRegularExpression(
        pattern: #"^\s*([A-Za-z]{2,4})\s*[-|:]\s*"#)
    private static let tagRegex = try! NSRegularExpression(pattern: #"\[[^\]]*\]"#)
    private static let backupRegex = try! NSRegularExpression(pattern: #"\[BK\d*\]"#)

    private static let heights: [String: Int] = [
        "SD": 480, "LQ": 360, "MQ": 480, "HQ": 720,
        "360P": 360, "480P": 480, "576P": 576, "720P": 720, "1080P": 1080,
        "HEVC": 720, "H265": 720,  // usually a 720p, despite the label
        "HD": 720, "FHD": 1080, "FULLHD": 1080, "1080": 1080,
        "4K": 2160, "UHD": 2160,
    ]

    public static func parse(_ name: String) -> ParsedName {
        // NFKC turns superscript labels ("FRANCE 2 ᴴᴰ") into plain tokens; without it the
        // channel forms its own group and can no longer back up its twin.
        var upper = name.precomposedStringWithCompatibilityMapping.uppercased()
        upper = replace(decorationRegex, in: upper, with: " ")

        var lang: String?
        let full = NSRange(upper.startIndex..., in: upper)
        if let match = prefixRegex.firstMatch(in: upper, range: full),
           let range = Range(match.range(at: 1), in: upper) {
            lang = String(upper[range])
        }

        let body = replace(prefixRegex, in: upper, with: "")
        let bodyRange = NSRange(body.startIndex..., in: body)
        let quality = qualityRegex.matches(in: body, range: bodyRange).last
            .flatMap { Range($0.range(at: 1), in: body) }
            .map { String(body[$0]) }

        var canonical = replace(qualityRegex, in: body, with: "")
        canonical = replace(tagRegex, in: canonical, with: "")
        let words = String(canonical.map { $0.isLetter || $0.isNumber ? $0 : " " })
            .split(separator: " ")
        return ParsedName(lang: lang, canonical: words.joined(separator: " "), quality: quality)
    }

    public static func height(ofQuality token: String?) -> Int {
        guard let token else { return 0 }
        return heights[token] ?? 0
    }

    public static func isBackup(_ name: String) -> Bool {
        let upper = name.uppercased()
        return backupRegex.firstMatch(in: upper, range: NSRange(upper.startIndex..., in: upper)) != nil
    }

    /// Readable label: the provider's own casing, minus language prefix, quality and tags.
    public static func displayName(_ name: String) -> String {
        var label = name.precomposedStringWithCompatibilityMapping
        label = replace(decorationRegex, in: label, with: " ")
        label = replace(prefixRegexAnyCase, in: label, with: "")
        label = replace(tagRegex, in: label, with: "")
        label = replace(qualityRegexAnyCase, in: label, with: "")
        let words = label.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return words.trimmingCharacters(in: CharacterSet(charactersIn: " -|:"))
    }

    private static func replace(_ regex: NSRegularExpression, in text: String, with template: String) -> String {
        regex.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text), withTemplate: template)
    }
}
