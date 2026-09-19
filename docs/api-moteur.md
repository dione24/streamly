# API du moteur Streamly pour l'application

Contrat entre l'app (Flutter) et un moteur Streamly. Tout ce qui est décrit ici existe et est testé (`tests/test_streamly.py`, classes `HTTPTests` et `RelaySourceTests`). Code : `server/streamly/app.py`, `server/streamly/relay.py`.

Pour l'app, **un moteur = une adresse de base + un jeton d'appareil**. Exemple d'adresse : `https://tv.exemple.fr` ou `http://192.168.1.20:8088`.

Toutes les réponses d'erreur sont du JSON `{"error": "message en français"}` — affichable tel quel à l'utilisateur.

## 1. Association d'un appareil

L'administrateur du moteur clique « Associer un appareil » dans Réglages (web) : un code de 8 caractères s'affiche (`ABCD-EFGH`), valable 10 minutes, utilisable une fois.

```
POST {base}/api/pair
Content-Type: application/json

{"code": "ABCD-EFGH", "name": "Pixel 8 de Awa"}
```

- Le code est insensible à la casse ; tirets et espaces sont ignorés.
- `200` → `{"id": "9f2c1a…", "name": "Pixel 8 de Awa", "token": "…32 caractères…"}`
- `401` → code inconnu ou expiré. `429` → trop de tentatives (10 échecs / 5 min par adresse IP). `400` → trop d'appareils (20 max).

Le `token` n'est renvoyé **qu'une fois** : le stocker dans le trousseau sécurisé de l'appareil. Le moteur n'en garde que l'empreinte.

Toutes les requêtes suivantes portent : `Authorization: Bearer <token>`. Pas de cookie.

Un appareil n'a accès qu'aux quatre routes ci-dessous ; tout le reste répond `403`. Si l'appareil est retiré depuis Réglages, ses requêtes répondent `401` : l'app doit alors proposer de se ré-associer.

## 2. Vérifier l'association

```
GET {base}/api/me        → 200 {"role": "device"}   | 401
```

À appeler à l'ajout du moteur et au lancement de l'app (état « moteur joignable »).

## 3. Demander un flux compressé

```
POST {base}/api/relay
{"source": "http://panel.tld:80/user/pass/12345",
 "label": "TF1",
 "mode": "balanced"}
```

| Champ | Rôle |
|---|---|
| `source` | URL http(s) du direct, telle que l'app la lirait elle-même. 2048 caractères max. |
| `label` | Nom affiché dans le tableau de bord du moteur (80 car. max). |
| `mode` | `eco` (360p max, ~0,2 Go/h), `balanced` (480p max, ~0,4 Go/h), `sport` (720p max, ~0,7 Go/h), ou `budget`. |
| `budget_mb`, `minutes` | Avec `mode: "budget"` : volume (20 à 50 000 Mo) et durée (5 à 1 440 min). Le moteur en déduit le débit plafond et **coupe à l'épuisement**. |
| `audio_only` | `true` = son seul (~45 Mo/h). |

Réponse `200` :

```json
{"ticket": "…", "play_url": "/s/…/master.m3u8",
 "ceiling": 1150000, "budget": 0, "audio_only": false}
```

Lire `{base}{play_url}` : HLS adaptatif standard (H.264 + AAC, segments MPEG-TS de 2 s, plusieurs qualités). **L'URL de lecture se lit sans en-tête d'authentification** : le ticket dans le chemin suffit.

Erreurs : `400` source refusée (pas http/https, hôte introuvable, **adresse privée ou locale** — voir § 7) ou budget hors limites ; `409` moteur ou abonnement occupé (message explicite) ; `401`/`403`.

