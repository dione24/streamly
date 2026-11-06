import XCTest
@testable import StreamlyKit

final class ChannelNameTests: XCTestCase {
    func testParsesPrefixNameAndQuality() {
        XCTAssertEqual(ChannelName.parse("FR| CANAL+ SPORT FHD"),
                       ParsedName(lang: "FR", canonical: "CANAL SPORT", quality: "FHD"))
    }

    func testSuperscriptQualityIsRecognised() {
        let parsed = ChannelName.parse("FRANCE 2 ᴴᴰ")
        XCTAssertEqual(parsed.canonical, "FRANCE 2")
        XCTAssertEqual(parsed.quality, "HD")
    }

    func testDecorationsAreRemoved() {
        XCTAssertEqual(ChannelName.parse("####### CANAL+ LIVE #######").canonical, "CANAL LIVE")
    }

    func testArabicNamesNeverCollapse() {
        let a = ChannelName.parse("24/7 AR| آخر كلام").canonical
        let b = ChannelName.parse("24/7 AR| افطارنا غير").canonical
        XCTAssertNotEqual(a, b)
        XCTAssertTrue(a.contains("آخر"))
    }

    func testBackupTag() {
        XCTAssertTrue(ChannelName.isBackup("FR| TF1 [BK]"))
        XCTAssertTrue(ChannelName.isBackup("FR| TF1 [bk2]"))
        XCTAssertFalse(ChannelName.isBackup("FR| TF1 HD"))
    }

    func testHeights() {
        XCTAssertEqual(ChannelName.height(ofQuality: "HEVC"), 720)
        XCTAssertEqual(ChannelName.height(ofQuality: "FHD"), 1080)
        XCTAssertEqual(ChannelName.height(ofQuality: "4K"), 2160)
        XCTAssertEqual(ChannelName.height(ofQuality: nil), 0)
    }

    func testDisplayNameKeepsProviderCasing() {
        XCTAssertEqual(ChannelName.displayName("FR| Canal+ Sport FHD [BK]"), "Canal+ Sport")
    }
}

final class GroupingTests: XCTestCase {
    private let streams = [
        LiveStream(id: "1", name: "FR| TF1 HD", categoryID: "a", number: 3),
        LiveStream(id: "2", name: "FR| TF1 FHD", categoryID: "a", number: 1),
        LiveStream(id: "3", name: "FR| TF1 [BK]", categoryID: "b"),
        LiveStream(id: "4", name: "BE| TF1 HD", categoryID: "c"),
        LiveStream(id: "5", name: "FR| ONLY BACKUP [BK]", categoryID: "a"),
        LiveStream(id: "6", name: "HD", categoryID: "a"),
        LiveStream(id: "7", name: "FR| Élysée Télé", categoryID: "a"),
    ]

    func testVariantsMergeIntoOneChannelPerLanguage() {
        let channels = ChannelGrouper.group(streams)
        XCTAssertEqual(channels.map(\.id), ["FR|TF1", "BE|TF1", "FR|ÉLYSÉE TÉLÉ"])
        let tf1 = channels[0]
        XCTAssertEqual(tf1.name, "TF1")
        XCTAssertEqual(tf1.number, 1)
        XCTAssertEqual(tf1.variants.map(\.stream.id), ["2", "1", "3"])
        XCTAssertEqual(tf1.categoryIDs, ["a"])  // the backup does not list the channel elsewhere
    }

    func testCeilingPrefersLowerQualityButKeepsTheRestAsFallback() {
        let tf1 = ChannelGrouper.group(streams)[0]
        XCTAssertEqual(ChannelGrouper.sorted(tf1.variants, maxHeight: 720).map(\.stream.id), ["1", "2", "3"])
    }

    func testSearchIgnoresCaseAndAccents() {
        let channels = ChannelGrouper.group(streams)
        XCTAssertEqual(ChannelGrouper.filter(channels, query: "elysee").map(\.name), ["Élysée Télé"])
        XCTAssertEqual(ChannelGrouper.filter(channels, query: "TÉLÉ ELY").count, 1)
        XCTAssertEqual(ChannelGrouper.filter(channels, query: "  ").count, channels.count)
    }

