# Streamly produit — modèle hybride

| Champ | Valeur |
|---|---|
| **Statut** | Cadrage, décidé le 2026-09-19 |
| **Suite de** | `docs/facade-lecteur.md` (façade Xtream/M3U, en bêta avec des amis) |

## Le produit en une phrase

Un lecteur IPTV du niveau de TiviMate ou d'IPTV Smarters, qui marche seul en lecture directe, et dont la fonction payante « Économie de données » fait passer le direct par un moteur Streamly qui le compresse et l'adapte à la connexion.

## Ce qui change par rapport à aujourd'hui

Aujourd'hui **le moteur détient l'abonnement** : on l'entre dans Réglages, il synchronise le catalogue, et les lecteurs le consultent.

Dans le modèle hybride **l'app détient la playlist**, comme TiviMate : l'utilisateur y entre son Xtream ou son M3U, l'app lit en direct sans aucun serveur. Le moteur n'intervient que lorsque l'économie de données est active ; l'app lui dit alors « compresse-moi cette source ».

| | Lecture directe (gratuit) | Économie de données (premium) |
|---|---|---|
| Qui parle au fournisseur | l'app | le moteur |
| Données consommées | 2,5 à 4 Go/h (source) | 0,1 à 0,7 Go/h selon le mode |
| Coût pour nous | aucun | ~0,6 cœur par lecture (480p) |
| Marche sans moteur | oui | non |

Les deux usages du moteur cohabitent : le mode « catalogue » actuel reste (web, façade Xtream), le mode « relais » s'ajoute pour l'app.

## Architecture

```
App (Android / iOS / TV)
 ├─ playlist Xtream ou M3U, favoris, guide, réglages   → stockés dans l'app
 ├─ lecture directe  ───────────────────────────────→  fournisseur IPTV
 └─ économie de données
      POST /api/relay {source, mode | budget}  ──→  Moteur Streamly ──→ fournisseur
      ←── HLS adaptatif /s/{ticket}/master.m3u8         (auto-hébergé ou cloud)
```

### Contrat app ↔ moteur (en place depuis le 2026-09-19)

- `POST /api/pair-code` (admin, depuis Réglages) → code de 8 caractères, 10 min, usage unique. `POST /api/pair {code, name}` → `{id, name, token}`. L'app envoie ensuite `Authorization: Bearer <token>` ; le moteur n'en garde que l'empreinte. Un appareil n'a accès qu'à `/api/me`, `/api/relay`, `/api/playback`, `/api/stop`, et se retire depuis Réglages.
- `POST /api/relay` : `{source_url, label, mode}` ou `{…, budget_mb, minutes}` → `{play_url, ticket}`. Réutilise `Transcoder.open` tel quel : l'identité du worker devient l'empreinte de la source.
- `GET /api/playback?ticket=` (existe) : état, génération, octets consommés — c'est ce qui permet à l'app d'afficher le compteur du mode Budget, que les lecteurs tiers ne peuvent pas offrir.
- `POST /api/stop` (existe).

Sécurité du relais — un relais ouvert est une faille : jeton d'appareil obligatoire, schémas `http`/`https` seulement, refus des adresses privées et locales (pas de rebond vers le réseau interne du serveur), une lecture par appareil, quota par compte sur le cloud.

### Où tourne le moteur

1. **Auto-hébergé** (modèle Plex) : Mac, mini-PC, NAS ou VPS de l'utilisateur. Aucun coût pour nous, l'utilisateur reste maître de ses flux. À faire : installation en une commande (Docker, app Mac de barre de menus), accès depuis l'extérieur (tunnel).
2. **Cloud Streamly** (abonnement) : pour ceux qui ne veulent rien installer. Coûts serveur et exposition juridique plus forts (nous faisons transiter les flux) ; à ouvrir seulement après validation du reste, avec encodage matériel pour la rentabilité.

Le même contrat sert les deux : pour l'app, un moteur est une adresse et un jeton.

## L'app : ce qu'il faut pour rivaliser

Socle (parité avec TiviMate / Smarters) : plusieurs playlists Xtream et M3U, catégories, recherche, favoris, guide des programmes, zapping rapide, reprise de la dernière chaîne, télécommande et grand écran, films et séries en lecture directe, contrôle parental.

Différence Streamly : bascule « Économie de données » (automatique en données mobiles), modes Économie / Équilibré / Sport, **mode Budget avec compteur** (« 800 Mo pour ce match »), estimation des Go économisés.

## Feuille de route

| Étape | Contenu | Dépend de |
|---|---|---|
| 0. Bêta amis (en cours) | Mesurer charge réelle, pannes, usages sur les 4 instances | — |
| 1. Relais moteur — **fait** | `/api/pair`, `/api/relay`, garde-fous, tests ; validé en local sur un flux public (360p + 240p, compteur d'octets) | — |
| 2. Moteur installable | Image Docker, service, doc d'installation, accès distant | 1 |
| 3. App v0 | Playlist, liste, lecture directe + bascule relais, sur une plateforme | choix de plateforme, 1 |
| 4. App v1 | Guide, favoris, recherche, mode Budget, TV | 3 |
| 5. Premium | Achat intégré, cloud optionnel, encodage matériel | 4 |

## Décisions prises (2026-09-19)

- **Plateforme** : base commune **Flutter** (Android et iOS, Android TV ensuite), lecteur vidéo natif de chaque système en dessous.
- **Premium de départ** : moteur **auto-hébergé**. Le cloud payant viendra après validation.

## Décisions ouvertes

- Nom commercial, prix, domaine (remplacer `sslip.io`).
