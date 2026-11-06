import Foundation
import StreamlyKit
import StreamlyPlayer

// Decodes a stream without a window and reports what happened. Exit code 0 means playback really
// progressed. Usage: streamly-smoke <url> [seconds]

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    print("usage: streamly-smoke <url> [seconds]")
    exit(2)
}
let url = arguments[1]
let duration = arguments.count > 2 ? (Double(arguments[2]) ?? 6) : 6
let label = Redact.secrets(in: url)

let player = MPVPlayer(headless: true, enableAudio: false)
var started = false
var firstPosition: Double?

func report(failure: String) -> Never {
    print("FAIL \(label) | \(failure)")
    exit(1)
}

func finish() async -> Never {
    let snapshot = await player.snapshot()
    let info = snapshot.media
    let advanced = (snapshot.position ?? 0) - (firstPosition ?? 0)
    let summary = "\(label) | \(info.width ?? 0)x\(info.height ?? 0) | video=\(info.videoCodec ?? "-")"
        + " | audio=\(info.audioCodec ?? "-") | advanced=\(String(format: "%.1f", advanced))s"
    if advanced > 0.5, (info.width ?? 0) > 0 {
        print("OK   \(summary)")
        exit(0)
    }
    print("FAIL \(summary)")
    exit(1)
}

player.onEvent = { event in
    switch event {
    case .playing:
        guard !started else { return }
        started = true
        Task {
            firstPosition = await player.snapshot().position
            try? await Task.sleep(for: .seconds(duration))
            await finish()
        }
    case .ended(let reason, let error):
        if reason == .streamStopped { report(failure: error ?? "fin du flux") }
    default:
        break
    }
}

player.play(url: url, userAgent: Playlist.defaultUserAgent)
DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
    report(failure: "aucune image après 30 s")
}
dispatchMain()
