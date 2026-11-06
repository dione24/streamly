#if DEBUG
import AppKit
import Foundation
import StreamlyKit
import StreamlyPlayer

/// Debug builds only. With `STREAMLY_SELFTEST_DIR` set, the app uses that folder and in-memory
/// secrets instead of the user's data, plays a scripted scenario against `tools/fake_panel.py` in
/// the real window, writes `report.txt` with images, and quits.
final class MemorySecrets: SecretStore, @unchecked Sendable {
    private var values: [String: String] = [:]
    func set(_ secret: String, for key: String) throws { values[key] = secret }
    func get(_ key: String) -> String? { values[key] }
    func delete(_ key: String) { values[key] = nil }
}

@MainActor
enum SelfTest {
    static var directory: URL? {
        ProcessInfo.processInfo.environment["STREAMLY_SELFTEST_DIR"].map { URL(fileURLWithPath: $0) }
    }

    static func makeModel() -> AppModel {
        guard let directory, let store = try? LocalStore(directory: directory) else { return AppModel() }
        return AppModel(store: store, secrets: MemorySecrets())
    }

    static func runIfRequested(_ model: AppModel) {
        guard let directory else { return }
        Task { @MainActor in
            await Runner(model: model, directory: directory).run()
            NSApp.terminate(nil)
        }
    }

    @MainActor
    private final class Runner {
        let model: AppModel
        let directory: URL
        var lines: [String] = []
        var failures = 0
        var playback: PlaybackController { model.playback }

        init(model: AppModel, directory: URL) {
            self.model = model
            self.directory = directory
        }

        func log(_ text: String) {
            lines.append(text)
            try? lines.joined(separator: "\n").write(
                to: directory.appendingPathComponent("report.txt"), atomically: true, encoding: .utf8)
        }

        func check(_ condition: Bool, _ label: String) {
            if !condition { failures += 1 }
            log((condition ? "OK    " : "ECHEC ") + label)
        }

        /// Waits for a state, logging each transition with its time.
        func wait(for label: String, timeout: TimeInterval, until done: (PlaybackState) -> Bool) async -> Bool {
            let start = Date()
            var last: PlaybackState?
            while Date().timeIntervalSince(start) < timeout {
                if playback.state != last {
                    last = playback.state
                    log(String(format: "      %5.1fs %@", Date().timeIntervalSince(start), String(describing: playback.state)))
                }
                if done(playback.state) { return true }
                try? await Task.sleep(for: .milliseconds(100))
            }
            log("      délai dépassé pour \(label)")
            for name in ["idle-active", "core-idle", "paused-for-cache", "demuxer-cache-duration", "file-format", "vo-configured", "time-pos"] {
                log("        \(name) = \(await playback.player.property(name) ?? "-")")
            }
            return false
        }

        func channel(_ name: String) -> Channel? {
            model.channels.first { $0.name == name }
        }

        func play(_ name: String, expectAudio: String? = nil, expectVideo: String? = nil, expectHeight: Int? = nil) async {
            log("--- \(name)")
            guard let channel = channel(name) else { return check(false, "chaîne trouvée") }
            model.play(channel)
            let playing = await wait(for: name, timeout: 25) { $0 == .playing }
            check(playing, "\(name) : lecture")
            guard playing else { return }
            try? await Task.sleep(for: .seconds(2))
            let before = await playback.player.snapshot().position ?? 0
            try? await Task.sleep(for: .seconds(2))
            let snapshot = await playback.player.snapshot()
            let info = snapshot.media
            let advanced = (snapshot.position ?? 0) - before
            log("      \(info.width ?? 0)x\(info.height ?? 0) · \(info.videoCodec ?? "-") · \(info.audioCodec ?? "-") · matériel=\(info.hardwareDecoder ?? "non") · variante=\(playback.variantLabel ?? "-")")
            check(advanced > 1, String(format: "\(name) : le temps avance (%.1f s en 2 s)", advanced))
            if let expectAudio { check(info.audioCodec == expectAudio, "\(name) : audio \(expectAudio)") }
            if let expectVideo { check(info.videoCodec?.contains(expectVideo) == true, "\(name) : vidéo \(expectVideo)") }
            if let expectHeight { check(info.height == expectHeight, "\(name) : hauteur \(expectHeight)") }
        }

