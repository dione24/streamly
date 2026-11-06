import StreamlyKit
import StreamlyPlayer
import SwiftUI

struct PlayerPane: View {
    @Environment(AppModel.self) private var model
    let theater: Bool
    let toggleTheater: () -> Void

    @State private var guide: NowNext?
    @State private var controlsVisible = true
    @State private var hideTask: Task<Void, Never>?
    @State private var channelListVisible = false
    @State private var showingArchive = false
    @FocusState private var focused: Bool

    private var playback: PlaybackController { model.playback }

    var body: some View {
        VStack(spacing: 0) {
            video
            if !theater { details }
        }
        .background(Theme.background)
        .task(id: playback.channel?.id) {
            guide = nil
            guard let channel = playback.channel else { return }
            guide = await model.nowNext(for: channel)
        }
        .onChange(of: theater) { _, isTheater in
            channelListVisible = false
            focused = true
            if isTheater { revealControls() } else { controlsVisible = true }
        }
        .onChange(of: playback.announced) { _, channel in
            if channel != nil { revealControls() }
        }
    }

    private var video: some View {
        ZStack {
            Color.black
            if model.videoLocation == .main, playback.onDemand == nil, let layer = playback.player.layer {
                VideoSurface(layer: layer)
            }
            stateOverlay
            if controlsVisible, playback.channel != nil { controlsOverlay }
            if theater, channelListVisible {
                HStack(spacing: 0) {
                    ChannelListView(onOpen: { channelListVisible = false })
                        .frame(width: 340)
                        .background(Theme.background.opacity(0.94))
                    Spacer()
                }
                .transition(.move(edge: .leading))
            }
        }
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { toggleTheater() }
        .onTapGesture { focused = true }
        .onContinuousHover { phase in
            if case .active = phase { revealControls() }
        }
        .ignoresSafeArea(edges: .top)
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onKeyPress(keys: [.upArrow, .downArrow, .space, .escape, .leftArrow]) { press in
            handleKey(press.key)
        }
        .onKeyPress(characters: CharacterSet(charactersIn: "fFmMlL")) { press in
            handleCharacter(press.characters.lowercased())
        }
    }

    private func handleKey(_ key: KeyEquivalent) -> KeyPress.Result {
        switch key {
        case .upArrow: model.zap(by: -1)
        case .downArrow: model.zap(by: 1)
        case .space: playback.togglePause()
        case .leftArrow: model.playPrevious()
        case .escape:
            if channelListVisible {
                channelListVisible = false
            } else if theater {
                toggleTheater()
            } else {
                return .ignored
            }
        default: return .ignored
        }
        return .handled
    }

    private func handleCharacter(_ character: String) -> KeyPress.Result {
        switch character {
        case "f": toggleTheater()
        case "m": playback.toggleMute()
        case "l":
            guard theater else { return .ignored }
            withAnimation(.easeOut(duration: 0.18)) { channelListVisible.toggle() }
        default: return .ignored
        }
        return .handled
    }

