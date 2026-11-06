import Foundation
import Observation
import StreamlyKit
import StreamlyPlayer

enum PlaybackState: Equatable {
    case idle
    case connecting(String)
    case reconnecting(String)
    case playing
    case buffering
    case failed(String)
}

/// Runs the player for one channel at a time and follows the `PlaybackPlan` when a stream fails.
@MainActor
@Observable
final class PlaybackController {
    private(set) var state: PlaybackState = .idle
    private(set) var channel: Channel?
    /// Channel name shown while zapping, before the debounced switch happens.
    private(set) var announced: Channel?
    private(set) var variantLabel: String?
    private(set) var media = MediaInfo()
    private(set) var tracks: [MediaTrack] = []
    private(set) var isPaused = false
    private(set) var isMuted = false
    private(set) var volume = 100.0

    @ObservationIgnored let player = MPVPlayer()
    @ObservationIgnored private var plan: PlaybackPlan?
    @ObservationIgnored private var userAgent = Playlist.defaultUserAgent
    @ObservationIgnored private var playingSince: Date?
    @ObservationIgnored private var pendingAttempt: Task<Void, Never>?
    @ObservationIgnored private var pendingZap: Task<Void, Never>?

    /// Ended events are delivered late: one from the previous stream must not count against the
    /// new one, so we wait for the `loadfile` we asked for to start before trusting them.
    @ObservationIgnored private var awaitingStart = false

    init() {
        player.onEvent = { [weak self] event in
            MainActor.assumeIsolated { self?.handle(event) }
        }
    }

    func play(channel: Channel, attempts: [PlaybackAttempt], userAgent: String) {
        pendingZap?.cancel()
        pendingAttempt?.cancel()
        announced = nil
        self.channel = channel
        self.userAgent = userAgent
        media = MediaInfo()
        tracks = []
        isPaused = false

        guard let first = attempts.first else {
            plan = nil
            state = .failed("Cette chaîne n'a pas d'adresse de lecture.")
            return
        }
        plan = PlaybackPlan(attempts: attempts)
        load(first, state: .connecting(first.label))
    }

    func stop() {
        pendingZap?.cancel()
        pendingAttempt?.cancel()
        plan = nil
        channel = nil
        announced = nil
        variantLabel = nil
        playingSince = nil
        media = MediaInfo()
        tracks = []
        state = .idle
        awaitingStart = false
        player.stop()
    }

    /// Rapid key presses move through the list without opening each stream: the single connection
    /// of the subscription would be refused between two channels.
    func zapDebounced(_ action: @escaping @MainActor () -> Void) {
        pendingZap?.cancel()
        pendingZap = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(350))
            guard !Task.isCancelled else { return }
            action()
        }
    }

    func announce(_ channel: Channel) {
        announced = channel
    }

    func retry() {
        guard let plan, let channel else { return }
        play(channel: channel, attempts: plan.attempts, userAgent: userAgent)
    }

    func togglePause() {
        player.togglePause()
    }

    func toggleMute() {
        player.toggleMute()
    }

    func setVolume(_ value: Double) {
        player.setVolume(value)
        volume = value
    }

    func select(_ track: MediaTrack) {
        player.select(track: track)
        refreshTracks()
    }

    func disableSubtitles() {
        player.disableSubtitles()
        refreshTracks()
    }

    // MARK: Internals

    private func load(_ attempt: PlaybackAttempt, state newState: PlaybackState) {
        variantLabel = attempt.label
        state = newState
        playingSince = nil
        awaitingStart = true
        player.play(url: attempt.url, userAgent: userAgent)
    }

    private func refreshTracks() {
        let channelID = channel?.id
        Task { @MainActor [weak self] in
            guard let snapshot = await self?.player.snapshot(), let self, self.channel?.id == channelID else { return }
            self.tracks = snapshot.tracks
            self.media = snapshot.media
        }
    }

    private func handle(_ event: PlayerEvent) {
        switch event {
        case .startedLoading:
            awaitingStart = false
        case .playing:
            if playingSince == nil { playingSince = Date() }
            if plan != nil { state = .playing }
            refreshTracks()
        case .buffering(let active):
            if plan != nil, playingSince != nil { state = active ? .buffering : .playing }
        case .paused(let paused):
            isPaused = paused
        case .muted(let muted):
            isMuted = muted
        case .mediaChanged:
            if plan != nil { refreshTracks() }
        case .ended(let reason, _):
            guard reason == .streamStopped, !awaitingStart else { return }
            streamStopped()
        }
    }

    private func streamStopped() {
        guard plan != nil else { return }
        let played = playingSince.map { Date().timeIntervalSince($0) } ?? 0
        playingSince = nil
        media = MediaInfo()

        switch plan!.failed(afterPlaying: played) {
        case .exhausted:
            state = .failed("""
                Impossible de lire cette chaîne. L'abonnement n'autorise souvent qu'une connexion : \
                vérifiez qu'il n'est pas utilisé sur un autre appareil, puis réessayez.
                """)
        case .attempt(let attempt, let delay):
            state = .reconnecting(attempt.label)
            pendingAttempt?.cancel()
            pendingAttempt = Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard !Task.isCancelled, let self else { return }
                self.load(attempt, state: .reconnecting(attempt.label))
            }
        }
    }
}