    func testGroupsALargeBouquetQuickly() {
        let many = (0..<25_000).map {
            LiveStream(id: "\($0)", name: "FR| Chaine X\($0 % 9_000) \(["SD", "HD", "FHD"][$0 % 3])", categoryID: "a")
        }
        let start = Date()
        let channels = ChannelGrouper.group(many)
        XCTAssertEqual(channels.count, 9_000)
        XCTAssertLessThan(Date().timeIntervalSince(start), 5)
    }

    /// Same behaviour as the engine's regex: a bare 720 or 1080 counts as a quality label.
    func testBareResolutionNumbersAreTreatedAsQuality() {
        XCTAssertEqual(ChannelName.parse("FR| Chaine 720 HD").canonical, "CHAINE")
        XCTAssertEqual(ChannelName.parse("FR| Chaine 1080").quality, "1080")
    }
}

final class PlaybackPlanTests: XCTestCase {
    private let a = PlaybackAttempt(url: "a", label: "FHD")
    private let b = PlaybackAttempt(url: "b", label: "HD")

    func testRetriesThenMovesToNextVariantThenGivesUp() {
        var plan = PlaybackPlan(attempts: [a, b])
        XCTAssertEqual(plan.failed(), .attempt(a, after: PlaybackPlan.reconnectDelay))
        XCTAssertEqual(plan.failed(), .attempt(b, after: PlaybackPlan.reconnectDelay))
        XCTAssertEqual(plan.failed(), .attempt(b, after: PlaybackPlan.reconnectDelay))
        XCTAssertEqual(plan.failed(), .exhausted)
        XCTAssertEqual(plan.failed(), .exhausted)
    }

    func testADropAfterRealPlaybackStaysOnTheSameVariant() {
        var plan = PlaybackPlan(attempts: [a, b])
        _ = plan.failed()
        for _ in 0..<5 {
            XCTAssertEqual(plan.failed(afterPlaying: 60), .attempt(a, after: PlaybackPlan.reconnectDelay))
        }
    }

    func testEmptyPlanIsExhausted() {
        var plan = PlaybackPlan(attempts: [])
        XCTAssertEqual(plan.failed(), .exhausted)
    }

    func testAttemptsFollowVariantOrder() {
        let channel = ChannelGrouper.group([
            LiveStream(id: "1", name: "FR| TF1 HD"),
            LiveStream(id: "2", name: "FR| TF1 FHD"),
            LiveStream(id: "3", name: "FR| TF1 [BK]"),
        ])[0]
        let playlist = Playlist(name: "x", kind: .xtream(host: "http://panel.test", username: "u"))
        let attempts = StreamURL.attempts(for: channel, playlist: playlist, secret: "p/w")
        XCTAssertEqual(attempts.map(\.url), [
            "http://panel.test/u/p%2Fw/2", "http://panel.test/u/p%2Fw/1", "http://panel.test/u/p%2Fw/3",
        ])
        XCTAssertEqual(attempts.map(\.label), ["FHD", "HD", "secours"])
    }
}

final class M3UTests: XCTestCase {
    func testParsesAttributesAndCommasInsideQuotes() {
        let text = """
        #EXTM3U
        #EXTINF:-1 tvg-id="tf1.fr" tvg-logo="http://l/tf1.png" group-title="Info, FR",FR| TF1 HD
        http://p/1
        """
        XCTAssertEqual(M3UParser.parse(text), [
            M3UEntry(name: "FR| TF1 HD", url: "http://p/1", group: "Info, FR",
                     logo: "http://l/tf1.png", epgID: "tf1.fr"),
        ])
    }

    func testToleratesUnquotedAttributesCRLFVLCOptionsAndEXTGRP() {
        let text = "#EXTM3U\r\n#EXTINF:-1 tvg-id=abc group-title=News,Name\r\n#EXTVLCOPT:http-user-agent=x\r\nhttp://p/1\r\n"
            + "#EXTINF:-1,Other\r\n#EXTGRP:Sport\r\nhttp://p/2\r\n"
        let entries = M3UParser.parse(text)
        XCTAssertEqual(entries.map(\.name), ["Name", "Other"])
        XCTAssertEqual(entries.map(\.group), ["News", "Sport"])
        XCTAssertEqual(entries[0].epgID, "abc")
    }