    private func revealControls() {
        withAnimation(.easeOut(duration: 0.12)) { controlsVisible = true }
        hideTask?.cancel()
        guard theater else { return }
        hideTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(3.5))
            guard !Task.isCancelled else { return }
            withAnimation(.easeIn(duration: 0.25)) { controlsVisible = false }
        }
    }

    @ViewBuilder
    private var stateOverlay: some View {
        switch playback.state {
        case .idle:
            VStack(spacing: 10) {
                Image(systemName: "play.tv")
                    .font(.system(size: 42, weight: .light))
                    .foregroundStyle(Theme.faint)
                Text("Choisissez une chaîne")
                    .font(.system(size: 15))
                    .foregroundStyle(Theme.muted)
                if model.preferences.lastChannel != nil {
                    Button("Reprendre la dernière chaîne") { model.resumeLastChannel() }
                        .buttonStyle(PrimaryButtonStyle())
                        .padding(.top, 8)
                }
            }
        case .connecting:
            message("Connexion…", spinner: true)
        case .reconnecting:
            message("Reconnexion à la chaîne…", spinner: true)
        case .buffering:
            message("Mise en mémoire tampon…", spinner: true)
        case .failed(let text):
            VStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 30))
                    .foregroundStyle(Theme.danger)
                Text(text)
                    .font(.system(size: 14))
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 420)
                Button("Réessayer") { playback.retry() }
                    .buttonStyle(PrimaryButtonStyle())
            }
            .padding(24)
            .background(Theme.surface.opacity(0.92), in: RoundedRectangle(cornerRadius: 14))
        case .playing, .finished:
            EmptyView()
        }
    }

    private func message(_ text: String, spinner: Bool) -> some View {
        HStack(spacing: 10) {
            if spinner { ProgressView().controlSize(.small) }
            Text(text).font(.system(size: 13, weight: .medium))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(.black.opacity(0.65), in: RoundedRectangle(cornerRadius: 10))
    }

    /// Full-screen band: what is on, how far along, and what comes next, over the picture.
    @ViewBuilder
    private var theaterInfo: some View {
        if theater, let channel = playback.announced ?? playback.channel {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    ChannelLogo(channel: channel, size: 38)
                    Text(channel.name)
                        .font(.system(size: 24, weight: .bold))
                        .lineLimit(1)
                    if playback.state == .playing || playback.state == .buffering { LiveBadge() }
                }
                if playback.announced == nil, let now = guide?.now {
                    Text(now.title)
                        .font(.system(size: 15, weight: .medium))
                        .lineLimit(1)
                    HStack(spacing: 10) {
                        Text(Format.time(now.start))
                        ProgressView(value: now.progress(at: Date()))
                            .progressViewStyle(.linear)
                            .tint(Theme.violet)
                        Text(Format.time(now.stop))
                    }
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.muted)
                    if let next = guide?.next {
                        Text("Ensuite : \(next.title)")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.muted)
                            .lineLimit(1)
                    }
                }
            }
            .padding(.horizontal, 16)
        }
    }

    private var controlsOverlay: some View {
        VStack(spacing: 0) {
            Spacer()
            theaterInfo
            HStack(spacing: 12) {
                Button {
                    playback.togglePause()
                } label: {
                    Image(systemName: playback.isPaused ? "play.fill" : "pause.fill")
                }
                .help("Pause (espace)")

                Button {
                    playback.toggleMute()
                } label: {
                    Image(systemName: playback.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                }
                .help("Couper le son (M)")

                Slider(value: Binding(get: { playback.volume }, set: { playback.setVolume($0) }), in: 0...100)
                    .frame(width: 90)
                    .controlSize(.small)

                Spacer()
                if theater {
                    Button {
                        withAnimation(.easeOut(duration: 0.18)) { channelListVisible.toggle() }
                    } label: {
                        Label("Chaînes", systemImage: "list.bullet")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    .help("Liste des chaînes (L)")
                }
                TracksMenu()
                Button(action: toggleTheater) {
                    Image(systemName: theater ? "arrow.down.right.and.arrow.up.left"
                                              : "arrow.up.left.and.arrow.down.right")
                }
                .help("Plein écran (F)")
            }
            .buttonStyle(.plain)
            .font(.system(size: 14))
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
        .background(alignment: .bottom) {
            LinearGradient(colors: [.clear, .black.opacity(0.85)], startPoint: .top, endPoint: .bottom)
                .frame(height: theater ? 240 : 90)
                .allowsHitTesting(false)
        }
    }

    @ViewBuilder
    private var details: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let channel = playback.announced ?? playback.channel {
                HStack(spacing: 12) {
                    ChannelLogo(channel: channel, size: 44)
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 8) {
                            Text(channel.name)
                                .font(.system(size: 18, weight: .bold))
                                .lineLimit(1)
                            if playback.state == .playing || playback.state == .buffering { LiveBadge() }
                        }
                        Text(technicalLine)
                            .font(.system(size: 11).monospacedDigit())
                            .foregroundStyle(Theme.muted)
                    }
                    Spacer()
                    if channel.archiveStream != nil {
                        Button {
                            showingArchive = true
                        } label: {
                            Label("Rattrapage", systemImage: "clock.arrow.circlepath")
                                .font(.system(size: 12, weight: .medium))
                        }
                        .buttonStyle(SecondaryButtonStyle())
                        .help("Revoir un programme passé")
                        .sheet(isPresented: $showingArchive) {
                            CatchUpSheet(channel: channel)
                        }
                    }
                    Button {
                        model.toggleFavorite(channel)
                    } label: {
                        Image(systemName: model.isFavorite(channel) ? "star.fill" : "star")
                            .font(.system(size: 16))
                            .foregroundStyle(model.isFavorite(channel) ? Theme.accent : Theme.muted)
                    }
                    .buttonStyle(.plain)
                    .help(model.isFavorite(channel) ? "Retirer des favoris" : "Ajouter aux favoris")
                }

                if let now = guide?.now {
                    programme(now, caption: "En ce moment", showProgress: true)
                }
                if let next = guide?.next {
                    programme(next, caption: "Ensuite", showProgress: false)
                }
                if guide?.now == nil, guide?.next == nil {
                    Text("Pas de programme disponible pour cette chaîne.")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.faint)
                }
            } else {
                Text("Les flèches ↑ ↓ changent de chaîne, F passe en plein écran.")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.faint)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, minHeight: 150, alignment: .topLeading)
        .background(Theme.surface)
    }

    private var technicalLine: String {
        var parts: [String] = []
        if let height = playback.media.height, let width = playback.media.width {
            parts.append("\(width)×\(height)")
        }
        if let codec = playback.media.videoCodec?.components(separatedBy: " / ").first {
            parts.append(codec)
        }
        if let audio = playback.media.audioCodec { parts.append(audio.uppercased()) }
        if playback.media.hardwareDecoder != nil { parts.append("décodage matériel") }
        if parts.isEmpty, let label = playback.variantLabel { parts.append(label) }
        return parts.joined(separator: " · ")
    }

    private func programme(_ item: Programme, caption: String, showProgress: Bool) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(caption.uppercased())
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Theme.accent)
                Text("\(Format.time(item.start)) – \(Format.time(item.stop))")
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.muted)
            }
            Text(item.title)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(1)
            if showProgress {
                ProgressView(value: item.progress(at: Date()))
                    .progressViewStyle(.linear)
                    .tint(Theme.violet)
            }
        }
    }
}

