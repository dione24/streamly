import StreamlyKit
import SwiftUI

struct PosterImage: View {
    let url: String?
    let title: String
    var cornerRadius: CGFloat = 10

    var body: some View {
        ZStack {
            LinearGradient(colors: [Theme.surface3, Theme.surface2], startPoint: .top, endPoint: .bottom)
            Text(title.prefix(1).uppercased())
                .font(.system(size: 34, weight: .bold))
                .foregroundStyle(Theme.faint)
            if let url, let parsed = URL(string: url) {
                AsyncImage(url: parsed) { phase in
                    if case .success(let image) = phase {
                        image.resizable().scaledToFill()
                    }
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
    }
}

/// Thin violet bar along the bottom of a poster or a still.
struct WatchBar: View {
    let fraction: Double

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Rectangle().fill(.black.opacity(0.55))
                Rectangle().fill(Theme.violet).frame(width: proxy.size.width * fraction)
            }
        }
        .frame(height: 3)
    }
}

struct PosterCard: View {
    let entry: CatalogEntry
    let progress: WatchProgress?
    let isSaved: Bool
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            PosterImage(url: entry.poster, title: entry.title)
                .aspectRatio(2 / 3, contentMode: .fit)
                .overlay(alignment: .bottom) {
                    if let progress, !progress.isFinished, progress.fraction > 0 {
                        WatchBar(fraction: progress.fraction)
                            .clipShape(UnevenRoundedRectangle(bottomLeadingRadius: 10, bottomTrailingRadius: 10))
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if progress?.isFinished == true {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 16))
                            .foregroundStyle(.white, Theme.violet)
                            .padding(6)
                    } else if isSaved {
                        Image(systemName: "bookmark.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.accent)
                            .padding(7)
                    }
                }
                .overlay {
                    RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(hovering ? Theme.accent : .clear, lineWidth: 2)
                }
                .scaleEffect(hovering ? 1.025 : 1)
                .animation(.easeOut(duration: 0.12), value: hovering)
            Text(entry.title)
                .font(.system(size: 12, weight: .medium))
                .lineLimit(2, reservesSpace: false)
            if let rating = entry.rating {
                Label(String(format: "%.1f", rating), systemImage: "star.fill")
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(Theme.muted)
                    .labelStyle(.titleAndIcon)
            }
        }
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

struct CatalogView: View {
    @Environment(AppModel.self) private var model
    let kind: CatalogEntry.Kind

    private var section: CatalogSection { model.catalog(kind) }

    var body: some View {
        Group {
            if let entry = section.opened {
                CatalogDetailView(entry: entry, kind: kind)
                    .id(entry.id)
            } else {
                CatalogGrid(kind: kind)
            }
        }
        .background(Theme.background)
    }
}

private struct CatalogGrid: View {
    @Environment(AppModel.self) private var model
    let kind: CatalogEntry.Kind
    /// A category can hold thousands of titles: the grid grows as the viewer reaches its end.
    @State private var limit = 120