    func testDecodesBOMAndLatin1() {
        XCTAssertEqual(M3UParser.decode(Data([0xEF, 0xBB, 0xBF]) + Data("é".utf8)), "é")
        XCTAssertEqual(M3UParser.decode(Data([0xE9])), "é")
    }

    func testMoviesAreNotListedAsChannels() {
        let text = """
        #EXTINF:-1 group-title="Films",Film
        http://p/movie/u/p/1.mkv
        #EXTINF:-1 group-title="Films",Autre
        http://p/film.mp4?x=1
        #EXTINF:-1 group-title="TV",Chaine
        http://p/live/u/p/9.m3u8
        """
        let library = M3UParser.library(from: M3UParser.parse(text))
        XCTAssertEqual(library.streams.map(\.name), ["Chaine"])
        XCTAssertEqual(library.categories.map(\.name), ["TV"])
    }

    func testLargePlaylistParsesInTime() {
        var text = "#EXTM3U\n"
        for index in 0..<25_000 {
            text += "#EXTINF:-1 tvg-id=\"c\(index)\" group-title=\"G\(index % 50)\",FR| Chaine \(index) HD\nhttp://p/\(index)\n"
        }
        let start = Date()
        XCTAssertEqual(M3UParser.parse(text).count, 25_000)
        XCTAssertLessThan(Date().timeIntervalSince(start), 10)
    }

    func testGetPhpLinkIsRecognisedAsXtream() {
        XCTAssertEqual(
            XtreamCredentials.fromM3ULink(" http://panel.test:8080/get.php?username=u&password=p&type=m3u_plus "),
            XtreamCredentials(host: "http://panel.test:8080", username: "u", password: "p"))
        XCTAssertNil(XtreamCredentials.fromM3ULink("http://panel.test/list.m3u"))
        XCTAssertNil(XtreamCredentials.fromM3ULink("http://panel.test/get.php?username=u"))
    }
}

final class RedactTests: XCTestCase {
    func testHidesCredentialsInQueryAndPath() {
        XCTAssertEqual(Redact.secrets(in: "http://p.test/get.php?username=me&password=s3cret&type=m3u"),
                       "http://p.test/get.php?username=***&password=***&type=m3u")
        XCTAssertEqual(Redact.secrets(in: "failed http://p.test:80/me/s3cret/123 now"),
                       "failed http://p.test:80/***/***/123 now")
        XCTAssertEqual(Redact.secrets(in: "http://p.test/live/me/s3cret/123.m3u8"),
                       "http://p.test/live/***/***/123.m3u8")
    }
}

final class KeychainTests: XCTestCase {
    func testSecretRoundTripUpdateAndDelete() throws {
        let store = KeychainStore(service: "com.streamly.tests.\(UUID().uuidString)")
        let key = "playlist"
        defer { store.delete(key) }
        XCTAssertNil(store.get(key))
        try store.set("première", for: key)
        XCTAssertEqual(store.get(key), "première")
        try store.set("seconde", for: key)
        XCTAssertEqual(store.get(key), "seconde")
        store.delete(key)
        XCTAssertNil(store.get(key))
    }
}

final class PreferencesTests: XCTestCase {
    func testRecentsAreUniqueAndBounded() {
        var prefs = PlaylistPreferences()
        for index in 0..<40 { prefs.markPlayed("c\(index)") }
        prefs.markPlayed("c35")
        XCTAssertEqual(prefs.recents.first, "c35")
        XCTAssertEqual(prefs.recents.count, PlaylistPreferences.recentsLimit)
        XCTAssertEqual(prefs.recents.filter { $0 == "c35" }.count, 1)
        XCTAssertEqual(prefs.lastChannel, "c35")
    }

    func testListOrderKeepsAccentsAndNumbersNatural() {
        let names = ["Zèbre", "Éclair", "Chaine 10", "Chaine 2"]
        XCTAssertEqual(ListOrder.provider.apply(names) { $0 }, names)
        XCTAssertEqual(ListOrder.ascending.apply(names) { $0 }, ["Chaine 2", "Chaine 10", "Éclair", "Zèbre"])
        XCTAssertEqual(ListOrder.descending.apply(names) { $0 }, ["Zèbre", "Éclair", "Chaine 10", "Chaine 2"])
    }