/// Past programmes of a channel, latest first, grouped by day.
struct CatchUpSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let channel: Channel
    @State private var programmes: [Programme]?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Rattrapage").font(.system(size: 20, weight: .heavy))
                    Text(channel.name).font(.system(size: 12)).foregroundStyle(Theme.muted)
                }
                Spacer()
                Button("Fermer") { dismiss() }
                    .buttonStyle(SecondaryButtonStyle())
                    .keyboardShortcut(.cancelAction)
            }
            if let programmes {
                if programmes.isEmpty {
                    ContentUnavailableView("Aucun programme à revoir", systemImage: "clock",
                                           description: Text("Le fournisseur n'en propose pas pour cette chaîne en ce moment."))
                        .foregroundStyle(Theme.muted)
                        .frame(maxHeight: .infinity)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 4) {
                            ForEach(programmes, id: \.self) { programme in
                                Button {
                                    dismiss()
                                    model.replay(channel, programme: programme)
                                } label: {
                                    HStack(spacing: 12) {
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(Format.time(programme.start))
                                                .font(.system(size: 13, weight: .semibold).monospacedDigit())
                                            Text(Format.date(programme.start))
                                                .font(.system(size: 10))
                                                .foregroundStyle(Theme.muted)
                                        }
                                        .frame(width: 96, alignment: .leading)
                                        Text(programme.title)
                                            .font(.system(size: 13))
                                            .lineLimit(1)
                                        Spacer()
                                        Image(systemName: "play.fill")
                                            .font(.system(size: 10))
                                            .foregroundStyle(Theme.accent)
                                    }
                                    .padding(9)
                                    .contentShape(Rectangle())
                                    .background(Theme.surface, in: RoundedRectangle(cornerRadius: 8))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(22)
        .frame(width: 520, height: 500)
        .background(Theme.background)
        .task { programmes = await model.archive(for: channel) }
    }
}
