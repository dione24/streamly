import StreamlyKit
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model
    @State private var theater = false

    var body: some View {
        @Bindable var bindable = model
        Group {
            if let error = model.startupError {
                ContentUnavailableView("Démarrage impossible", systemImage: "exclamationmark.triangle",
                                       description: Text(error))
            } else if model.hasPlaylist {
                browser
            } else {
                AddPlaylistView(isWelcome: true)
            }
        }
        .background(Theme.background)
        .foregroundStyle(Theme.text)
        .sheet(isPresented: $bindable.showingSettings) { SettingsView() }
        .sheet(isPresented: $bindable.showingAddPlaylist) {
            AddPlaylistView(isWelcome: false)
                .frame(width: 520, height: 560)
                .background(Theme.background)
        }
    }

    private var browser: some View {
        HStack(spacing: 0) {
            if !theater {
                SidebarView()
                    .frame(width: 216)
                Divider().overlay(Theme.line)
                ChannelListView(onOpen: { setTheater(true) })
                    .frame(width: 340)
                Divider().overlay(Theme.line)
            }
            PlayerPane(theater: theater, toggleTheater: { setTheater(!theater) })
        }
        // The green button and the system's Esc also change full screen: follow them.
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didExitFullScreenNotification)) { _ in
            theater = false
        }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didEnterFullScreenNotification)) { _ in
            theater = true
        }
    }

    private func setTheater(_ value: Bool) {
        guard value != theater else { return }
        theater = value
        guard let window = NSApp.keyWindow ?? NSApp.windows.first else { return }
        if window.styleMask.contains(.fullScreen) != value {
            window.toggleFullScreen(nil)
        }
    }
}

struct SidebarView: View {
    @Environment(AppModel.self) private var model
    @State private var renaming: ChannelCategory?
    @State private var renameText = ""

    var body: some View {
        content.alert(
            "Renommer la catégorie",
            isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })
        ) {
            TextField("Nom", text: $renameText)
            Button("Renommer") {
                if let renaming { model.rename(renaming, to: renameText) }
            }
            Button("Annuler", role: .cancel) {}
        } message: {
            Text("Laissez vide pour retrouver le nom du fournisseur.")
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 7) {
                Text("Streamly")
                    .font(.system(size: 19, weight: .heavy))
                    .foregroundStyle(Theme.gradient)
                Spacer()
                Button {
                    model.showingSettings = true
                } label: {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.muted)
                .help("Réglages")
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 12)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    row("Toutes les chaînes", icon: "square.grid.2x2", item: .all)
                    row("Favoris", icon: "star", item: .favorites)
                    row("Récents", icon: "clock", item: .recents)
                    if !model.categories.isEmpty {
                        Text("CATÉGORIES")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Theme.faint)
                            .padding(.horizontal, 10)
                            .padding(.top, 16)
                            .padding(.bottom, 4)
                        ForEach(model.orderedCategories) { category in
                            row(model.name(of: category), icon: nil, item: .category(category.id))
                                .contextMenu {
                                    Button("Renommer…") {
                                        renameText = model.name(of: category)
                                        renaming = category
                                    }
                                }
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 12)
            }

            if let account = model.active {
                VStack(alignment: .leading, spacing: 2) {
                    Text(account.name).font(.system(size: 12, weight: .medium)).lineLimit(1)
                    if let expiry = account.account?.expiresAt {
                        Text("Expire le \(Format.date(expiry))")
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.muted)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.surface)
            }
        }
        .background(Theme.surface)
    }

    private func row(_ title: String, icon: String?, item: SidebarItem) -> some View {
        let selected = model.sidebar == item
        return Button {
            model.sidebar = item
        } label: {
            HStack(spacing: 8) {
                if let icon {
                    Image(systemName: icon)
                        .frame(width: 16)
                        .foregroundStyle(selected ? Theme.accent : Theme.muted)
                }
                Text(title).lineLimit(1)
                Spacer(minLength: 4)
                Text("\(model.count(in: item))")
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.faint)
            }
            .font(.system(size: 13, weight: selected ? .semibold : .regular))
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .contentShape(Rectangle())
            .background(selected ? Theme.violet.opacity(0.18) : .clear, in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }
}

struct ChannelListView: View {
    @Environment(AppModel.self) private var model
    let onOpen: () -> Void
    @State private var selection: Channel.ID?
    @FocusState private var searchFocused: Bool
    @FocusState private var listFocused: Bool