    private var section: CatalogSection { model.catalog(kind) }
    private let columns = [GridItem(.adaptive(minimum: 138, maximum: 190), spacing: 16, alignment: .top)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                let resuming = model.continueWatching(kind)
                if section.filter == .all, section.query.isEmpty, !resuming.isEmpty {
                    ContinueRow(items: resuming, title: "Continuer à regarder")
                }
                if section.visible.isEmpty {
                    ContentUnavailableView(emptyTitle, systemImage: kind == .movie ? "film" : "play.rectangle.on.rectangle",
                                           description: Text(emptyDetail))
                        .foregroundStyle(Theme.muted)
                        .frame(maxWidth: .infinity, minHeight: 320)
                } else {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 20) {
                        ForEach(section.visible.prefix(limit)) { entry in
                            Button {
                                model.open(entry, kind: kind)
                            } label: {
                                PosterCard(entry: entry, progress: model.progress(for: entry), isSaved: model.isSaved(entry))
                            }
                            .buttonStyle(.plain)
                            .contextMenu { menu(for: entry) }
                            .onAppear {
                                if entry.id == section.visible.prefix(limit).last?.id, limit < section.visible.count {
                                    limit += 120
                                }
                            }
                        }
                    }
                }
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 24)
        }
        .onChange(of: section.filter) { _, _ in limit = 120 }
        .onChange(of: section.query) { _, _ in limit = 120 }
    }

    @ViewBuilder
    private func menu(for entry: CatalogEntry) -> some View {
        Button(model.isSaved(entry) ? "Retirer de Ma liste" : "Ajouter à Ma liste") {
            model.toggleSaved(entry)
        }
        if entry.kind == .movie {
            let watched = model.progress(for: entry)?.isFinished == true
            Button(watched ? "Marquer comme non vu" : "Marquer comme vu") {
                model.setWatched(!watched, entry: entry)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 24, weight: .heavy))
                    .lineLimit(1)
                Text(section.visible.count == 1 ? "1 titre" : "\(section.visible.count) titres")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.muted)
            }
            Spacer()
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.faint)
                TextField(kind == .movie ? "Rechercher un film" : "Rechercher une série", text: Binding(
                    get: { section.query },
                    set: { model.setCatalogQuery($0, for: kind) }))
                    .textFieldStyle(.plain)
            }
            .font(.system(size: 13))
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .frame(width: 240)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 9))
            SortMenu()
        }
    }

    private var title: String {
        switch section.filter {
        case .all: return kind == .movie ? "Films" : "Séries"
        case .favorites: return "Ma liste"
        case .recents: return "Continuer à regarder"
        case .category(let id):
            return section.categories.first { $0.id == id }.map { model.name(of: $0, in: kind.rawValue) } ?? ""
        }
    }

    private var emptyTitle: String {
        if !section.query.isEmpty { return "Aucun résultat" }
        switch section.filter {
        case .favorites: return "Votre liste est vide"
        case .recents: return "Rien à reprendre pour l'instant"
        default: return kind == .movie ? "Aucun film" : "Aucune série"
        }
    }

    private var emptyDetail: String {
        if !section.query.isEmpty { return "Essayez un autre mot." }
        if section.filter == .favorites { return "Clic droit sur une affiche pour l'ajouter." }
        if section.filter == .all { return "Cet abonnement n'en propose pas, ou le catalogue n'a pas encore été téléchargé." }
        return ""
    }
}

struct SortMenu: View {
    @Environment(AppModel.self) private var model

    var body: some View {
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
        .help("Trier les listes")
    }
}

