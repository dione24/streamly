# Streamly

**Votre IPTV, compressée et adaptée à votre connexion.**

Streamly est un serveur auto-hébergé qui se place entre votre abonnement IPTV
(Xtream Codes ou M3U) et vos écrans. Il recompresse le direct à la demande en
HLS multi-débits : le lecteur choisit la qualité que la connexion supporte, et
un match qui pesait 3 Go/h n'en pèse plus que 0,2 à 0,7.

Il a été pensé pour les connexions irrégulières et les forfaits facturés au
volume. Vous apportez votre abonnement ; Streamly ne fournit, ne référence et ne
préconfigure aucun contenu.

```
abonnement IPTV ──▶ Streamly (FFmpeg, à la demande) ──▶ interface web
   2,5–4 Go/h         720p · 480p · 360p · 240p      ├─▶ TiviMate, Smarters, VLC…
                                                     └─▶ application (API de relais)
```

## Ce que fait Streamly

**Compression adaptative**
- Échelle 720p / 480p / 360p / 240p en H.264 + AAC, segments de 2 s, qualité
  constante plafonnée (CRF) : un plateau de JT consomme moins qu'un match.
- Seuls les barreaux que le mode autorise sont encodés : le 720p, qui pèse près
  de la moitié du processeur, n'est pas calculé pour un spectateur en Économie.
- Modes **Économie** (360p max), **Équilibré** (480p), **Sport** (720p),
  **Audio seul**, et **Budget** : « 800 Mo pour ce match de 2 h », avec compteur
  et arrêt à l'épuisement.
- Une source déjà plus légère que le barreau visé est recopiée sans réencodage.
- Bascule automatique sur une source de secours quand un flux se fige ; une même
  chaîne regardée sur plusieurs écrans ne s'encode qu'une fois.

**Interface web façon lecteur TV**
- Rail de navigation, catégories, chaînes numérotées avec le programme en cours,
  lecteur toujours visible avec « maintenant / ensuite » ; grilles d'affiches
  pour les films et les séries ; pilotable au clavier et à la télécommande.
- Écrans explicites plutôt qu'une image figée : préparation (avec le débit
  mesuré de votre connexion), reprise du direct, connexion trop lente pour la
  qualité en cours, connexion interrompue, chaîne indisponible chez le
  fournisseur.
