import StreamlyKit
import StreamlyPlayer
import SwiftUI

/// Audio and subtitle choice, shared by both players.
struct TracksMenu: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let playback = model.playback
        let audio = playback.tracks.filter { $0.kind == .audio }
        let subtitles = playback.tracks.filter { $0.kind == .sub }
        Menu {
            Section("Audio") {
                ForEach(audio) { track in
                    Button { playback.select(track) } label: {
                        Label(track.title, systemImage: track.selected ? "checkmark" : "")
                    }
                }
            }
            if !subtitles.isEmpty {
                Section("Sous-titres") {
                    Button("Aucun") { playback.disableSubtitles() }
                    ForEach(subtitles) { track in
                        Button { playback.select(track) } label: {
                            Label(track.title, systemImage: track.selected ? "checkmark" : "")
                        }
                    }
                }
            }
        } label: {
            Image(systemName: "text.bubble")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(audio.isEmpty && subtitles.isEmpty)
        .help("Pistes audio et sous-titres")
    }
}

struct OnDemandPlayerView: View {
    @Environment(AppModel.self) private var model

    @State private var controlsVisible = true
    @State private var hideTask: Task<Void, Never>?
    @State private var scrubbing: Double?
    @State private var countdown = 8
    @FocusState private var focused: Bool

    private var playback: PlaybackController { model.playback }

    var body: some View {
        ZStack {
            Color.black
            if model.videoLocation == .main, let layer = playback.player.layer {
                VideoSurface(layer: layer)
            }
            stateOverlay
            if controlsVisible || playback.state == .finished { controls }
            if playback.state == .finished { endOverlay }
        }
        .ignoresSafeArea()
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { model.setFullScreen(!model.isFullScreen) }
        .onTapGesture {
            focused = true
            playback.togglePause()
        }
        .onContinuousHover { phase in
            if case .active = phase { revealControls() }
        }
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onKeyPress(keys: [.space, .escape, .leftArrow, .rightArrow, .upArrow, .downArrow]) { handleKey($0.key) }
        .onKeyPress(characters: CharacterSet(charactersIn: "fFmMnN")) { handleCharacter($0.characters.lowercased()) }
        .onAppear {
            focused = true
            revealControls()
        }
        .onChange(of: playback.state) { _, state in
            if state == .finished { startCountdown() }
        }
    }

    private func handleKey(_ key: KeyEquivalent) -> KeyPress.Result {
        switch key {
        case .space: playback.togglePause()
        case .escape: model.closeOnDemand()
        case .leftArrow: playback.seek(by: -10)
        case .rightArrow: playback.seek(by: 10)
        case .upArrow: playback.setVolume(min(playback.volume + 5, 100))
        case .downArrow: playback.setVolume(max(playback.volume - 5, 0))
        default: return .ignored
        }
        revealControls()
        return .handled
    }

    private func handleCharacter(_ character: String) -> KeyPress.Result {
        switch character {
        case "f": model.setFullScreen(!model.isFullScreen)
        case "m": playback.toggleMute()
        case "n": model.playNextEpisode()
        default: return .ignored
        }
        return .handled
    }