    func testRenamingACategoryToNothingRestoresTheProviderName() {
        var prefs = PlaylistPreferences()
        prefs.rename(category: "7", to: "  Mon sport ")
        XCTAssertEqual(prefs.categoryNames["7"], "Mon sport")
        prefs.rename(category: "7", to: "   ")
        XCTAssertNil(prefs.categoryNames["7"])
    }

    func testStoreRoundTripIsPrivate() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = try LocalStore(directory: directory)
        var prefs = PlaylistPreferences()
        prefs.toggleFavorite("a")
        try store.save(prefs, to: "prefs.json")
        XCTAssertEqual(store.load(PlaylistPreferences.self, from: "prefs.json"), prefs)
        let attributes = try FileManager.default.attributesOfItem(
            atPath: directory.appendingPathComponent("prefs.json").path)
        XCTAssertEqual(attributes[.posixPermissions] as? Int, 0o600)
    }
}

/// Fake provider: tests never touch the network.
final class FakeProvider: URLProtocol {
    nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, Data))?
    nonisolated(unsafe) static var requests: [URLRequest] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        Self.requests.append(request)
        let (status, data) = Self.handler?(request) ?? (500, Data())
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    static func session(_ routes: @escaping (String) -> (Int, String)) -> URLSession {
        requests = []
        handler = { request in
            let url = request.url!.absoluteString
            let (status, body) = routes(url)
            return (status, Data(body.utf8))
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FakeProvider.self]
        return URLSession(configuration: configuration)
    }
}

final class XtreamTests: XCTestCase {
    private let okAccount = #"{"user_info":{"auth":1,"status":"Active","exp_date":"1893456000","max_connections":"1","active_cons":0},"server_info":{}}"#

    private func client(_ session: URLSession) -> XtreamClient {
        XtreamClient(host: "http://panel.test/", username: "u", password: "p", session: session)
    }

    func testAccountReadsNumbersWrittenAsStrings() async throws {
        let account = try await client(FakeProvider.session { _ in (200, self.okAccount) }).account()
        XCTAssertEqual(account.maxConnections, 1)
        XCTAssertEqual(account.expiresAt, Date(timeIntervalSince1970: 1_893_456_000))
        XCTAssertEqual(FakeProvider.requests.first?.value(forHTTPHeaderField: "User-Agent"), Playlist.defaultUserAgent)
    }

