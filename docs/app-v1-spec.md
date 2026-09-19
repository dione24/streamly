# Application Streamly — spécification V1

| Champ | Valeur |
|---|---|
| **Statut** | Prêt à implémenter |
| **Pour** | L'agent ou le développeur qui construit l'app. Ce document se suffit à lui-même ; lire aussi `docs/api-moteur.md` (contrat du moteur) et `docs/produit-hybride.md` (pourquoi ce modèle). |
| **Emplacement du code** | Dossier `app/` à la racine de ce dépôt |

## 1. Le produit

Un lecteur IPTV du niveau de TiviMate ou d'IPTV Smarters Pro. L'utilisateur apporte son abonnement (compte Xtream Codes ou lien M3U) ; l'app ne fournit aucun contenu.

Ce qui le distingue : **l'économie de données**. Quand elle est active, le direct passe par un *moteur Streamly* que l'utilisateur héberge, qui recompresse le flux en HLS adaptatif : 0,2 à 0,7 Go/h au lieu de 2,5 à 4 Go/h. Public visé en premier : utilisateurs sur données mobiles chères (Afrique de l'Ouest), donc **Android d'abord**, iOS ensuite, Android TV en ligne de mire.

L'app doit être pleinement utilisable **sans aucun moteur** (lecture directe). Le moteur est une option qu'on branche dans les réglages.

## 2. Règles pour l'implémenteur

1. Ne modifier que `app/`. Ne pas toucher à `server/`, `web/`, `deploy/`, `tests/`. Si le moteur doit évoluer, l'écrire dans `app/NOTES-MOTEUR.md` au lieu de le faire.
2. Aucun identifiant réel (fournisseur IPTV, moteur, VPS) dans le dépôt, les tests, les captures ou les journaux. Les tests utilisent des serveurs factices.
3. Chaque jalon (§ 9) se termine avec `flutter analyze` sans avertissement et `flutter test` au vert, puis un commit. Ne pas enchaîner sur le jalon suivant si l'un des deux échoue.
4. Ne pas déclarer une fonction « faite » sans l'avoir exécutée (test, ou lancement sur simulateur/émulateur). Dire clairement ce qui n'a pas pu être vérifié.
5. Textes de l'interface en français et en anglais via `flutter_localizations` / fichiers ARB. Français par défaut. Aucune chaîne en dur dans les widgets.
6. Code, noms et commentaires en anglais ; commentaires seulement pour expliquer un *pourquoi* non évident.

## 3. Pile technique

| Besoin | Choix | Raison |
|---|---|---|
| Framework | Flutter stable, Dart 3, null-safety | Une base pour Android, iOS, Android TV |
| État | `flutter_riverpod` (+ `riverpod_annotation`) | Testable, sans contexte |
| Navigation | `go_router` | Liens profonds, garde d'onboarding |
| Base locale | `drift` (SQLite) avec FTS5 | Un bouquet fait 25 000 chaînes, 60 000 films : tout en mémoire est exclu |
| Réseau | `dio` | Délais, annulation, en-têtes (User-Agent) |
| **Lecteur vidéo** | **`media_kit`** (libmpv) | Les directs IPTV sont souvent du MPEG-TS brut sur HTTP, que le lecteur natif d'iOS ne lit pas. `media_kit` lit TS, HLS, MKV, HEVC sur toutes les plateformes. Ne **pas** utiliser `video_player`. |
| Secrets | `flutter_secure_storage` | Mots de passe fournisseur et jeton du moteur |
| Réseau mobile | `connectivity_plus` | Activer l'économie de données en cellulaire |
| Images | `cached_network_image` | Logos de chaînes, affiches |
| XML | `xml` (mode événements) dans un isolate | Un guide XMLTV pèse jusqu'à 80 Mo |
| Achat | `in_app_purchase`, derrière une interface | Voir § 8 |

Identifiant provisoire : `com.streamly.app`, nom affiché « Streamly ». Les centraliser pour pouvoir les changer avant publication.

Cibles : Android minSdk 23 ; iOS 14+. Orientation libre ; paysage forcé en plein écran vidéo.

## 4. Architecture

```
app/lib/
  main.dart, app.dart            # thème, routeur, localisation
  core/                          # erreurs typées, résultat, journal sans secrets, utilitaires
  data/
    db/                          # drift : tables, DAO, migrations
    sources/
      xtream_client.dart         # player_api.php
      m3u_parser.dart            # analyse en flux, isolate
      xmltv_parser.dart          # analyse en flux, isolate
      engine_client.dart         # API du moteur (docs/api-moteur.md)
    repositories/                # playlists, channels, vod, series, epg, favorites, engine
  domain/                        # modèles immuables, règles (choix de variante, calcul d'économie)
  features/
    onboarding/  home/  live/  guide/  vod/  series/  search/  favorites/
    player/                      # lecteur, zapping, surimpressions, mode budget
    settings/                    # playlists, moteur, lecture, contrôle parental
  ui/                            # thème, composants partagés, gestion du focus (TV)
app/test/                        # unitaires + widgets ; serveurs factices
```

Principes : les widgets ne parlent qu'aux *providers* ; les dépôts cachent la base et le réseau ; tout ce qui analyse un gros fichier tourne dans un isolate et écrit en base par lots de 500 à 1 000 lignes ; aucune requête réseau dans `build`.

### Modèle de données (drift)

- `playlists` : id, nom, type (`xtream` | `m3u`), hôte/URL, utilisateur, *référence* du secret (le mot de passe vit dans le stockage sécurisé), user-agent, dernière synchro, état du compte (expiration, connexions max).
- `categories` : playlist, type (`live` | `vod` | `series`), id distant, nom, ordre, masquée, verrouillée (contrôle parental).
- `channels` : playlist, id distant, nom, nom canonique, qualité (SD/HD/FHD/UHD), hauteur estimée, catégorie, logo, `epg_id`, URL (M3U), numéro, est-un-secours.
- `vod`, `series`, `episodes` : id distant, titre, catégorie, affiche, note, année, conteneur, synopsis, durée, (saison, épisode).
- `epg_programmes` : `epg_id`, début, fin, titre, description. Index (`epg_id`, début). Purge de ce qui est fini depuis plus de 2 h.
- `favorites`, `recents` (chaînes, avec horodatage), `watch_progress` (films/épisodes : position, durée).
- `engines` : id, nom, adresse de base, *référence* du jeton, dernière vérification.
- `usage` : jour, octets via moteur, secondes de lecture via moteur (pour « données économisées »).
- Recherche : table FTS5 sur noms de chaînes, films et séries.

### Regroupement des qualités

Un même programme existe souvent en `FR| TF1 SD`, `FR| TF1 HD`, `FR| TF1 FHD`, `FR| TF1 [BK]`. L'app affiche **une** entrée « TF1 » et choisit la variante à la lecture. Porter en Dart la logique de `parse_name`, `quality_height` et `is_backup` de `server/streamly/xtream.py`, **avec ses cas de test** (voir `tests/test_streamly.py`, y compris les noms en arabe qui ne doivent jamais fusionner). Choix de la variante :
- lecture directe : la meilleure qualité, ou le plafond choisi dans les réglages ;
- économie de données : la plus proche de 720p sans descendre dessous si possible (`Catalog.pick_source`) ;
- en cas d'échec de lecture : essayer la variante suivante, puis les secours `[BK]`, avant d'afficher une erreur.

## 5. Sources de contenu

### Xtream Codes

Base : `{hôte}/player_api.php?username=…&password=…`. Sans `action` : `user_info` (statut, `exp_date`, `max_connections`, `active_cons`) et `server_info`. Actions : `get_live_categories`, `get_live_streams`, `get_vod_categories`, `get_vod_streams`, `get_vod_info&vod_id=`, `get_series_categories`, `get_series`, `get_series_info&series_id=`, `get_short_epg&stream_id=&limit=`.

URL de lecture : direct `{hôte}/{u}/{p}/{id}` (TS) ou `{hôte}/live/{u}/{p}/{id}.m3u8` ; film `{hôte}/movie/{u}/{p}/{id}.{ext}` ; épisode `{hôte}/series/{u}/{p}/{episode_id}.{ext}`. Guide complet : `{hôte}/xmltv.php?username=…&password=…`.

Pièges connus : les panels renvoient des nombres en chaînes, des `null`, des listes vides à la place d'objets, des catégories vides ; les titres de `get_short_epg` sont en **base64** ; certains panels refusent les User-Agent inconnus (utiliser `VLC/3.0.20 LibVLC/3.0.20` par défaut, réglable par playlist) ; `get_live_streams` peut peser 8 Mo (délai de 120 s, analyse en isolate). Tout champ est optionnel : ne jamais planter sur une réponse inattendue.

Un lien M3U de la forme `…/get.php?username=U&password=P…` doit être **reconnu comme un compte Xtream** (guide, films et séries deviennent disponibles).

### M3U

Analyse en flux (le fichier peut faire 60 Mo) : `#EXTINF:-1 tvg-id tvg-name tvg-logo group-title,Nom` puis l'URL ; tolérer attributs sans guillemets, lignes `#EXTVLCOPT`, `#EXTGRP`, encodage latin-1, BOM, fins de ligne Windows. Type déduit de l'URL (`/movie/`, `/series/`, extension vidéo → film/épisode ; sinon direct).

### Guide des programmes

1. Pour la chaîne à l'écran et les listes visibles : `get_short_epg` à la demande, avec cache.
2. En arrière-plan, au plus une fois toutes les 12 h et **seulement en Wi-Fi** par défaut : XMLTV analysé en flux dans un isolate, en ne gardant que les `epg_id` des favoris, des récents et des catégories ouvertes récemment, et les programmes des prochaines 48 h. Ne jamais stocker le guide complet.
3. Les moteurs Streamly servent aussi un XMLTV déjà réduit et compressé (`{moteur}/xmltv.php`), mais il exige un compte lecteur du moteur : hors V1.

## 6. Écrans et parcours

**Onboarding** : bienvenue → ajouter une playlist (Xtream : hôte, identifiant, mot de passe ; ou lien M3U ; bouton « coller ») → vérification du compte (statut, expiration, connexions) → synchronisation avec progression réelle (catégories, chaînes, films, séries) → accueil. Erreurs explicites : hôte injoignable, identifiants refusés, compte expiré.

**Accueil** : reprise de la dernière chaîne, récents, favoris, « en ce moment » sur les favoris, accès Direct / Films / Séries / Guide / Recherche.

**Direct** : catégories (réordonnables, masquables) → chaînes avec logo, numéro, programme en cours et barre de progression. Liste virtualisée, fluide à 25 000 entrées. Appui long : favori, masquer, infos.

**Lecteur** (le cœur du produit) :
- Démarrage le plus court possible ; état « préparation » distinct en économie de données (4 à 11 s).
- Zapping : haut/bas (télécommande, clavier) et balayage vertical ; liste des chaînes en surimpression sans quitter la vidéo ; retour à la chaîne précédente ; saisie d'un numéro.
- Bandeau d'info : chaîne, programme en cours et suivant, qualité réelle, indicateur **Direct** ou **Économie**.
- Bouton économie de données : bascule à chaud direct ↔ moteur sur la même chaîne.
- Pistes audio et sous-titres, format d'image, verrouillage de l'écran, image dans l'image (Android et iOS), lecture en arrière-plan en mode audio.
- Reprise automatique : nouvelle tentative, puis variante suivante, puis secours ; message clair si tout échoue (« connexion de l'abonnement déjà utilisée » est un cas fréquent à nommer).
- Films/épisodes : barre de progression, ±10 s, reprise à la dernière position, épisode suivant.

**Guide** : grille horaire des favoris et d'une catégorie, programme en cours mis en avant, appui = lire.

**Films / Séries** : catégories, grilles d'affiches, fiche (synopsis, durée, note, saisons/épisodes), reprise. Toujours en lecture directe.

**Recherche** : globale (chaînes, films, séries) via FTS, résultats pendant la frappe, insensible aux accents.

**Réglages** : playlists (ajouter, renommer, resynchroniser, supprimer, état du compte) · moteur Streamly (§ 7) · lecture (qualité max en direct, décodage matériel, tampon, User-Agent) · contrôle parental (code PIN, catégories verrouillées, masquage des catégories adultes par défaut) · langue · à propos.

### Direction visuelle

Sombre, esprit Netflix / Apple TV, accent violet — identique à l'app web (`web/style.css`) : fond `#0a0a14`, panneaux `#12121f` / `#1a1a2e`, lignes `#262639`, texte `#f5f5fa`, texte secondaire `#9d9db4`, accent `#a78bfa` (survol `#c4b5fd`), dégradé principal `#7c3aed → #a855f7`, danger `#ff7b92`, badge direct rouge `#ff4d5e`. **Pas de vert.** Coins 12–16 px, grandes cibles tactiles (48 dp), focus toujours visible.

**Télécommande dès le départ** : toute l'app doit se piloter au D-pad (`FocusTraversalGroup`, focus initial sur chaque écran, touches média). C'est ce qui rendra Android TV possible sans réécriture.

## 7. Économie de données (moteur Streamly)

Contrat complet : `docs/api-moteur.md`. Côté app :

- **Ajouter un moteur** : adresse + code d'association (saisie, et lecture d'un QR code `streamly://pair?base=…&code=…`). Refuser `http://` hors adresses privées du réseau local, avec une explication. Jeton dans le stockage sécurisé. Indicateur d'état (joignable, associé, révoqué).
- **Activation** : manuelle par le bouton du lecteur ; automatique « en données mobiles » (réglage, activé par défaut une fois un moteur associé) ; par chaîne (mémoriser le dernier choix).
- **Modes** : Économie, Équilibré, Sport, Audio seul, **Budget** (« 800 Mo pour ce match de 120 min »). Afficher le plafond réellement appliqué par le moteur (`ceiling`), qui peut être inférieur au mode demandé.
- **Compteur** : interroger `/api/playback` toutes les 3 s ; afficher Mo consommés, et en mode Budget une jauge restante ; à `402`, écran « budget épuisé » avec « ajouter 200 Mo » / « passer en audio » / « arrêter ».
- **Génération** : si `generation` change ou si un segment répond `410`, recharger la même `play_url` sans quitter l'écran ni afficher d'erreur.
- **Libération** : `POST /api/stop` à chaque sortie de lecture, changement de chaîne vers le direct, et après 60 s en arrière-plan. C'est ce qui rend la connexion de l'abonnement à l'utilisateur.
- **Repli** : moteur injoignable ou `409` → proposer la lecture directe en un geste, en rappelant le surcoût de données.
- **Données économisées** : estimation = durée × débit moyen de la source (mesuré en direct quand c'est possible, sinon 3 Go/h) − octets servis par le moteur. Cumul par jour dans `usage`, affiché dans Réglages et à la fin d'une lecture.
- Direct uniquement : le bouton n'apparaît pas sur les films et séries.

## 8. Premium

V1 : le code distingue les fonctions gratuites (tout le lecteur en direct) et premium (économie de données) derrière une interface `Entitlements`. Implémentation V1 : tout est débloqué, avec un drapeau de compilation pour simuler l'état verrouillé. Le branchement réel à `in_app_purchase` est le dernier jalon et ne doit jamais bloquer les précédents. Aucun prix ni texte de vente en dur.

## 9. Jalons et critères d'acceptation

Chaque jalon : code + tests + `flutter analyze` propre + commit.

| # | Jalon | C'est fait quand… |
|---|---|---|
| 1 | Socle | Projet `app/`, thème, routeur, localisation fr/en, base drift avec migrations, journal qui masque mots de passe et jetons (testé). |
| 2 | Playlists | Ajout Xtream et M3U, détection `get.php`, vérification du compte, synchro avec progression ; tests sur réponses de panel malformées, M3U de 50 000 lignes analysé en moins de 10 s sans geler l'interface. |
| 3 | Direct | Catégories, liste virtualisée, regroupement des qualités (cas de test portés du serveur), favoris, récents ; défilement fluide sur 25 000 chaînes. |
| 4 | Lecteur | Lecture TS et HLS, zapping, surimpressions, pistes, PiP, reprise sur échec par variante ; vérifié sur émulateur Android **et** simulateur iOS avec le flux de test public. |
| 5 | Guide | EPG court à la demande, XMLTV réduit en arrière-plan, grille ; test d'analyse en flux sur un XMLTV de 50 Mo avec mémoire bornée. |
| 6 | Films et séries | Grilles, fiches, lecture avec reprise de position, épisode suivant. |
| 7 | Recherche et réglages | FTS insensible aux accents, contrôle parental, gestion des playlists. |
| 8 | Économie de données | Association, relais, modes, compteur, budget et écran `402`, génération, libération, repli ; tests contre un **moteur factice** qui rejoue chaque réponse de `docs/api-moteur.md` ; essai réel contre un moteur local (§ 7 de `api-moteur.md`). |
| 9 | Télécommande et finition | Parcours complet au D-pad, états vides et d'erreur, accessibilité (tailles de texte, contrastes), icône et écran de lancement. |
| 10 | Premium | Interface `Entitlements` branchée à `in_app_purchase`, état verrouillé testable. |

## 10. Tests

- Unitaires : analyseurs (M3U, XMLTV, réponses Xtream), regroupement des qualités, choix de variante, calcul d'économie, masquage des secrets.
- Dépôts : contre des serveurs factices en mémoire (`dio` avec adaptateur de test) — jamais de réseau réel en test automatique.
- Widgets : onboarding, liste de chaînes, surimpressions du lecteur (lecteur vidéo simulé), écran budget.
- Jeux de données dans `app/test/fixtures/`, anonymisés : aucun vrai hôte, identifiant ou nom de fournisseur.

## 11. Hors V1

Enregistrement, rattrapage (catch-up), multi-écrans, Chromecast/AirPlay, synchronisation entre appareils, comptes cloud, relais pour films et séries, interface Android TV dédiée (le D-pad doit marcher, la mise en page 10 pieds viendra après), publication sur les magasins.

## 12. Risques à garder en tête

- **`media_kit` sur iOS** : vérifier tôt (jalon 4) la lecture TS, le PiP et l'audio en arrière-plan ; c'est le point technique le plus incertain.
- **Taille des bouquets** : toute liste complète en mémoire est un bug. Pagination par la base, toujours.
- **Une seule connexion par abonnement** : ne jamais ouvrir deux flux à la fois (pas de préchargement de la chaîne suivante ; arrêter l'ancien flux avant d'ouvrir le nouveau, y compris lors de la bascule direct ↔ moteur).
- **Validation des magasins** : l'app ne contient, ne suggère et ne préconfigure aucun fournisseur ; captures d'écran avec des contenus libres de droits.
