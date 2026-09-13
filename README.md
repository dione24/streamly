# Streamly

Un lecteur IPTV auto-hébergé pour les connexions irrégulières et les forfaits
facturés au volume. Le VPS transforme les sources Xtream en HLS multi-débits ;
le navigateur choisit une qualité compatible avec le réseau **et** le mode choisi.

## Lecture

- **Économie / Équilibré / Sport / Budget** : plafonds servis dans la playlist,
  y compris sur Safari natif. Le mode Budget réserve un volume pour une séance
  et refuse les nouveaux segments lorsque ce volume est atteint.
- **Direct réactif / Connexion instable** : objectifs de retard de 6 / 30 secondes,
  réserve client de 18 / 45 secondes et fenêtre serveur d'au moins 72 secondes.
  Ce sont des réglages, pas une garantie de continuité pendant toute coupure.
- Comptage des octets vidéo servis, estimation Mo/h, réserve et interruptions.
  Le compteur opérateur inclut d'autres données et peut différer ; le budget
  réserve 3 % de marge et compte conservativement les envois commencés.
- Cadence source sondée et conservée jusqu'à 60 images/s, images clés alignées
  dans le temps, pas d'agrandissement artificiel de la définition.
- Surveillance des nouveaux segments, même si FFmpeg est encore vivant.
  Une bascule crée une génération de fichiers distincte ; le lecteur la recharge.
  Les sources récemment défaillantes sont dépriorisées pendant deux minutes.

## Plusieurs appareils

Une même chaîne partage son encodage. Chaque lecture a son ticket, son budget
et son arrêt indépendant. Des chaînes différentes peuvent tourner dans les
limites `max_concurrent_streams` et `providers[].max_connections`.
Les préparations de films utilisent ces mêmes réservations.

Ne déclarez pas plus de connexions que votre abonnement n'en autorise.
Le défaut est deux traitements globaux et une connexion par provider ; ajustez
la capacité après mesure sur votre VPS. Les sessions de lecture sans requête
vidéo pendant trois minutes sont libérées.

## Films

Choisir un film ouvre une fiche avec poids source estimé et qualité à préparer.
Le VPS prépare des MP4 H.264/AAC et des variantes HLS ; aucun téléchargement
complet du film source n'est nécessaire sur le téléphone.

- Préparation explicite en arrière-plan, progression et résultat persistants.
- Choix de l'audio et des sous-titres **texte** disponibles (les sous-titres image
  ne sont pas convertis).
- Lecture adaptative lorsque la préparation est terminée ; reprise locale de
  la dernière position.
- Téléchargement MP4 avec HTTP Range : un client compatible peut reprendre
  son téléchargement. La reprise dépend du navigateur/gestionnaire utilisé.
- Cache plafonné à 8 Go par défaut, garde de disque libre et limite de quatre
  heures par commande. Les anciens résultats sont nettoyés lors d'une nouvelle
  préparation après sept jours sans accès (les accès sont mémorisés en RAM).

La préparation peut être longue sur un petit VPS. La lecture VOD directe de la
version précédente est remplacée par ce parcours pour garantir un format léger
et compatible. Les séries ne font pas encore partie du catalogue.

## Bibliothèque et interface

Interface responsive, commandes vidéo natives, plein écran et mini-lecteur si
le navigateur le propose, favoris, récents, recherche annulable et pagination
par 48 éléments. Le catalogue reste en SQLite sur le serveur. Les métadonnées
VOD survivent aux synchronisations. Une migration conserve les caractères non
latins dans l'identification des chaînes ; d'anciens favoris devenus ambigus
peuvent devoir être recréés.

hls.js 1.5.17 est distribué localement dans `web/vendor`, avec sa licence Apache
2.0. L'ouverture de l'app ne dépend plus d'un CDN JavaScript.

## Installation

Python **3.10+**, FFmpeg et ffprobe sont requis. Bibliothèque standard Python,
sans service de base de données externe.

```sh
cd server
cp config.example.json config.json
# Renseigner les providers, puis :
python3 run.py
```

L'installation historique dans `/opt/streamly` utilise un service systemd.
Ne remplacez jamais `server/config.json` ou `server/data` lors d'une mise à jour.
Le script `deploy/install.sh` est destiné à une première installation.

## Accès et HTTPS

Le jeton principal ouvre une session **administrateur**. Le jeton lecture seule
est accessible dans Réglages et ne peut pas modifier les abonnements.
Les sessions utilisent un cookie HttpOnly / SameSite=Strict, expirent après
sept jours et sont invalidées au redémarrage du serveur. Les URLs live portent
un ticket de lecture, jamais le jeton administrateur. Les journaux HTTP masquent
les tickets. `config.json` est privé et exclu du dépôt.

**Sans domaine, l'installation actuelle reste en HTTP et les échanges ne sont
pas chiffrés.** Le modèle Apache `deploy/apache-streamly.conf.example` prépare
l'ajout de HTTPS sans remplacer les autres virtual hosts. Ne l'activez qu'après
configuration du domaine et d'un certificat valide ; activez alors
`secure_cookies` et limitez l'écoute du backend à `127.0.0.1`.

Le passage par un VPS ne garantit pas de supprimer les blocages opérateur.

## Vérification

```sh
python3 -m unittest discover -s tests -v
python3 tests/media_smoke.py
node --check web/app.js
```

Le test média génère une vidéo synthétique à 50 images/s et vérifie les quatre
variantes HLS, l'alignement des segments et la préparation VOD H.264/AAC.
`tests/preview.py` lance une interface locale avec un catalogue fictif, sans
accès aux abonnements (jeton `preview-only`, usage local uniquement).

Les anciennes mesures de Claude sont conservées dans `docs/mesures.md` à titre
historique : elles ne prouvent pas les performances de cette version. Mesurer
sur les sources et appareils réels avant d'annoncer une économie ou un délai.

## Licence

MIT, voir [LICENSE](LICENSE). hls.js conserve sa propre licence.
