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
    case finished
    case failed(String)
}

/// A movie, an episode or a past programme: anything with a timeline.
struct OnDemandItem: Equatable {
    /// `WatchProgress.key`; nil for a replayed programme, whose position is not kept.
    let progressKey: String?
    let title: String
    let subtitle: String?
    let poster: String?
    let url: String
    var resumeAt: Double?
    var seriesID: String?
    var episode: Episode?
}

enum PlaybackContent: Equatable {
    case live(Channel)
    case onDemand(OnDemandItem)
}

/// Runs the player for one item at a time and follows the `PlaybackPlan` when a stream fails.
@MainActor
@Observable
final class PlaybackController {
    private(set) var state: PlaybackState = .idle
    private(set) var content: PlaybackContent?
    /// Channel name shown while zapping, before the debounced switch happens.
    private(set) var announced: Channel?
    private(set) var variantLabel: String?
    private(set) var media = MediaInfo()
    private(set) var tracks: [MediaTrack] = []
    private(set) var isPaused = false
    private(set) var isMuted = false
    private(set) var volume = 100.0
    /// Timeline of an on-demand item, refreshed once per second.
    private(set) var position: Double = 0
    private(set) var duration: Double = 0

    var channel: Channel? {
        if case .live(let channel) = content { return channel }
        return nil
    }

    var onDemand: OnDemandItem? {
        if case .onDemand(let item) = content { return item }
        return nil
    }

    @ObservationIgnored let player = MPVPlayer()
    @ObservationIgnored var onProgress: ((OnDemandItem, Double, Double) -> Void)?
    /// Asked when a live stream dies for good, before showing the failure.
    @ObservationIgnored var onLiveFailed: (() -> Void)?

    @ObservationIgnored private var plan: PlaybackPlan?
    @ObservationIgnored private var userAgent = Playlist.defaultUserAgent
    @ObservationIgnored private var playingSince: Date?
    @ObservationIgnored private var pendingAttempt: Task<Void, Never>?
    @ObservationIgnored private var pendingZap: Task<Void, Never>?
    @ObservationIgnored private var ticker: Task<Void, Never>?
    @ObservationIgnored private var watchdog: Task<Void, Never>?
    @ObservationIgnored private var lastProgressReport = Date.distantPast

    /// Ended events are delivered late: one from the previous stream must not count against the
    /// new one, so we wait for the `loadfile` we asked for to start before trusting them.
    @ObservationIgnored private var awaitingStart = false

    init() {
        player.onEvent = { [weak self] event in
            MainActor.assumeIsolated { self?.handle(event) }
        }
    }

    func play(channel: Channel, attempts: [PlaybackAttempt], userAgent: String) {
        prepareForNewContent()
        content = .live(channel)
        self.userAgent = userAgent

        guard let first = attempts.first else {
            plan = nil
            state = .failed("Cette chaîne n'a pas d'adresse de lecture.")
            return
        }
        plan = PlaybackPlan(attempts: attempts)
        load(first, state: .connecting(first.label))
    }

    func play(item: OnDemandItem, userAgent: String) {
        prepareForNewContent()
        content = .onDemand(item)
        self.userAgent = userAgent
        position = item.resumeAt ?? 0

        let attempt = PlaybackAttempt(url: item.url, label: "")
        plan = PlaybackPlan(attempts: [attempt], retriesPerVariant: 3)
        load(attempt, state: .connecting(""), startAt: item.resumeAt)
        startTicker()
    }

    /// The engine replaced its encoding: same URL, nothing for the viewer to do.
    func reload() {
        guard let attempt = plan?.current else { return }
        load(attempt, state: state == .playing ? .playing : .reconnecting(attempt.label))
    }

    func stop() {
        reportProgress(force: true)
        prepareForNewContent()
        plan = nil
        content = nil
        variantLabel = nil
        state = .idle
        awaitingStart = false
        player.stop()
    }