    private func revealControls() {
        withAnimation(.easeOut(duration: 0.12)) { controlsVisible = true }
        hideTask?.cancel()
        hideTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(3.5))
            guard !Task.isCancelled, scrubbing == nil else { return }
            withAnimation(.easeIn(duration: 0.25)) { controlsVisible = false }
        }
    }

    /// Credits are rolling: the next episode starts by itself unless the viewer leaves.
    private func startCountdown() {
        guard model.nextEpisode != nil else { return }
        countdown = 8
        Task { @MainActor in
            while countdown > 0 {
                try? await Task.sleep(for: .seconds(1))
                guard playback.state == .finished else { return }
                countdown -= 1
            }
            model.playNextEpisode()
        }
    }

    @ViewBuilder
    private var stateOverlay: some View {
        switch playback.state {
        case .connecting, .reconnecting:
            stateMessage("Chargement…")
        case .buffering:
            stateMessage("Mise en mémoire tampon…")
        case .failed(let text):
            VStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 30))
                    .foregroundStyle(Theme.danger)
                Text(text)
                    .font(.system(size: 14))
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 420)
                HStack(spacing: 10) {
                    Button("Réessayer") { playback.retry() }
                        .buttonStyle(PrimaryButtonStyle())
                    Button("Fermer") { model.closeOnDemand() }
                        .buttonStyle(SecondaryButtonStyle())
                }
            }
            .padding(24)
            .background(Theme.surface.opacity(0.94), in: RoundedRectangle(cornerRadius: 14))
        default:
            EmptyView()
        }
    }

    private func stateMessage(_ text: String) -> some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text(text).font(.system(size: 13, weight: .medium))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(.black.opacity(0.65), in: RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder
    private var endOverlay: some View {
        VStack(spacing: 14) {
            if let next = model.nextEpisode {
                Text("Épisode suivant dans \(countdown) s")
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.muted)
                Text("\(next.label) · \(next.title)")
                    .font(.system(size: 20, weight: .bold))
                HStack(spacing: 10) {
                    Button("Lire maintenant") { model.playNextEpisode() }
                        .buttonStyle(PrimaryButtonStyle())
                    Button("Fermer") { model.closeOnDemand() }
                        .buttonStyle(SecondaryButtonStyle())
                }
            } else {
                Text("Terminé")
                    .font(.system(size: 20, weight: .bold))
                Button("Fermer") { model.closeOnDemand() }
                    .buttonStyle(PrimaryButtonStyle())
            }
        }
        .padding(28)
        .background(Theme.surface.opacity(0.94), in: RoundedRectangle(cornerRadius: 16))
    }

    private var controls: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Button {
                    model.closeOnDemand()
                } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 16, weight: .semibold))
                }
                .help("Fermer (Échap)")
                VStack(alignment: .leading, spacing: 2) {
                    if let item = playback.onDemand {
                        Text(item.title)
                            .font(.system(size: 17, weight: .bold))
                            .lineLimit(1)
                        if let subtitle = item.subtitle {
                            Text(subtitle)
                                .font(.system(size: 12))
                                .foregroundStyle(Theme.muted)
                                .lineLimit(1)
                        }
                    }
                }
                Spacer()
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 20)
            .padding(.top, model.isFullScreen ? 18 : 34)
            .padding(.bottom, 40)
            .background(LinearGradient(colors: [.black.opacity(0.75), .clear], startPoint: .top, endPoint: .bottom))

            Spacer()

            VStack(spacing: 8) {
                timeline
                HStack(spacing: 16) {
                    Button { playback.seek(by: -10) } label: { Image(systemName: "gobackward.10") }
                        .help("Reculer de 10 s (←)")
                    Button { playback.togglePause() } label: {
                        Image(systemName: playback.isPaused ? "play.fill" : "pause.fill")
                            .font(.system(size: 20))
                    }
                    .help("Pause (espace)")
                    Button { playback.seek(by: 10) } label: { Image(systemName: "goforward.10") }
                        .help("Avancer de 10 s (→)")
                    Button { playback.toggleMute() } label: {
                        Image(systemName: playback.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                    }
                    .help("Couper le son (M)")
                    Slider(value: Binding(get: { playback.volume }, set: { playback.setVolume($0) }), in: 0...100)
                        .frame(width: 90)
                        .controlSize(.small)
                    Spacer()
                    if model.nextEpisode != nil {
                        Button {
                            model.playNextEpisode()
                        } label: {
                            Label("Épisode suivant", systemImage: "forward.end.fill")
                                .font(.system(size: 12, weight: .semibold))
                        }
                        .help("Épisode suivant (N)")
                    }
                    TracksMenu()
                    Button {
                        model.setFullScreen(!model.isFullScreen)
                    } label: {
                        Image(systemName: model.isFullScreen ? "arrow.down.right.and.arrow.up.left"
                                                             : "arrow.up.left.and.arrow.down.right")
                    }
                    .help("Plein écran (F)")
                }
                .buttonStyle(.plain)
                .font(.system(size: 15))
            }
            .padding(.horizontal, 20)
            .padding(.top, 50)
            .padding(.bottom, 16)
            .background(LinearGradient(colors: [.clear, .black.opacity(0.85)], startPoint: .top, endPoint: .bottom))
        }
    }

    private var timeline: some View {
        HStack(spacing: 10) {
            Text(Duration.clock(scrubbing ?? playback.position))
                .font(.system(size: 11).monospacedDigit())
            Slider(
                value: Binding(get: { scrubbing ?? playback.position }, set: { scrubbing = $0 }),
                in: 0...max(playback.duration, 1),
                onEditingChanged: { editing in
                    if !editing, let target = scrubbing {
                        playback.seek(to: target)
                        scrubbing = nil
                        revealControls()
                    }
                })
                .controlSize(.small)
                .disabled(playback.duration <= 0)
            Text(Duration.clock(playback.duration))
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(Theme.muted)
        }
    }
}
