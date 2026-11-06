import StreamlyKit
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var bindable = model
        Group {
            if let error = model.startupError {
                ContentUnavailableView("Démarrage impossible", systemImage: "exclamationmark.triangle",
                                       description: Text(error))
            } else if model.hasPlaylist {
                ZStack {
                    browser
                    if model.playback.onDemand != nil {
                        OnDemandPlayerView()
                            .transition(.opacity)
                    }
                }
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
        // The green button and the system's Esc also change full screen: follow them.
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didExitFullScreenNotification)) { _ in
            model.noteFullScreen(false)
        }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didEnterFullScreenNotification)) { _ in
            model.noteFullScreen(true)
        }
    }

    private var theater: Bool { model.isFullScreen && model.section == .direct }

    private var browser: some View {
        HStack(spacing: 0) {
            if !theater {
                SidebarView()
                    .frame(width: 216)
                Divider().overlay(Theme.line)
            }
            switch model.section {
            case .direct:
                if !theater {
                    ChannelListView(onOpen: { model.setFullScreen(true) })
                        .frame(width: 340)
                    Divider().overlay(Theme.line)
                }
                PlayerPane(theater: theater, toggleTheater: { model.setFullScreen(!model.isFullScreen) })
            case .guide:
                GuideScreen()
            case .movies:
                CatalogView(kind: .movie)
            case .series:
                CatalogView(kind: .series)
            case .search:
                SearchView()
            }
        }
    }
}

struct SidebarView: View {
    @Environment(AppModel.self) private var model
    @State private var renaming: (category: ChannelCategory, section: String)?
    @State private var renameText = ""

    var body: some View {
        content.alert(
            "Renommer la catégorie",
            isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })
        ) {
            TextField("Nom", text: $renameText)
            Button("Renommer") {
                if let renaming { model.rename(renaming.category, in: renaming.section, to: renameText) }
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
            .padding(.bottom, 10)

            VStack(spacing: 2) {
                ForEach(AppSection.allCases, id: \.self) { section in
                    sectionRow(section)
                }
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 8)

            Divider().overlay(Theme.line)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    switch model.section {
                    case .direct, .guide:
                        liveFilters
                    case .movies:
                        catalogFilters(.movie)
                    case .series:
                        catalogFilters(.series)
                    case .search:
                        EmptyView()
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 8)
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

    private func sectionRow(_ section: AppSection) -> some View {
        let selected = model.section == section
        return Button {
            model.section = section
            if section == .movies { model.open(nil, kind: .movie) }
            if section == .series { model.open(nil, kind: .series) }
        } label: {
            HStack(spacing: 9) {
                Image(systemName: section.icon)
                    .frame(width: 17)
                    .foregroundStyle(selected ? Theme.accent : Theme.muted)
                Text(section.title)
                Spacer()
            }
            .font(.system(size: 13, weight: selected ? .semibold : .medium))
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .contentShape(Rectangle())
            .background(selected ? Theme.violet.opacity(0.18) : .clear, in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var liveFilters: some View {
        row("Toutes les chaînes", icon: "square.grid.2x2", selected: model.sidebar == .all,
            count: model.count(in: .all)) { model.sidebar = .all }
        row("Favoris", icon: "star", selected: model.sidebar == .favorites,
            count: model.count(in: .favorites)) { model.sidebar = .favorites }
        row("Récents", icon: "clock", selected: model.sidebar == .recents,
            count: model.count(in: .recents)) { model.sidebar = .recents }
        if !model.categories.isEmpty {
            caption("CATÉGORIES")
            ForEach(model.orderedCategories) { category in
                let item = SidebarItem.category(category.id)
                row(model.name(of: category), icon: nil, selected: model.sidebar == item,
                    count: model.count(in: item)) { model.sidebar = item }
                    .contextMenu { renameButton(category, section: "live") }
            }
        }
    }

    @ViewBuilder
    private func catalogFilters(_ kind: CatalogEntry.Kind) -> some View {
        let section = model.catalog(kind)
        row(kind == .movie ? "Tous les films" : "Toutes les séries", icon: "square.grid.2x2",
            selected: section.filter == .all, count: model.catalogCount(in: .all, kind: kind)) {
            model.setFilter(.all, for: kind)
        }
        row("Ma liste", icon: "bookmark", selected: section.filter == .favorites,
            count: model.catalogCount(in: .favorites, kind: kind)) {
            model.setFilter(.favorites, for: kind)
        }
        row("Continuer à regarder", icon: "play.circle", selected: section.filter == .recents,
            count: model.catalogCount(in: .recents, kind: kind)) {
            model.setFilter(.recents, for: kind)
        }
        if !section.categories.isEmpty {
            caption("CATÉGORIES")
            ForEach(model.orderedCategories(kind)) { category in
                let item = SidebarItem.category(category.id)
                row(model.name(of: category, in: kind.rawValue), icon: nil, selected: section.filter == item,
                    count: model.catalogCount(in: item, kind: kind)) {
                    model.setFilter(item, for: kind)
                }
                .contextMenu { renameButton(category, section: kind.rawValue) }
            }
        }
    }

    private func caption(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(Theme.faint)
            .padding(.horizontal, 10)
            .padding(.top, 14)
            .padding(.bottom, 4)
    }

    private func renameButton(_ category: ChannelCategory, section: String) -> some View {
        Button("Renommer…") {
            renameText = model.name(of: category, in: section)
            renaming = (category, section)
        }
    }

    private func row(_ title: String, icon: String?, selected: Bool, count: Int, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if let icon {
                    Image(systemName: icon)
                        .frame(width: 16)
                        .foregroundStyle(selected ? Theme.accent : Theme.muted)
                }
                Text(title).lineLimit(1)
                Spacer(minLength: 4)
                Text("\(count)")
                    .font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(Theme.faint)
            }
            .font(.system(size: 12.5, weight: selected ? .semibold : .regular))
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .contentShape(Rectangle())
            .background(selected ? Theme.violet.opacity(0.14) : .clear, in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }
}

/// Temporary until the guide grid lands.
struct GuideScreen: View {
    var body: some View {
        ContentUnavailableView("Guide TV", systemImage: "calendar")
            .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                SortMenu()
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