    private func prepareForNewContent() {
        reportProgress(force: true)
        pendingZap?.cancel()
        pendingAttempt?.cancel()
        watchdog?.cancel()
        ticker?.cancel()
        ticker = nil
        announced = nil
        media = MediaInfo()
        tracks = []
        isPaused = false
        position = 0
        duration = 0
        playingSince = nil
        lastProgressReport = .distantPast
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
        switch content {
        case .live(let channel):
            guard let plan else { return }
            play(channel: channel, attempts: plan.attempts, userAgent: userAgent)
        case .onDemand(var item):
            item.resumeAt = position > 0 ? position : item.resumeAt
            play(item: item, userAgent: userAgent)
        case nil:
            break
        }
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

    func seek(to target: Double) {
        position = target
        player.seek(to: target)
    }

    func seek(by offset: Double) {
        let target = min(max(position + offset, 0), duration > 0 ? duration : position + offset)
        position = target
        player.seek(by: offset)
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

    private func load(_ attempt: PlaybackAttempt, state newState: PlaybackState, startAt: Double? = nil) {
        variantLabel = attempt.label.isEmpty ? nil : attempt.label
        state = newState
        playingSince = nil
        awaitingStart = true
        player.play(url: attempt.url, userAgent: userAgent, startAt: startAt, onDemand: onDemand != nil)
        startWatchdog()
    }

    /// mpv times out on the network by itself, but a refused command produces no event at all: the
    /// screen must not stay on "loading" forever.
    private func startWatchdog() {
        watchdog?.cancel()
        watchdog = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(30))
            guard !Task.isCancelled, let self, self.playingSince == nil else { return }
            self.awaitingStart = false
            self.streamStopped()
        }
    }

    private func refreshTracks() {
        let current = content
        Task { @MainActor [weak self] in
            guard let snapshot = await self?.player.snapshot(), let self, self.content == current else { return }
            self.tracks = snapshot.tracks
            self.media = snapshot.media
        }
    }

    private func startTicker() {
        ticker = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled, let self else { return }
                let snapshot = await self.player.snapshot()
                guard !Task.isCancelled, self.onDemand != nil, self.state == .playing || self.state == .buffering else { continue }
                if let position = snapshot.position { self.position = position }
                if let duration = snapshot.duration, duration > 0 { self.duration = duration }
                self.reportProgress(force: false)
            }
        }
    }

    private func reportProgress(force: Bool) {
        guard let item = onDemand, duration > 0, position > 0 else { return }
        guard force || Date().timeIntervalSince(lastProgressReport) >= 5 else { return }
        lastProgressReport = Date()
        onProgress?(item, position, duration)
    }

    private func handle(_ event: PlayerEvent) {
        switch event {
        case .startedLoading:
            awaitingStart = false
        case .playing:
            watchdog?.cancel()
            if playingSince == nil { playingSince = Date() }
            if plan != nil, state != .finished { state = .playing }
            refreshTracks()
        case .buffering(let active):
            if plan != nil, playingSince != nil, state != .finished { state = active ? .buffering : .playing }
        case .paused(let paused):
            isPaused = paused
        case .muted(let muted):
            isMuted = muted
        case .mediaChanged:
            if plan != nil { refreshTracks() }
        case .ended(let reason, _):
            guard reason != .replaced, !awaitingStart else { return }
            if reason == .finished, onDemand != nil, duration > 0, duration - position < 20 {
                finishOnDemand()
            } else {
                streamStopped()
            }
        }
    }

    private func finishOnDemand() {
        position = duration
        reportProgress(force: true)
        ticker?.cancel()
        ticker = nil
        state = .finished
    }

    private func streamStopped() {
        guard plan != nil else { return }
        let played = playingSince.map { Date().timeIntervalSince($0) } ?? 0
        playingSince = nil
        let isOnDemand = onDemand != nil
        if !isOnDemand { media = MediaInfo() }

        switch plan!.failed(afterPlaying: played) {
        case .exhausted:
            if !isOnDemand { onLiveFailed?() }
            state = .failed(isOnDemand
                ? "Impossible de lire ce titre. Vérifiez que l'abonnement n'est pas utilisé sur un autre appareil, puis réessayez."
                : """
                Impossible de lire cette chaîne. L'abonnement n'autorise souvent qu'une connexion : \
                vérifiez qu'il n'est pas utilisé sur un autre appareil, puis réessayez.
                """)
        case .attempt(let attempt, let delay):
            state = .reconnecting(attempt.label)
            pendingAttempt?.cancel()
            let resumeAt = isOnDemand && position > 0 ? position : nil
            pendingAttempt = Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard !Task.isCancelled, let self else { return }
                self.load(attempt, state: .reconnecting(attempt.label), startAt: resumeAt)
            }
        }
    }
}