        /// The window's interface with the video frame drawn in place: an app cannot read back a
        /// Metal layer through AppKit, so the frame comes from mpv's own screenshot.
        func capture(_ name: String) async {
            var frameImage: NSImage?
            if playback.state == .playing {
                // Prefer what the renderer really shows: that is the proof the picture reaches the window.
                var raw = await playback.player.rawFrame(mode: "window")
                var source = "rendu de la fenêtre"
                if raw == nil {
                    raw = await playback.player.rawFrame(mode: "video")
                    source = "image décodée"
                }
                if let raw, let image = cgImage(raw) {
                    frameImage = NSImage(cgImage: image, size: NSSize(width: raw.width, height: raw.height))
                    let brightness = averageBrightness(raw)
                    log(String(format: "      capture (\(source)) %dx%d, luminosité moyenne %.0f/255", raw.width, raw.height, brightness))
                    check(brightness > 10, "\(name) : l'image n'est pas noire")
                } else {
                    check(false, "\(name) : capture de l'image")
                }
            }
            guard let window = NSApp.windows.first(where: { $0.isVisible }), let content = window.contentView,
                  let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else {
                return log("      pas de fenêtre à capturer")
            }
            content.cacheDisplay(in: content.bounds, to: bitmap)
            let composed = NSImage(size: content.bounds.size)
            composed.addRepresentation(bitmap)
            if let frameImage {
                writePNG(frameImage, name: "\(name)-rendu")
            }
            if let host = findVideoHost(in: content), let frameImage {
                // AppKit's snapshot paints the Metal area black, controls included: the frame goes on top.
                composed.lockFocus()
                frameImage.draw(in: host.convert(host.bounds, to: content))
                composed.unlockFocus()
            }
            writePNG(composed, name: name)
        }

