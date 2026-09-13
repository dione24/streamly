# Streamly — chantier approuvé le 13 septembre 2026

L'utilisateur a validé les huit axes : économie/budget, récupération des coupures,
tampon adaptable, encodage sport, sessions multi-appareils, VOD allégée et reprise,
performances/catalogue, refonte lecteur + sécurité/HTTPS.
Priorité : F1/sport à Bamako, réseau irrégulier, données mobiles payantes.

État initial : main c033de4. Correctif CSS hidden déjà déployé lors du tour précédent.
Autorisation : implémenter et déployer les améliorations. Ne pas écraser config.json,
catalogue ou identifiants sur le VPS. Ne jamais versionner de secrets.
HTTPS : nom de domaine demandé à l'utilisateur, réponse en attente.
Le quota hebdomadaire était à 97 % consommé au début du chantier.

Travail en cours : moteur de sessions partagé et récupération des flux, puis UI,
VOD et tests. Ce fichier sera mis à jour avec les validations avant livraison.