    func testRefusedAndExpiredAccountsHaveTheirOwnErrors() async {
        await assertThrows(.credentialsRefused) {
            _ = try await self.client(FakeProvider.session { _ in (200, #"{"user_info":{"auth":0}}"#) }).account()
        }
        await assertThrows(.credentialsRefused) {
            _ = try await self.client(FakeProvider.session { _ in (401, "") }).account()
        }
        await assertThrows(.accountExpired) {
            _ = try await self.client(FakeProvider.session { _ in
                (200, #"{"user_info":{"auth":1,"status":"Expired"}}"#)
            }).account()
        }
        await assertThrows(.badResponse) {
            _ = try await self.client(FakeProvider.session { _ in (200, "<html>") }).account()
        }
    }

    func testLibrarySurvivesMalformedRows() async throws {
        let session = FakeProvider.session { url in
            if url.contains("get_live_categories") {
                return (200, #"[{"category_id":"1","category_name":"Info"},{"category_id":null},[]]"#)
            }
            return (200, #"[{"stream_id":10,"name":"FR| A HD","category_id":"1","num":"7","stream_icon":null},"#
                + #"{"stream_id":null,"name":"bad"},{"stream_id":11},"oops",{"stream_id":"12","name":"FR| B"}]"#)
        }
        let library = try await client(session).library()
        XCTAssertEqual(library.categories, [ChannelCategory(id: "1", name: "Info")])
        XCTAssertEqual(library.streams.map(\.id), ["10", "12"])
        XCTAssertEqual(library.streams[0].number, 7)
    }

    func testEmptyCategoriesAreRebuiltFromTheM3UExport() async throws {
        let session = FakeProvider.session { url in
            if url.contains("get_live_categories") { return (200, "[]") }
            if url.contains("get_live_streams") {
                return (200, #"[{"stream_id":1,"name":"A","category_id":"9"},{"stream_id":2,"name":"B","category_id":"9"},{"stream_id":3,"name":"C","category_id":"4"}]"#)
            }
            return (200, "#EXTM3U\n#EXTINF:-1 group-title=\"Sport\",A\nhttp://p/u/p/1\n#EXTINF:-1 group-title=\"Sport\",B\nhttp://p/u/p/2\n")
        }
        let library = try await client(session).library()
        XCTAssertEqual(library.categories, [
            ChannelCategory(id: "4", name: "Catégorie 4"), ChannelCategory(id: "9", name: "Sport"),
        ])
    }

    func testShortEPGDecodesBase64() async throws {
        let title = Data("Journal de 20h".utf8).base64EncodedString()
        let session = FakeProvider.session { _ in
            (200, #"{"epg_listings":[{"title":"\#(title)","description":"plain text!","start_timestamp":"100","stop_timestamp":"200"},{"title":"x"}]}"#)
        }
        let programmes = try await client(session).shortEPG(streamID: "1")
        XCTAssertEqual(programmes.count, 1)
        XCTAssertEqual(programmes[0].title, "Journal de 20h")
        XCTAssertEqual(programmes[0].summary, "plain text!")

        let nowNext = NowNext(programmes: programmes, at: Date(timeIntervalSince1970: 150))
        XCTAssertEqual(nowNext.now?.title, "Journal de 20h")
        XCTAssertNil(nowNext.next)
        XCTAssertEqual(programmes[0].progress(at: Date(timeIntervalSince1970: 150)), 0.5)
    }

    func testAddDetectsGetPhpAndNeverCachesThePassword() async throws {
        let session = FakeProvider.session { url in
            if url.contains("get_live_streams") { return (200, #"[{"stream_id":1,"name":"FR| A HD"}]"#) }
            if url.contains("get_live_categories") { return (200, #"[{"category_id":"1","category_name":"Info"}]"#) }
            return (200, self.okAccount)
        }
        let result = try await PlaylistService(session: session)
            .add(.m3u(name: "", link: "http://panel.test/get.php?username=u&password=s3cret"))
        XCTAssertEqual(result.playlist.kind, .xtream(host: "http://panel.test", username: "u"))
        XCTAssertEqual(result.secret, "s3cret")
        let encoded = String(decoding: try JSONEncoder().encode(result.library), as: UTF8.self)
            + String(decoding: try JSONEncoder().encode(result.playlist), as: UTF8.self)
        XCTAssertFalse(encoded.contains("s3cret"))
    }

    func testFlatPlaylistAndEmptyPlaylist() async throws {
        let flat = FakeProvider.session { _ in (200, "#EXTM3U\n#EXTINF:-1,Chaine\nhttp://p/1\n") }
        let result = try await PlaylistService(session: flat).add(.m3u(name: "Ma liste", link: "http://list.test/a.m3u"))
        XCTAssertEqual(result.playlist.kind, .m3u)
        XCTAssertEqual(result.library.streams.first?.url, "http://p/1")

        let empty = FakeProvider.session { _ in (200, "#EXTM3U\n") }
        await assertThrows(.emptyPlaylist) {
            _ = try await PlaylistService(session: empty).add(.m3u(name: "", link: "http://list.test/a.m3u"))
        }
        await assertThrows(.invalidAddress) {
            _ = try await PlaylistService(session: empty).add(.m3u(name: "", link: "file:///etc/passwd"))
        }
    }

    func testErrorsNeverCarryTheURL() {
        let cases: [ProviderError] = [.invalidAddress, .unreachable, .timedOut, .credentialsRefused,
                                      .accountExpired, .badResponse, .emptyPlaylist]
        for error in cases { XCTAssertFalse(error.localizedDescription.contains("http://panel")) }
    }

    func testHostNormalisation() {
        XCTAssertEqual(PlaylistService.normalizeHost(" panel.test:8080// "), "http://panel.test:8080")
        XCTAssertEqual(PlaylistService.normalizeHost("https://panel.test/"), "https://panel.test")
    }

    private func assertThrows(_ expected: ProviderError, _ body: () async throws -> Void,
                              file: StaticString = #filePath, line: UInt = #line) async {
        do {
            try await body()
            XCTFail("expected \(expected)", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? ProviderError, expected, file: file, line: line)
        }
    }
}
