import StreamlyKit
import SwiftUI

struct AddPlaylistView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let isWelcome: Bool

    private enum Source: Hashable { case xtream, m3u }

    @State private var source: Source = .xtream
    @State private var name = ""
    @State private var host = ""
    @State private var username = ""
    @State private var password = ""
    @State private var link = ""

    private var canSubmit: Bool {
        if case .running = model.sync { return false }
        switch source {
        case .xtream:
            return !host.trimmingCharacters(in: .whitespaces).isEmpty && !username.isEmpty && !password.isEmpty
        case .m3u:
            return !link.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text(isWelcome ? "Bienvenue sur Streamly" : "Ajouter un abonnement")
                    .font(.system(size: 26, weight: .heavy))
                Text("Entrez votre abonnement IPTV. Streamly ne fournit aucune chaîne ; le mot de passe reste dans le trousseau de ce Mac.")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Picker("", selection: $source) {
                Text("Compte Xtream").tag(Source.xtream)
                Text("Lien M3U").tag(Source.m3u)
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            VStack(spacing: 10) {
                field("Nom (facultatif)", text: $name)
                if source == .xtream {
                    field("Adresse du serveur, ex. http://serveur.exemple:8080", text: $host)
                    field("Identifiant", text: $username)
                    SecureField("Mot de passe", text: $password)
                        .modifier(FieldStyle())
                } else {
                    field("Lien de la playlist", text: $link)
                }
            }

            status

            HStack {
                if !isWelcome {
                    Button("Annuler") {
                        model.clearSyncError()
                        dismiss()
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.muted)
                }
                Spacer()
                Button("Ajouter") { submit() }
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(!canSubmit)
                    .opacity(canSubmit ? 1 : 0.45)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(maxWidth: 480)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var status: some View {
        switch model.sync {
        case .running(let text):
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(text).font(.system(size: 13)).foregroundStyle(Theme.muted)
            }
        case .failed(let text):
            Text(text)
                .font(.system(size: 13))
                .foregroundStyle(Theme.danger)
                .fixedSize(horizontal: false, vertical: true)
        case .idle:
            EmptyView()
        }
    }

    private func field(_ title: String, text: Binding<String>) -> some View {
        TextField(title, text: text)
            .autocorrectionDisabled()
            .modifier(FieldStyle())
    }

    private func submit() {
        let input: PlaylistInput = source == .xtream
            ? .xtream(name: name, host: host, username: username, password: password)
            : .m3u(name: name, link: link)
        Task {
            if await model.add(input), !isWelcome { dismiss() }
        }
    }
}

private struct FieldStyle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .textFieldStyle(.plain)
            .font(.system(size: 13))
            .padding(10)
            .background(Theme.surface2, in: RoundedRectangle(cornerRadius: 9))
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var pendingRemoval: Playlist?

    private let qualities: [(label: String, height: Int?)] = [
        ("La meilleure disponible", nil), ("1080p", 1080), ("720p", 720), ("480p", 480),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("Réglages").font(.system(size: 22, weight: .heavy))
                Spacer()
                Button("Fermer") {
                    model.clearSyncError()
                    dismiss()
                }
                .buttonStyle(PrimaryButtonStyle())
                .keyboardShortcut(.cancelAction)
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("Abonnements").font(.system(size: 14, weight: .semibold))
                    Spacer()
                    Button("Ajouter…") {
                        dismiss()
                        model.showingAddPlaylist = true
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.accent)
                }
                ForEach(model.playlists) { playlist in
                    playlistRow(playlist)
                }
                switch model.sync {
                case .running(let text):
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(text).font(.system(size: 12)).foregroundStyle(Theme.muted)
                    }
                case .failed(let text):
                    Text(text).font(.system(size: 12)).foregroundStyle(Theme.danger)
                case .idle:
                    EmptyView()
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Qualité en direct").font(.system(size: 14, weight: .semibold))
                Picker("", selection: Binding(
                    get: { model.preferences.maxHeight },
                    set: { model.setMaxHeight($0) })) {
                    ForEach(qualities, id: \.label) { quality in
                        Text(quality.label).tag(quality.height)
                    }
                }
                .labelsHidden()
                .frame(maxWidth: 240)
                Text("Si la qualité choisie n'existe pas ou ne répond pas, Streamly essaie les autres.")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.muted)
            }

            Spacer()
        }
        .padding(24)
        .frame(width: 560, height: 480)
        .background(Theme.background)
        .confirmationDialog(
            "Supprimer « \(pendingRemoval?.name ?? "") » ?",
            isPresented: Binding(get: { pendingRemoval != nil }, set: { if !$0 { pendingRemoval = nil } }),
            titleVisibility: .visible
        ) {
            Button("Supprimer", role: .destructive) {
                if let pendingRemoval { model.remove(pendingRemoval) }
                pendingRemoval = nil
            }
        } message: {
            Text("Ses favoris et son mot de passe enregistré seront effacés de ce Mac.")
        }
    }

    private func playlistRow(_ playlist: Playlist) -> some View {
        HStack(spacing: 10) {
            Image(systemName: model.active?.id == playlist.id ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(model.active?.id == playlist.id ? Theme.accent : Theme.faint)
                .onTapGesture { model.activate(playlist) }
            VStack(alignment: .leading, spacing: 2) {
                Text(playlist.name).font(.system(size: 13, weight: .medium))
                Text(summary(of: playlist))
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.muted)
            }
            Spacer()
            Button("Actualiser") {
                Task { await model.resync(playlist) }
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.accent)
            .font(.system(size: 12))
            Button {
                pendingRemoval = playlist
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.muted)
            .help("Supprimer cet abonnement")
        }
        .padding(10)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
    }

    private func summary(of playlist: Playlist) -> String {
        var parts: [String] = []
        switch playlist.kind {
        case .xtream(_, let username): parts.append("Xtream · \(username)")
        case .m3u: parts.append("Lien M3U")
        }
        if let max = playlist.account?.maxConnections {
            parts.append(max == 1 ? "1 connexion" : "\(max) connexions")
        }
        if let expiry = playlist.account?.expiresAt {
            parts.append("expire le \(Format.date(expiry))")
        }
        return parts.joined(separator: " · ")
    }
}