        func writePNG(_ image: NSImage, name: String) {
            if let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
               let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: directory.appendingPathComponent("\(name).png"))
            }
        }

        func cgImage(_ raw: RawFrame) -> CGImage? {
            guard let provider = CGDataProvider(data: raw.data as CFData) else { return nil }
            let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
            return CGImage(width: raw.width, height: raw.height, bitsPerComponent: 8, bitsPerPixel: 32,
                           bytesPerRow: raw.stride, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: info,
                           provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
        }

        func averageBrightness(_ raw: RawFrame) -> Double {
            var total = 0, samples = 0
            raw.data.withUnsafeBytes { (bytes: UnsafeRawBufferPointer) in
                for y in stride(from: 0, to: raw.height, by: 8) {
                    for x in stride(from: 0, to: raw.width, by: 8) {
                        let offset = y * raw.stride + x * 4
                        guard offset + 2 < bytes.count else { continue }
                        total += Int(bytes[offset]) + Int(bytes[offset + 1]) + Int(bytes[offset + 2])
                        samples += 3
                    }
                }
            }
            return samples == 0 ? 0 : Double(total) / Double(samples)
        }

        func findVideoHost(in view: NSView) -> VideoHostView? {
            if let host = view as? VideoHostView { return host }
            for child in view.subviews {
                if let found = findVideoHost(in: child) { return found }
            }
            return nil
        }

        /// Real events through the app's own event queue: exercises focus, gestures and key handlers.
        func press(_ character: String, keyCode: UInt16) async {
            guard let window = NSApp.windows.first(where: { $0.isVisible }) else { return }
            for type in [NSEvent.EventType.keyDown, .keyUp] {
                if let event = NSEvent.keyEvent(
                    with: type, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                    windowNumber: window.windowNumber, context: nil, characters: character,
                    charactersIgnoringModifiers: character, isARepeat: false, keyCode: keyCode) {
                    NSApp.sendEvent(event)
                }
            }
            try? await Task.sleep(for: .milliseconds(200))
        }

        func click(fromTop y: CGFloat, x: CGFloat, count: Int = 1) async {
            guard let window = NSApp.windows.first(where: { $0.isVisible }) else { return }
            let point = NSPoint(x: x, y: window.frame.height - y)
            for clickIndex in 1...count {
                for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                    if let event = NSEvent.mouseEvent(
                        with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                        windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: clickIndex, pressure: 1) {
                        NSApp.sendEvent(event)
                    }
                }
            }
            try? await Task.sleep(for: .milliseconds(200))
        }

        var isFullScreen: Bool {
            NSApp.windows.first(where: { $0.isVisible })?.styleMask.contains(.fullScreen) == true
        }

        func waitForChannel(_ name: String) async -> Bool {
            for _ in 0..<40 {
                if playback.channel?.name == name { return true }
                try? await Task.sleep(for: .milliseconds(100))
            }
            return false
        }

        func interaction() async {
            log("--- Clics et clavier")
            NSApp.activate(ignoringOtherApps: true)
            try? await Task.sleep(for: .seconds(1))

            await click(fromTop: 240, x: 256)
            check(await waitForChannel("Demo Movies"), "clic sur une chaîne : \(playback.channel?.name ?? "-")")

            await press(String(UnicodeScalar(NSDownArrowFunctionKey)!), keyCode: 125)
            check(await waitForChannel("Démo Nature"), "flèche ↓ dans la liste : \(playback.channel?.name ?? "-")")

            await press("\r", keyCode: 36)
            try? await Task.sleep(for: .seconds(2))
            check(isFullScreen, "Entrée : plein écran")
            _ = await wait(for: "lecture", timeout: 12) { $0 == .playing }
            try? await Task.sleep(for: .seconds(2))
            await capture("plein-ecran")

            await press(String(UnicodeScalar(NSDownArrowFunctionKey)!), keyCode: 125)
            check(await waitForChannel("Démo Audio MP2"), "flèche ↓ en plein écran : \(playback.channel?.name ?? "-")")

            let wasMuted = playback.isMuted
            await press("m", keyCode: 46)
            check(playback.isMuted != wasMuted, "touche M : son")
            await press("m", keyCode: 46)

            await press("f", keyCode: 3)
            try? await Task.sleep(for: .seconds(2))
            check(!isFullScreen, "touche F : sortie du plein écran")
        }

        func run() async {
            playback.toggleMute()  // a test must not play tones on the user's speakers
            log("--- Abonnement")
            let wrong = await model.add(.xtream(name: "Refus", host: "http://127.0.0.1:8765", username: "demo", password: "faux"))
            check(!wrong && !model.hasPlaylist, "mauvais mot de passe refusé, rien d'enregistré")
            let added = await model.add(.xtream(name: "Démo", host: "http://127.0.0.1:8765", username: "demo", password: "demo"))
            check(added, "compte ajouté")
            log("      \(model.channels.map(\.name).joined(separator: " | "))")
            check(model.channels.count == 6, "9 flux regroupés en 6 chaînes")
            check(channel("Démo Sport")?.variants.count == 3, "Démo Sport a 3 variantes")
            check(model.categories.count == 3, "3 catégories")

            let files = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
            let cached = files.filter { $0.hasSuffix(".json") }.compactMap {
                try? String(contentsOf: directory.appendingPathComponent($0), encoding: .utf8)
            }.joined()
            check(!cached.isEmpty && !cached.contains("/demo/demo/"), "pas d'adresse avec mot de passe dans le cache")

            // FHD answers 404: the plan retries once, then moves on to HD.
            await play("Démo Sport", expectAudio: "aac", expectHeight: 720)
            check(playback.variantLabel == "HD", "Démo Sport : FHD en panne, passage à HD")
            let configured = await playback.player.property("vo-configured")
            log("      moteur vidéo : \(await playback.player.property("current-vo") ?? "-"), configuré : \(configured ?? "-"), images perdues : \(await playback.player.property("frame-drop-count") ?? "-")")
            check(configured == "yes", "le moteur vidéo est branché sur la fenêtre")
            let guide = await model.nowNext(for: channel("Démo Sport")!)
            check(guide?.now != nil, "guide : \(guide?.now?.title ?? "vide")")
            try? await Task.sleep(for: .seconds(1))
            await capture("direct")

            // Switching right away hits the provider's refusal window, as with a real panel.
            await play("Démo Cinéma", expectAudio: "ac3")
            await play("Démo Nature", expectAudio: "aac", expectVideo: "HEVC")
            await capture("hevc")
            await play("Démo Audio MP2", expectAudio: "mp2")

            log("--- Abonnement déjà utilisé ailleurs")
            playback.stop()
            try? await Task.sleep(for: .seconds(2.2))
            let other = URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:8765/demo/demo/202")!)
            other.resume()
            try? await Task.sleep(for: .seconds(1.5))
            if let channel = channel("Demo Movies") { model.play(channel) }
            let refused = await wait(for: "refus", timeout: 15) { if case .failed = $0 { return true } else { return false } }
            check(refused, "message clair quand l'abonnement est occupé")
            await capture("occupe")
            other.cancel()
            try? await Task.sleep(for: .seconds(4))
            playback.retry()
            let retried = await wait(for: "réessai", timeout: 15) { $0 == .playing }
            check(retried, "« Réessayer » relance la lecture une fois la connexion libérée")

            await interaction()

            log("--- Liste et préférences")
            model.setOrder(.descending)
            check(model.visibleChannels.first?.name == "قناة تجريبية" || model.visibleChannels.first?.name == "Démo Sport",
                  "tri Z→A : \(model.visibleChannels.first?.name ?? "-")")
            model.setOrder(.provider)
            check(model.visibleChannels.first?.name == "Démo Sport", "ordre du fournisseur")
            if let sport = model.categories.first {
                model.rename(sport, to: "Mes matchs")
                check(model.name(of: sport) == "Mes matchs", "catégorie renommée")
            }
            model.query = "CINEMA"
            check(model.visibleChannels.map(\.name) == ["Démo Cinéma"], "recherche sans accents")
            model.query = ""
            model.sidebar = .recents
            check(model.visibleChannels.first?.name == playback.channel?.name, "récents : \(model.visibleChannels.prefix(3).map(\.name))")
            model.sidebar = .all
            if let sport = channel("Démo Sport") {
                model.toggleFavorite(sport)
                model.sidebar = .favorites
                check(model.visibleChannels.map(\.name) == ["Démo Sport"], "favori")
                model.sidebar = .all
            }

            if let playlist = model.active {
                await model.resync(playlist)
                check(model.sync == .idle && model.channels.count == 6, "actualisation")
                check(model.categories.first.map(model.name(of:)) == "Mes matchs", "le nom choisi survit à l'actualisation")
                model.remove(playlist)
                check(!model.hasPlaylist && model.playback.state == .idle, "suppression : la lecture s'arrête")
            }
            log(failures == 0 ? "RESULTAT : tout est bon" : "RESULTAT : \(failures) échec(s)")
        }
    }
}
#endif
