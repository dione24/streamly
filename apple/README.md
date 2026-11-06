# Streamly pour macOS

Lecteur IPTV natif en Swift. L'utilisateur apporte son abonnement (compte Xtream ou lien M3U) ; l'app ne fournit aucun contenu et lit directement chez le fournisseur, sans moteur Streamly.

## Lancer l'app

Il faut Xcode et [XcodeGen](https://github.com/yonaskolb/XcodeGen).

```sh
cd apple
xcodegen generate
open Streamly.xcodeproj    # puis ⌘R
```

Le premier build télécharge MPVKit (environ 1,7 Go), ce qui prend plusieurs minutes.

## Essayer sans abonnement

Un faux panel sert des mires générées par FFmpeg. Il reproduit ce qui piège un lecteur : une seule connexion à la fois, un refus pendant 1,5 s après chaque fermeture, du MPEG-TS brut et une variante en panne.

```sh
python3 apple/tools/fake_panel.py
```

Dans l'app : compte Xtream, adresse `http://127.0.0.1:8765`, identifiant `demo`, mot de passe `demo`.

## Vérifier

```sh
cd apple
swift test --filter StreamlyKitTests                # logique, sans réseau
swift run streamly-smoke "<adresse du flux>" 5      # décode sans fenêtre, code retour 0 si la lecture avance
```

Parcours complet dans la vraie fenêtre, avec le faux panel lancé (version Debug uniquement). L'app utilise le dossier indiqué et des secrets en mémoire, joue le scénario son coupé, envoie de vrais clics et touches, écrit `report.txt` avec des images, puis se ferme. Elle passe en plein écran quelques secondes.

```sh
STREAMLY_SELFTEST_DIR=/tmp/streamly-test build/Build/Products/Debug/Streamly.app/Contents/MacOS/Streamly
```

## Organisation

| Dossier | Rôle |
|---|---|
| `Sources/StreamlyKit` | Noms de chaînes, regroupement, Xtream, M3U, guide court, plan de reprise, trousseau. Aucune interface : réutilisable sur iOS et tvOS. |
| `Sources/StreamlyPlayer` | libmpv (via MPVKit, version LGPL) et la surface vidéo. |
| `Sources/streamly-smoke` | Vérification de lecture en ligne de commande. |
| `App` | Interface SwiftUI et pilotage de la lecture. |
| `tools/fake_panel.py` | Faux panel de développement. |

`ChannelName` et `ChannelGrouper` reprennent `parse_name`, `quality_height`, `is_backup` de `server/streamly/xtream.py`. Une correction d'un côté doit être reportée de l'autre.

## Pourquoi libmpv plutôt qu'AVPlayer

Les directs IPTV sont souvent du MPEG-TS brut sur HTTP, qu'AVPlayer ne lit pas, et beaucoup de films sont en MKV. libmpv lit ces formats ainsi que HEVC, AC3 et MP2.

La version LGPL impose de distribuer libmpv comme bibliothèque remplaçable : ne pas dépendre du produit `MPVKit-GPL`.

## Règles à garder

- **Jamais d'appel bloquant à mpv depuis le fil principal :** pendant que mpv recrée sa sortie vidéo, MoltenVK attend le fil principal et le cœur de mpv attend la sortie vidéo. Une lecture de propriété sur le fil principal à ce moment fige l'app définitivement. Tout passe par la file `control` de `MPVPlayer`.
- **Une seule connexion :** jamais de préchargement ni de deuxième flux. Le changement rapide de chaîne attend 350 ms avant d'ouvrir le flux.
- **Secrets :** le mot de passe ou le lien M3U va au trousseau. Les adresses Xtream sont reconstruites à la lecture, jamais mises en cache. Un lien M3U ordinaire peut contenir des identifiants dans ses entrées : le cache est donc lisible par le seul propriétaire. Aucune adresse dans les messages d'erreur (`Redact`).

## Pas encore fait

- Films et séries, grille du guide, contrôle parental, profils.
- Image dans l'image : mpv n'y a pas accès, il faudra une petite fenêtre flottante maison. AirPlay vidéo : pas possible avec mpv, seul le son suit la sortie choisie dans macOS.
- Économie de données : association au moteur et relais (`docs/api-moteur.md`).
- Icône, signature Developer ID et notarisation pour distribuer l'app.
- Cache en base SQLite : le bouquet est gardé en mémoire, ce qui passe sur Mac mais pas sur iOS (`docs/app-v1-spec.md`, § 12).