- Favoris, récents, recherche, PWA installable, mini-lecteur, plein écran.
- Films et épisodes préparés sur le serveur en version légère (choix de la
  qualité, de l'audio et des sous-titres texte), lisibles ou téléchargeables
  avec reprise.

**Vos lecteurs habituels**
- Streamly se présente comme un panel **Xtream Codes** et comme un lien **M3U** :
  TiviMate, IPTV Smarters, VLC… reçoivent le flux compressé et adaptatif, avec
  guide des programmes (XMLTV réduit et mis en cache, `get_short_epg`).
- L'encodage ne démarre qu'à l'ouverture réelle d'une chaîne : lister ou survoler
  le bouquet ne coûte rien, ni au serveur ni à l'abonnement.
- Pendant le démarrage de l'encodeur, le lecteur reçoit un court écran d'attente,
  puis le direct, sans rupture de numérotation. Détails : [`docs/facade-lecteur.md`](docs/facade-lecteur.md).

**API de relais pour une application**
- Une application qui garde la playlist sur l'appareil peut confier un flux à
  compresser : association par code à usage unique, jeton d'appareil, droits
  minimaux. Contrat : [`docs/api-moteur.md`](docs/api-moteur.md).

## Démarrage rapide

Prérequis : **Python 3.10+**, **FFmpeg** et **ffprobe**. Aucune dépendance Python
hors bibliothèque standard, aucune base de données externe (SQLite intégré).

```sh
git clone https://github.com/dione24/streamly.git
cd streamly/server
python3 run.py
```

Au premier lancement, `config.json` est créé et un **jeton administrateur**
s'affiche dans le terminal. Ouvrez `http://localhost:8088`, connectez-vous avec
ce jeton, puis dans **Réglages** : ajoutez votre abonnement et lancez la
synchronisation. Les identifiants pour vos lecteurs externes sont dans
**Réglages → Lecteur externe**.

Pour essayer l'interface sans abonnement : `python3 tests/preview.py`
(catalogue fictif, jeton `preview-only`, usage local uniquement).

## Installation sur un serveur

```sh
sudo ./deploy/install.sh              # /opt/streamly + service systemd
sudo ./deploy/https-site.sh tv.exemple.fr 8088   # Apache + certificat Let's Encrypt
```

- **HTTPS est indispensable dès que le serveur est joignable d'Internet** : les
  lecteurs externes envoient leurs identifiants dans l'URL. Derrière le proxy,
  mettez `listen_host` à `127.0.0.1` et `secure_cookies` à `true`.
- `https-site.sh` n'ajoute qu'un site Apache et vérifie la syntaxe avant de
  recharger : les sites existants ne sont pas touchés.
- Une mise à jour ne doit jamais remplacer `server/config.json` ni `server/data`.

**Plusieurs personnes sur une même machine** — chaque abonnement doit avoir son
instance : dans une même instance, deux abonnements se servent de secours l'un à
l'autre.

```sh
sudo ./deploy/instance.sh alice 8089   # /opt/streamly-alice, service streamly@alice
sudo ./deploy/https-site.sh alice.exemple.fr 8089
```

Les instances partagent une tranche de processeur bornée (systemd) et peuvent
être limitées en qualité avec `max_mode`.

**Dimensionnement.** Le coût suit les lectures simultanées, pas le nombre de
comptes : environ 1,15 cœur pour une chaîne en Sport (quatre barreaux depuis du
720p), moins en Équilibré et en Économie. Mesurez sur votre machine et vos
sources avant de promettre une capacité.

## Configuration

Tout est dans `server/config.json` (privé, `chmod 600`, exclu du dépôt) ;
[`server/config.example.json`](server/config.example.json) documente chaque clé.
Les principales :

| Clé | Rôle |
|---|---|
| `providers` | Abonnements Xtream ou M3U, avec `max_connections` (ne déclarez pas plus que ce que l'abonnement autorise). |
| `max_concurrent_streams` | Encodages simultanés, films en préparation compris. |
| `ladder`, `crf`, `x264_preset` | Échelle de qualités et réglage de l'encodeur. |
| `max_mode` | Borne de qualité de l'instance (`eco`, `balanced`, `sport`). |
| `players` | Comptes des lecteurs externes et leur mode. |
| `public_url`, `trust_proxy`, `secure_cookies`, `listen_host` | Publication derrière un proxy HTTPS. |
| `epg_refresh_hours` | Fréquence de reconstruction du guide des programmes. |
| `relay_allow_private` | Autorise l'application à faire compresser une source du réseau local (moteur domestique uniquement). |

## Sécurité

- Le jeton principal ouvre une session **administrateur** ; un jeton en lecture
  seule et des comptes `users` (mots de passe en PBKDF2, `deploy/adduser.py`)
  existent. Sessions par cookie HttpOnly / SameSite=Strict, sept jours.
- Les lecteurs externes ont des identifiants distincts du jeton administrateur ;
  les flux portent un ticket de lecture éphémère, jamais ce jeton.
- Dix tentatives manquées en cinq minutes bloquent une adresse ; derrière un
  proxy local, l'adresse réelle du client est prise en compte.
- Les journaux masquent tickets, mots de passe et identifiants d'abonnement.
  Aucune URL ni identifiant du fournisseur ne sort dans les playlists servies.
- Tout ce qui va chercher une adresse venue de l'extérieur (relais, logos)
  refuse les adresses privées et locales, redirections comprises.

Une faille ? Merci de la signaler en privé au mainteneur (onglet *Security* du
dépôt s'il est activé) plutôt que dans une issue publique.

## Développement

```sh
python3 -m unittest discover -s tests -v   # près de cent tests, sans réseau
python3 tests/media_smoke.py               # FFmpeg réel : échelle HLS et film préparé
node --check web/app.js
```

```
server/streamly/   app.py (HTTP, API)   transcoder.py (FFmpeg, tickets, bascules)
                   player.py (façade Xtream/M3U)   epg.py   relay.py   logos.py
                   catalog.py (SQLite)   vod.py (films)   xtream.py · m3u.py (sources)
web/               interface sans framework ni étape de build ; hls.js embarqué
deploy/            installation, instances, HTTPS
docs/              conception, contrat de l'API, mesures
tests/
```

Le code et ses commentaires sont en français. Les contributions sont
bienvenues ; les plus utiles aujourd'hui :

- essais avec de vrais lecteurs (TiviMate, Smarters, Apple TV) et retours précis ;
- encodage matériel (VAAPI, QSV, NVENC, VideoToolbox) ;
- image Docker ; segments fMP4 ; grille horaire complète du guide ;
- l'application mobile décrite dans [`docs/app-v1-spec.md`](docs/app-v1-spec.md)
  (modèle : [`docs/produit-hybride.md`](docs/produit-hybride.md)).

Toute modification du serveur vient avec son test ; ne promettez dans la
documentation que ce qui a été mesuré ([`docs/mesures.md`](docs/mesures.md)).

## Limites connues

- Démarrage d'une chaîne : 3 à 11 s selon la source (l'écran d'attente le couvre).
- Le relais et les lecteurs externes servent le direct ; films et séries passent
  par l'interface web.
- Le mode Budget n'existe que dans l'interface web et l'API : un lecteur tiers ne
  sait pas afficher un compteur.
- Certains fournisseurs refusent les connexions venant d'un centre de données.
- Passer par un serveur ne garantit pas de contourner un blocage opérateur.

## Cadre d'usage

Streamly est un outil personnel : il relaie **votre** abonnement vers **vos**
écrans. Vous êtes responsable du respect des conditions de votre fournisseur et
du droit applicable. Ne partagez pas un abonnement entre des personnes qui n'y
ont pas droit, et ne fusionnez jamais les abonnements de personnes différentes
dans une même instance.

## Licence

MIT, voir [LICENSE](LICENSE). hls.js (`web/vendor`) est distribué sous sa propre
licence Apache 2.0.