/// Horizontal row of items to resume, with a still and how far along each one is.
struct ContinueRow: View {
    @Environment(AppModel.self) private var model
    let items: [WatchProgress]
    let title: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 15, weight: .semibold))
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 12) {
                    ForEach(items, id: \.key) { item in
                        Button {
                            Task { await model.resume(item) }
                        } label: {
                            VStack(alignment: .leading, spacing: 5) {
                                PosterImage(url: item.poster, title: item.title, cornerRadius: 9)
                                    .frame(width: 210, height: 118)
                                    .overlay(alignment: .bottom) {
                                        WatchBar(fraction: item.fraction)
                                            .clipShape(UnevenRoundedRectangle(bottomLeadingRadius: 9, bottomTrailingRadius: 9))
                                    }
                                    .overlay {
                                        Image(systemName: "play.circle.fill")
                                            .font(.system(size: 30))
                                            .foregroundStyle(.white.opacity(0.9), .black.opacity(0.35))
                                    }
                                Text(item.title)
                                    .font(.system(size: 12, weight: .medium))
                                    .lineLimit(1)
                                if let subtitle = item.subtitle {
                                    Text(subtitle)
                                        .font(.system(size: 11))
                                        .foregroundStyle(Theme.muted)
                                        .lineLimit(1)
                                }
                            }
                            .frame(width: 210, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

struct CatalogDetailView: View {
    @Environment(AppModel.self) private var model
    let entry: CatalogEntry
    let kind: CatalogEntry.Kind

    @State private var movie: MovieDetail?
    @State private var seriesDetail: SeriesDetail?
    @State private var season: Int?
    @State private var loading = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                hero
                if kind == .series { episodes }
            }
            .padding(.bottom, 28)
        }
        .overlay(alignment: .topLeading) {
            Button {
                model.open(nil, kind: kind)
            } label: {
                Label("Retour", systemImage: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(.black.opacity(0.55), in: Capsule())
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.cancelAction)
            .padding(.leading, 18)
            .padding(.top, 4)
        }
        .task(id: entry.id) {
            loading = true
            if kind == .movie {
                movie = await model.movieDetail(entry)
            } else {
                seriesDetail = await model.seriesDetail(entry)
                season = seasonToOpen
            }
            loading = false
        }
    }

    private var backdrop: String? {
        (kind == .movie ? movie?.backdrop : seriesDetail?.backdrop) ?? entry.poster
    }

    private var hero: some View {
        ZStack(alignment: .bottomLeading) {
            GeometryReader { proxy in
                PosterImage(url: backdrop, title: "", cornerRadius: 0)
                    .frame(width: proxy.size.width, height: proxy.size.height)
                    .clipped()
                    .blur(radius: backdrop == entry.poster ? 28 : 0)
                    .opacity(0.55)
            }
            LinearGradient(colors: [.clear, Theme.background], startPoint: .top, endPoint: .bottom)

            HStack(alignment: .bottom, spacing: 20) {
                PosterImage(url: entry.poster, title: entry.title)
                    .frame(width: 170, height: 255)
                    .shadow(color: .black.opacity(0.5), radius: 16, y: 8)
                VStack(alignment: .leading, spacing: 10) {
                    Text(entry.title)
                        .font(.system(size: 30, weight: .heavy))
                        .lineLimit(2)
                    if !metadata.isEmpty {
                        Text(metadata)
                            .font(.system(size: 12).monospacedDigit())
                            .foregroundStyle(Theme.muted)
                    }
                    actions
                    if let plot, !plot.isEmpty {
                        Text(plot)
                            .font(.system(size: 13))
                            .foregroundStyle(Theme.text.opacity(0.85))
                            .lineLimit(5)
                            .frame(maxWidth: 620, alignment: .leading)
                    } else if loading {
                        ProgressView().controlSize(.small)
                    }
                    if let people, !people.isEmpty {
                        Text(people)
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.muted)
                            .lineLimit(2)
                            .frame(maxWidth: 620, alignment: .leading)
                    }
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 8)
        }
        .frame(height: 400)
    }

    private var metadata: String {
        var parts: [String] = []
        if let rating = (kind == .movie ? movie?.rating : nil) ?? entry.rating { parts.append(String(format: "★ %.1f", rating)) }
        if let year = kind == .movie ? movie?.year : seriesDetail?.year { parts.append(year) }
        if let seconds = movie?.durationSeconds { parts.append(Duration.label(seconds: seconds)) }
        if let genre = kind == .movie ? movie?.genre : seriesDetail?.genre { parts.append(genre) }
        if let count = seriesDetail?.seasons.count, count > 0 { parts.append(count == 1 ? "1 saison" : "\(count) saisons") }
        return parts.joined(separator: "  ·  ")
    }

    private var plot: String? {
        kind == .movie ? movie?.plot : (seriesDetail?.plot ?? entry.series?.plot)
    }

    private var people: String? {
        var parts: [String] = []
        if let director = movie?.director { parts.append("Réalisation : \(director)") }
        if let cast = kind == .movie ? movie?.cast : seriesDetail?.cast { parts.append("Avec : \(cast)") }
        return parts.isEmpty ? nil : parts.joined(separator: "   ")
    }

    private var resumeEpisode: Episode? {
        guard let detail = seriesDetail else { return nil }
        let latest = detail.episodes
            .compactMap { episode in model.progress(for: episode).map { (episode, $0) } }
            .max { $0.1.updatedAt < $1.1.updatedAt }
        guard let latest else { return detail.episodes.first }
        return latest.1.isFinished ? (detail.episode(after: latest.0) ?? latest.0) : latest.0
    }

    private var seasonToOpen: Int? {
        resumeEpisode?.season ?? seriesDetail?.seasons.first
    }

    @ViewBuilder
    private var actions: some View {
        HStack(spacing: 10) {
            if kind == .movie {
                let resume = model.progress(for: entry)?.resumePosition
                Button {
                    model.play(movie: entry, detail: movie)
                } label: {
                    Label(resume.map { "Reprendre à \(Duration.clock($0))" } ?? "Lecture", systemImage: "play.fill")
                }
                .buttonStyle(PrimaryButtonStyle())
                if resume != nil {
                    Button("Depuis le début") { model.play(movie: entry, detail: movie, fromStart: true) }
                        .buttonStyle(SecondaryButtonStyle())
                }
            } else if let episode = resumeEpisode {
                let started = model.progress(for: episode)?.resumePosition != nil
                Button {
                    model.play(episode: episode, of: entry)
                } label: {
                    Label(started ? "Reprendre \(episode.label)" : "Lecture \(episode.label)", systemImage: "play.fill")
                }
                .buttonStyle(PrimaryButtonStyle())
            }
            Button {
                model.toggleSaved(entry)
            } label: {
                Label(model.isSaved(entry) ? "Dans Ma liste" : "Ma liste",
                      systemImage: model.isSaved(entry) ? "bookmark.fill" : "bookmark")
            }
            .buttonStyle(SecondaryButtonStyle())
            if kind == .movie {
                let watched = model.progress(for: entry)?.isFinished == true
                Button {
                    model.setWatched(!watched, entry: entry)
                } label: {
                    Label(watched ? "Vu" : "Marquer comme vu",
                          systemImage: watched ? "checkmark.circle.fill" : "checkmark.circle")
                }
                .buttonStyle(SecondaryButtonStyle())
            }
        }
    }

    @ViewBuilder
    private var episodes: some View {
        if let detail = seriesDetail, !detail.episodes.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                if detail.seasons.count > 1 {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(detail.seasons, id: \.self) { number in
                                Button {
                                    season = number
                                } label: {
                                    Text("Saison \(number)")
                                        .font(.system(size: 12, weight: season == number ? .semibold : .regular))
                                        .padding(.horizontal, 11)
                                        .padding(.vertical, 6)
                                        .background(season == number ? Theme.violet.opacity(0.25) : Theme.surface2,
                                                    in: Capsule())
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                }
                LazyVStack(spacing: 6) {
                    ForEach(detail.episodes(inSeason: season ?? detail.seasons.first ?? 0)) { episode in
                        EpisodeRow(entry: entry, episode: episode)
                    }
                }
            }
            .padding(.horizontal, 24)
        } else if !loading {
            Text("Aucun épisode disponible.")
                .font(.system(size: 13))
                .foregroundStyle(Theme.muted)
                .padding(.horizontal, 24)
        }
    }
}

private struct EpisodeRow: View {
    @Environment(AppModel.self) private var model
    let entry: CatalogEntry
    let episode: Episode
    @State private var hovering = false

    var body: some View {
        let progress = model.progress(for: episode)
        Button {
            model.play(episode: episode, of: entry)
        } label: {
            HStack(spacing: 12) {
                PosterImage(url: episode.still ?? entry.poster, title: entry.title, cornerRadius: 7)
                    .frame(width: 128, height: 72)
                    .overlay(alignment: .bottom) {
                        if let progress, !progress.isFinished, progress.fraction > 0 {
                            WatchBar(fraction: progress.fraction)
                                .clipShape(UnevenRoundedRectangle(bottomLeadingRadius: 7, bottomTrailingRadius: 7))
                        }
                    }
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text("\(episode.number).").foregroundStyle(Theme.muted)
                        Text(episode.title).lineLimit(1)
                        if progress?.isFinished == true {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(.white, Theme.violet)
                        }
                    }
                    .font(.system(size: 13, weight: .medium))
                    if let plot = episode.plot {
                        Text(plot)
                            .font(.system(size: 11))
                            .foregroundStyle(Theme.muted)
                            .lineLimit(2)
                    }
                }
                Spacer(minLength: 6)
                if let seconds = episode.durationSeconds {
                    Text(Duration.label(seconds: seconds))
                        .font(.system(size: 11).monospacedDigit())
                        .foregroundStyle(Theme.muted)
                }
            }
            .padding(6)
            .contentShape(Rectangle())
            .background(hovering ? Theme.surface2 : .clear, in: RoundedRectangle(cornerRadius: 9))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .contextMenu {
            let watched = progress?.isFinished == true
            Button(watched ? "Marquer comme non vu" : "Marquer comme vu") {
                model.setWatched(!watched, episode: episode, of: entry)
            }
        }
    }
}

struct SearchView: View {
    @Environment(AppModel.self) private var model
    @FocusState private var focused: Bool

    var body: some View {
        @Bindable var bindable = model
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 9) {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.faint)
                TextField("Chaînes, films, séries", text: $bindable.globalQuery)
                    .textFieldStyle(.plain)
                    .font(.system(size: 18))
                    .focused($focused)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 12))
            .frame(maxWidth: 640)

            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    let results = model.globalResults
                    if model.globalQuery.trimmingCharacters(in: .whitespaces).isEmpty {
                        ContentUnavailableView("Que voulez-vous regarder ?", systemImage: "magnifyingglass",
                                               description: Text("La recherche ignore les accents et les majuscules."))
                            .foregroundStyle(Theme.muted)
                            .frame(maxWidth: .infinity, minHeight: 320)
                    } else if results.isEmpty {
                        ContentUnavailableView("Aucun résultat", systemImage: "magnifyingglass",
                                               description: Text("Essayez un autre mot."))
                            .foregroundStyle(Theme.muted)
                            .frame(maxWidth: .infinity, minHeight: 320)
                    } else {
                        if !results.channels.isEmpty { channels(results.channels) }
                        if !results.movies.isEmpty { posters("Films", results.movies, kind: .movie) }
                        if !results.series.isEmpty { posters("Séries", results.series, kind: .series) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 22)
        .padding(.top, 8)
        .background(Theme.background)
        .onAppear { focused = true }
    }

    private func channels(_ list: [Channel]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Chaînes").font(.system(size: 15, weight: .semibold))
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 230), spacing: 8, alignment: .leading)], spacing: 8) {
                ForEach(list) { channel in
                    Button {
                        model.section = .direct
                        model.play(channel)
                    } label: {
                        HStack(spacing: 9) {
                            ChannelLogo(channel: channel)
                            Text(channel.name)
                                .font(.system(size: 13, weight: .medium))
                                .lineLimit(1)
                            Spacer(minLength: 4)
                            if let lang = channel.lang { QualityBadge(text: lang) }
                        }
                        .padding(7)
                        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 9))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func posters(_ title: String, _ list: [CatalogEntry], kind: CatalogEntry.Kind) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 15, weight: .semibold))
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 14) {
                    ForEach(list) { entry in
                        Button {
                            model.section = kind == .movie ? .movies : .series
                            model.open(entry, kind: kind)
                        } label: {
                            PosterCard(entry: entry, progress: model.progress(for: entry), isSaved: model.isSaved(entry))
                                .frame(width: 128)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }
}