    var body: some View {
        @Bindable var bindable = model
        VStack(spacing: 0) {
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.faint)
                TextField("Rechercher une chaîne", text: $bindable.query)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                if !model.query.isEmpty {
                    Button {
                        model.query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.faint)
                }
                Menu {
                    Picker("Trier", selection: Binding(get: { model.preferences.order }, set: { model.setOrder($0) })) {
                        Text("Ordre du fournisseur").tag(ListOrder.provider)
                        Text("De A à Z").tag(ListOrder.ascending)
                        Text("De Z à A").tag(ListOrder.descending)
                    }
                    .pickerStyle(.inline)
                } label: {
                    Image(systemName: "arrow.up.arrow.down")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .foregroundStyle(model.preferences.order == .provider ? Theme.faint : Theme.accent)
                .help("Trier les chaînes et les catégories")
            }
            .font(.system(size: 13))
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 9))
            .padding(.horizontal, 12)
            .padding(.top, 6)
            .padding(.bottom, 10)
            .background {
                Button("") { searchFocused = true }
                    .keyboardShortcut("f")
                    .opacity(0)
            }

            if model.visibleChannels.isEmpty {
                ContentUnavailableView(emptyTitle, systemImage: "tv", description: Text(emptyDetail))
                    .foregroundStyle(Theme.muted)
                    .frame(maxHeight: .infinity)
            } else {
                // No List selection: the system would draw its own grey or blue highlight.
                ScrollViewReader { proxy in
                    List(model.visibleChannels) { channel in
                        ChannelRow(channel: channel, isPlaying: model.playback.channel?.id == channel.id)
                            .id(channel.id)
                            .listRowBackground(rowBackground(for: channel))
                            .listRowSeparator(.hidden)
                            .contextMenu {
                                Button(model.isFavorite(channel) ? "Retirer des favoris" : "Ajouter aux favoris") {
                                    model.toggleFavorite(channel)
                                }
                            }
                            .simultaneousGesture(TapGesture(count: 2).onEnded { onOpen() })
                            .simultaneousGesture(TapGesture().onEnded {
                                listFocused = true
                                choose(channel)
                            })
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .focusable()
                    .focusEffectDisabled()
                    .focused($listFocused)
                    .onKeyPress(keys: [.upArrow, .downArrow, .return]) { press in
                        if press.key == .return {
                            onOpen()
                        } else {
                            move(by: press.key == .downArrow ? 1 : -1, proxy: proxy)
                        }
                        return .handled
                    }
                    .onChange(of: model.playback.channel?.id) { _, id in
                        if selection != id, let id {
                            selection = id
                            proxy.scrollTo(id)
                        }
                    }
                    .onAppear {
                        selection = model.playback.channel?.id
                        listFocused = true
                    }
                }
            }
        }
        .background(Theme.background)
    }

    /// Debounced so that holding an arrow key does not open every stream on the way.
    private func choose(_ channel: Channel) {
        selection = channel.id
        guard channel.id != model.playback.channel?.id else { return }
        model.playback.zapDebounced { model.play(channel) }
        model.playback.announce(channel)
    }

    private func move(by offset: Int, proxy: ScrollViewProxy) {
        let list = model.visibleChannels
        guard !list.isEmpty else { return }
        let index = list.firstIndex { $0.id == selection }.map { $0 + offset } ?? 0
        let target = list[min(max(index, 0), list.count - 1)]
        choose(target)
        proxy.scrollTo(target.id)
    }

    private var emptyTitle: String {
        if !model.query.isEmpty { return "Aucun résultat" }
        switch model.sidebar {
        case .favorites: return "Aucun favori"
        case .recents: return "Rien de regardé pour l'instant"
        default: return "Aucune chaîne"
        }
    }

    private var emptyDetail: String {
        if !model.query.isEmpty { return "Essayez un autre mot." }
        if model.sidebar == .favorites { return "Clic droit sur une chaîne pour l'ajouter." }
        return ""
    }

    private func rowBackground(for channel: Channel) -> some View {
        RoundedRectangle(cornerRadius: 8)
            .fill(selection == channel.id ? Theme.violet.opacity(0.22) : .clear)
            .padding(.horizontal, 6)
    }
}

struct ChannelRow: View {
    @Environment(AppModel.self) private var model
    let channel: Channel
    let isPlaying: Bool
    @State private var guide: NowNext?

    var body: some View {
        HStack(spacing: 10) {
            ChannelLogo(channel: channel)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if let number = channel.number {
                        Text("\(number)")
                            .font(.system(size: 11).monospacedDigit())
                            .foregroundStyle(Theme.faint)
                    }
                    Text(channel.name)
                        .font(.system(size: 13, weight: isPlaying ? .semibold : .medium))
                        .foregroundStyle(isPlaying ? Theme.accent : Theme.text)
                        .lineLimit(1)
                    if model.isFavorite(channel) {
                        Image(systemName: "star.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(Theme.accent)
                    }
                }
                if let now = guide?.now {
                    Text(now.title)
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                    ProgressView(value: now.progress(at: Date()))
                        .progressViewStyle(.linear)
                        .controlSize(.mini)
                        .tint(Theme.violet)
                }
            }
            Spacer(minLength: 4)
            VStack(alignment: .trailing, spacing: 3) {
                if let quality = channel.bestQuality { QualityBadge(text: quality) }
                if let lang = channel.lang { QualityBadge(text: lang) }
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .task(id: channel.id) {
            // Rows scrolled past quickly are cancelled here and never reach the provider.
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled else { return }
            guide = await model.nowNext(for: channel)
        }
    }
}