Règles à connaître :
- **Une lecture par appareil** : un nouveau `relay` arrête le précédent du même appareil.
- **Un hôte source = une connexion** : deux flux du même panel ne s'ouvrent pas en parallèle (les abonnements IPTV n'ont souvent qu'une connexion).
- Le moteur peut imposer une borne de qualité (`max_mode`) : `ceiling` dans la réponse reflète le plafond réellement appliqué, à afficher plutôt que le mode demandé.
- Démarrage à froid : 4 à 11 s avant le premier segment. Afficher un état « préparation ».
- Relais = **direct uniquement**. Pas de retour en arrière ni d'avance : fenêtre glissante d'environ 70 s. Films et séries se lisent en direct depuis le fournisseur.

## 4. Suivre la lecture

```
GET {base}/api/playback?ticket=…
```

`200` :

```json
{"state": "playing", "generation": 0, "failovers": 0,
 "bytes": 18234567, "budget": 0, "ceiling": 1150000,
 "label": "TF1", "uptime_s": 95, "passthrough": false,
 "audio_only": false, "error": null,
 "media": {"width": 1280, "height": 720, "fps": 25.0}}
```

- `state` : `starting` → `buffering` → `playing`, ou `failed`.
- `bytes` : octets réellement servis à cet appareil. C'est le **compteur du mode Budget** et la base du calcul « données économisées ».
- `generation` : augmente quand le moteur relance l'encodage (source instable, changement de qualités). **Quand elle change, l'app doit recharger `play_url`** (même URL), sinon le lecteur reste sur une playlist qui ne bouge plus.
- `404` → lecture expirée : refaire un `relay`.

Interroger toutes les 3 s pendant la lecture. Un ticket sans aucune requête pendant 180 s expire.

Codes des URL de lecture `/s/{ticket}/…` : `401` ticket expiré, `402` **budget atteint** (afficher « budget épuisé », proposer de continuer), `410` génération remplacée (recharger `play_url`), `502` source indisponible, `504` source trop lente.

## 5. Arrêter

```
POST {base}/api/stop   {"ticket": "…"}   → 200 {"ok": true}
```

À appeler en quittant la lecture, en changeant de chaîne vers le mode direct, et à la mise en arrière-plan prolongée : cela libère la connexion de l'abonnement.

## 6. Quelle source envoyer

- Compte Xtream : `{hôte}/{utilisateur}/{mot_de_passe}/{stream_id}` (flux MPEG-TS). C'est ce que le moteur utilise lui-même.
- Playlist M3U : l'URL de l'entrée, telle quelle.
- Si une chaîne existe en plusieurs qualités (SD / HD / FHD / 4K), envoyer la variante **la plus proche de 720p sans descendre en dessous si possible** : ingérer du 1080p triple le coût processeur du moteur pour un rendu identique après compression. Règle de référence : `Catalog.pick_source` dans `server/streamly/catalog.py` ; découpage des noms : `parse_name` dans `server/streamly/xtream.py`.

Les identifiants du fournisseur transitent donc vers le moteur dans `source`. C'est acceptable parce que le moteur appartient à l'utilisateur (auto-hébergé) ; **exiger HTTPS** pour toute adresse de moteur qui n'est pas sur le réseau local.

## 7. Environnement de test

Moteur local :

```
cd server && python3 run.py        # port dans server/config.json (8099 sur le Mac de dev)
```

Code d'association : interface web → Réglages → « Associer un appareil ».

Le relais refuse les sources en adresse privée ou locale. Pour tester avec une source sur le réseau local, mettre `"relay_allow_private": true` dans `server/config.json` du moteur **de test uniquement**. Source publique utilisable : `https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8`.

Essai complet en ligne de commande :

```
curl -X POST $BASE/api/pair  -H 'Content-Type: application/json' -d '{"code":"ABCDEFGH","name":"curl"}'
curl -X POST $BASE/api/relay -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"source":"https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8","mode":"eco"}'
ffplay "$BASE/s/<ticket>/master.m3u8"
```

## 8. Limites connues du moteur (à ne pas contourner dans l'app)

- Pas de relais pour films et séries (préparation asynchrone, hors V1).
- Le changement de génération impose un rechargement côté app (§ 4). Une playlist stable côté moteur est prévue plus tard.
- Pas de découverte automatique du moteur sur le réseau local (saisie de l'adresse ou QR code).
