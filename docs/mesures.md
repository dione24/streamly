# Mesures

Toutes les valeurs proviennent d'un panel Xtream réel et d'un VPS
**Xeon Gold 6242R 3,10 GHz, 4 vCPU, 8 Go**. Elles justifient les choix
techniques du projet ; refaites-les sur votre propre installation, les
panels varient énormément.

## Pourquoi un transcodage est nécessaire

Débits mesurés sur les variantes d'une même chaîne, telles que servies par
le panel :

| Variante annoncée | Résolution réelle | Débit | 1 h |
|---|---|---|---|
| FHD | 1920×1080 | 8 893 kbps | 4,00 Go |
| HD | 1280×720 | 5 988 kbps | 2,69 Go |
| HEVC | **1280×720 en H.264** | 5 522 kbps | 2,48 Go |

Deux enseignements : même la variante la plus légère consomme ~2,5 Go/h, et
**les étiquettes du panel mentent** — la variante « HEVC » est du H.264.
Il faut sonder les flux, pas se fier aux noms.

## Après transcodage

Chaîne sport, source 1080p à 6,81 Mbps :

| Barreau | Débit | 1 h | Match de 2 h |
|---|---|---|---|
| Source | 6,81 Mbps | 3,07 Go | 6,13 Go |
| 720p | 1,50 Mbps | 0,68 Go | 1,35 Go |
| 480p | 0,89 Mbps | 0,40 Go | 0,80 Go |
| 360p | 0,55 Mbps | 0,25 Go | 0,49 Go |
| 240p | 0,25 Mbps | 0,11 Go | 0,22 Go |

## Coût CPU

Mesures en **régime établi** (delta de `/proc/<pid>/stat` sur 20 s). Attention :
`ps %cpu` donne une moyenne depuis le lancement du processus et surestime
fortement, le décodage du tampon initial se faisant à pleine vitesse.

| Configuration | Source | vCPU | Machine |
|---|---|---|---|
| 3 barreaux, copie du 1080p + 720p + 360p | 1080p | 2,09 | 52 % |
| 3 barreaux encodés (720/480/360) | 720p | 0,94 | 23 % |
| **4 barreaux encodés (720/480/360/240)** | **720p** | **1,15** | **29 %** |
| 3 barreaux encodés | 1080p | 2,77 | 69 % |

**Ingérer le 720p plutôt que le 1080p divise le coût par trois** pour un
rendu final identique : c'est l'optimisation la plus rentable du projet.

## Régularité des segments

Durées réelles mesurées, en secondes :

```
flux copié   : 2,36  1,84  4,96  0,88  0,68  1,04     <- keyframes de la source
flux encodé  : 2,00  2,00  2,00  2,00  2,00  2,00     <- avec -g force
```

Les frontières d'un flux copié ne coïncident pas avec celles des barreaux
encodés, ce qui dégrade la bascule ABR. C'est la raison pour laquelle
Streamly ré-encode **tous** les barreaux, y compris le plus haut — et cela
se trouve être moins coûteux, puisqu'on décode alors du 720p et non du 1080p.

## Démarrage

| Étape | Depuis le VPS | Depuis une machine distante |
|---|---|---|
| `master.m3u8` | 0,39 s | 0,006 s (généré localement) |
| Premier segment | 1,79 s | 8 à 11 s |

L'écart vient de la route réseau vers le panel, pas du transcodage. Héberger
Streamly près du panel améliore nettement le confort.

## Volume du catalogue

| Endpoint | Entrées | Taille |
|---|---|---|
| `get_live_streams` | 26 495 | 7,4 Mo |
| `get_vod_streams` | 46 068 | 14,4 Mo |
| `get_series` | 23 508 | 37,9 Mo |
| **Total** | **96 071** | **≈ 60 Mo** |

Un client qui recharge tout à chaque ouverture télécharge 60 Mo. D'où la
synchronisation unique vers SQLite.

## Anomalies de panel rencontrées

- `get_vod_categories` et `get_series_categories` renvoient **une liste vide**
  alors que les flux portent un `category_id` valide. Les libellés sont
  récupérables depuis les `group-title` de l'export M3U : 320/320 catégories
  live et 40/47 catégories VOD ont ainsi été reconstruites.
- Le panel **refuse les requêtes** selon le `User-Agent` (échec avec celui de
  `curl`, succès avec celui de VLC).
- L'export M3U est régénéré à la volée et met **plus de deux minutes** pour
  30 Mo.
- Les URLs de flux redirigent (302) vers un edge **tokenisé** : elles sont
  éphémères et ne peuvent pas être mises en cache.
- `container_extension` est absent de `get_vod_streams` et n'est disponible
  que via `get_vod_info`, soit un appel par film.
- Le conteneur VOD est souvent **MKV** avec de l'HEVC Main 10 et de l'E-AC3,
  que `AVPlayer` ne sait pas lire — un remux `-c copy` vers MP4 suffit à le
  rendre lisible, sans ré-encodage.

## Films : lecture pendant la préparation (22 septembre 2026)

Source synthétique 1080p HEVC Main 10 + E-AC3 (2 min), commande exacte du
serveur (`Movies._command`), sans autre lecture en cours.

| Machine | 480p (3 qualités) | 720p (4 qualités) |
|---|---|---|
| VPS, 4 cœurs (instance principale) | 3,6× temps réel | 2,5× temps réel |
| VPS, 2 cœurs (proche des instances amis, `CPUQuota=150%`) | 2,3× | 1,5× |
| Mac M1 Pro | 7,1× | 5,7× |

Préversion locale, faux panel Xtream bridé à 4× le temps réel, Chrome + hls.js :
première image 3,1 à 3,4 s après « Regarder maintenant » ; reprise 3,5 à
5,5 s après un saut vers un passage non encodé (une passe FFmpeg par saut) ;
film de 10 min entièrement préparé en 160 s, MP4 assemblé de 7 passes :
15 000 images pour 600 s, sans trou ni doublon.

Sous-titres texte dans la passe vidéo : FFmpeg n'écrit rien avant la
réplique suivante — 44 s d'attente mesurées pour 2 min 30 sans dialogue,
identique en copie SRT/MKV et avec `-max_interleave_delta`. Ils sont donc
extraits par un FFmpeg séparé.
