# Streamly — où on en est

Mis à jour le 6 novembre 2026. Remplace la note du chantier du 13 septembre, terminé.

## En ligne

- 5 instances (principale, ami1 à ami4) en HTTPS sur `*.51-38-214-123.sslip.io`, version web du 24 septembre.
- Page de présentation sur `/landing/`. Il lui manque une adresse de contact pour le bouton « Demander un accès ».
- Façade Xtream/M3U pour les lecteurs externes (TiviMate, Smarters, Smart One, IBO Player, VLC).

## Lecteur natif

- macOS en Swift, dans `apple/` (voir `apple/README.md`). Suite des jalons de `docs/app-v1-spec.md`.
- La spécification reste écrite pour Flutter (Android d'abord). Le choix de Swift pour iOS n'est pas encore tranché.

## À faire

1. Contrôler le VPS : renouvellement des certificats, à vérifier avant le 18 décembre 2026, espace disque, journaux.
2. Recueillir les retours des amis sur la bêta.
3. Donner une adresse de contact à la page de présentation.

## Réseau du Mac de développement

Certains réseaux bloquent le port 22 en sortie, vers GitHub comme vers le VPS. GitHub reste accessible par `ssh.github.com:443`. Le VPS n'a pas d'accès SSH sur le 443 : pour le contrôler, il faut un autre réseau.

## Règles

Ne pas écraser `config.json`, le catalogue ni les identifiants sur le VPS. Ne jamais versionner de secrets. Avant de redémarrer une instance, vérifier qu'aucune lecture n'est en cours.
