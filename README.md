# Streamly

Lecteur IPTV auto-hébergé qui transcode les flux Xtream Codes en **HLS
multi-débits**, pour les rendre regardables sur des connexions lentes,
instables ou facturées au volume.

Le problème de départ : un flux IPTV est servi à **débit fixe**. Contrairement
à YouTube ou Netflix, aucun lecteur ne peut en réduire la qualité quand le
réseau faiblit — l'adaptation se décide à l'encodage, côté serveur. Streamly
remet cet encodeur entre vos mains.

## Ce que ça change, concrètement

Mesures réelles sur une chaîne sport 1080p (`ffmpeg`, preset `veryfast`) :

| Barreau | Débit | 1 h | Un match de 2 h |
|---|---|---|---|
| Source, sans Streamly | 6,81 Mbps | 3,07 Go | **6,13 Go** |
| 720p | 1,50 Mbps | 0,68 Go | 1,35 Go |
| 480p | 0,89 Mbps | 0,40 Go | **0,80 Go** |
| 360p | 0,55 Mbps | 0,25 Go | 0,49 Go |
| 240p | 0,25 Mbps | 0,11 Go | 0,22 Go |

Le lecteur bascule automatiquement entre ces barreaux selon la bande passante
réelle, et l'utilisateur peut aussi **forcer** un barreau bas pour économiser
son forfait.

Coût : **≈ 0,94 vCPU** par flux sur un Xeon 3,1 GHz. Un VPS 4 vCPU suffit.

## Fonctionnalités

- Transcodage ABR **à la demande** : rien ne tourne tant que personne ne regarde
- **Plusieurs providers** Xtream simultanément, ajoutés depuis l'interface
- Catalogue **synchronisé une fois** puis servi en local (un panel dépasse
  couramment 90 000 entrées et 60 Mo de JSON à chaque rechargement)
- **Reconstruction des catégories** quand le panel renvoie des listes vides
- Détection des **échelles de qualité** et des **flux de secours** d'une chaîne
- **Bascule automatique de source** : variantes de qualité, flux de secours du
  panel puis autres providers, essayés dans l'ordre quand un flux lâche
- **Films** : catalogue synchronisé, poids estimé affiché **avant** lecture,
  MP4 relayé tel quel et MKV remultiplexé à la volée pour le navigateur
- Filtre de langue **mémorisé**, recherche, favoris
- Interface web unique pour tous les appareils, `hls.js` là où c'est nécessaire
  et HLS natif sur Safari

## Installation

```bash
git clone https://github.com/dione24/streamly.git
cd streamly
sudo ./deploy/install.sh
```

Le script installe `ffmpeg`, copie le projet dans `/opt/streamly`, crée le
service systemd et affiche l'URL et le jeton d'accès.

### Lancement manuel

```bash
cd server
cp config.example.json config.json   # puis renseignez vos providers
python3 run.py
```

Aucune dépendance Python externe : bibliothèque standard uniquement
(Python 3.8+). Seul `ffmpeg` est requis.

## Configuration

Tout est dans `server/config.json`, créé au premier lancement depuis
`config.example.json`. **Ce fichier contient vos identifiants et n'est jamais
versionné.**

| Clé | Rôle |
|---|---|
| `token` | Jeton d'accès, généré automatiquement s'il est vide |
| `providers` | Liste des abonnements Xtream |
| `ladder` | Barreaux de l'échelle ABR |
| `preferred_source_height` | Définition de source à ingérer (720 par défaut) |
| `idle_timeout_seconds` | Extinction du flux après inactivité |

## Notes techniques

**Tous les barreaux sont ré-encodés**, y compris le plus haut. Recopier le flux
source (`-c:v copy`) ne coûte rien en CPU, mais produit des segments calqués
sur les keyframes de la source, donc irréguliers (mesuré : de 0,68 à 4,96 s).
Les frontières ne coïncident alors plus entre barreaux et la bascule ABR
hoquète — précisément quand le réseau faiblit. Avec `-g` forcé, les segments
font exactement 2,000 s. Bonus inattendu : c'est aussi **moins cher**
(0,94 vCPU contre 1,30), puisqu'on décode alors une source 720p et non 1080p.

**La source ingérée est choisie automatiquement.** Prendre le barreau 720p du
panel plutôt que son 1080p fait passer le coût de 2,77 à 0,94 vCPU, pour un
rendu final identique après transcodage.

**La playlist maîtresse est générée par Streamly**, pas par ffmpeg : ce dernier
annonce mal la bande passante des barreaux, ce qui fait choisir le mauvais
niveau au lecteur.

**Les films MKV ne sont pas lisibles en navigateur.** Plutôt que de les
transcoder, Streamly les remultiplexe en MP4 fragmenté : la vidéo est recopiée
(`-c:v copy`, coût nul) et seul l'audio est réencodé, les pistes E-AC3
fréquentes sur ces fichiers n'étant pas décodables par les navigateurs. Un MP4
est, lui, simplement relayé avec les requêtes Range, ce qui préserve le
déplacement natif dans la vidéo.

**Le poids d'un film est estimé, pas mesuré** : `bitrate x durée`. La taille
réelle demanderait une requête HEAD par film, et `get_vod_streams` ne fournit
ni le conteneur ni le débit — seul `get_vod_info` les donne, à raison d'un
appel par film. Streamly ne le fait donc qu'à l'ouverture d'une fiche, puis
conserve le résultat.

**Le jeton est placé dans le chemin** des URLs de lecture (`/s/<jeton>/...`) :
les playlists HLS référencent leurs segments en relatif, qui héritent donc du
jeton sans réécriture.

## Sécurité

Le serveur écoute en **HTTP simple**. Exposé à Internet, placez-le derrière un
reverse proxy avec TLS (Caddy ou nginx). Le jeton protège l'API et les flux,
mais ne chiffre rien.

`config.json` est écrit en `0600` et exclu du dépôt.

## Licence

MIT — voir [LICENSE](LICENSE).
