# Vérification du 19 septembre 2026

## Arrêt signalé dans Smart One IPTV

L'utilisateur signale une lecture qui s'arrête après environ deux minutes sur
l'instance ami1. Le lecteur utilise un lien M3U ; auparavant, seule France 24
fonctionnait selon le spectateur.

Les anciennes traces disponibles correspondent surtout au navigateur, dont une
lecture terminée par un POST /api/stop. Elles ne permettent pas d'attribuer la
coupure à Smart One. Le fournisseur est joignable, l'abonnement est actif et
n'avait aucune connexion occupée au début du contrôle.

Vérifications avant modification, depuis le VPS et avec FFmpeg :

- France 24 : 25 secondes décodées, sortie normale.
- TF1 : 160 secondes décodées via la façade lecteur, sortie normale ; état
  playing, aucune bascule de source et segments répondant en HTTP 200.
- Après déploiement : récupération du M3U public en HTTPS (HTTP 200),
  2 642 entrées, toutes en HLS sur l'adresse HTTPS de l'instance ; nouvelle
  lecture de TF1 pendant 35 secondes depuis une entrée de ce M3U, sortie
  normale. Le User-Agent du lecteur apparaît bien dans les nouveaux journaux.
- FFmpeg signale des timestamps audio non monotones autour des écrans de
  démarrage. Il termine les deux lectures ; cela ne prouve ni une panne ni
  la compatibilité du décodeur Smart One.

La coupure n'est pas reproduite. Ces essais ne couvrent ni le réseau du
spectateur ni son téléviseur. Aucun correctif de lecture spécifique à Smart One
n'est déclaré validé. Les journaux incluent désormais le User-Agent pour pouvoir
identifier les requêtes du lecteur lors d'une nouvelle reproduction.

## Protection des sources du relais et des logos

Avant correction, /api/relay validait seulement l'URL initiale, puis FFmpeg
pouvait suivre des redirections et des références HLS sans ce contrôle. Les
logos vérifiaient les redirections mais résolvaient à nouveau le nom au moment
de la connexion, laissant une fenêtre de changement DNS.

Le nouveau module egress.py :

- contrôle chaque destination et se connecte directement à l'IP validée ;
- conserve Host, SNI et la vérification du certificat HTTPS ;
- revalide toutes les redirections ;
- fait passer playlists, variantes, segments, clés et initialisations HLS par
  une passerelle locale, avec des URL signées et révocables par lecture ;
- borne la taille des playlists et les transferts simultanés ;
- limite les formats et protocoles d'entrée FFmpeg pour le relais.

Le réglage explicite relay_allow_private=true reste disponible pour un moteur
sur réseau domestique. Les logos utilisent le même client HTTP protégé.
Les paramètres ticket et token sont désormais masqués dans le journal d'accès.

## Validation et déploiement

104 tests automatisés passent sur une copie isolée du code sur le VPS.
Le test avec FFmpeg réel passe sur macOS et sur le VPS : échelle HLS,
remux, film préparé, lecture TS/HLS via la passerelle et encodage par un worker
relais. Les cas de redirection privée, IP validée, SNI, clé/segment privé,
URI HLS non HTTP ou sans guillemets et révocation sont couverts.

Les quatre modules Python concernés ont été déployés sur les instances
principale et ami1 après sauvegarde et vérification de l'absence de lecture.
Les deux services répondent après redémarrage ; les empreintes des fichiers de
configuration sont inchangées. Les catalogues et identifiants ne sont pas
modifiés. Aucune modification d'Apache ou des comptes fournisseur.

## Ajout d'abonnement : synchronisation automatique

L'ajout validait et enregistrait le fournisseur, puis attendait un clic manuel
sur Synchroniser. L'API programme désormais l'import immédiatement après la
sauvegarde, pour Xtream comme pour les liens M3U. Le traitement continue côté
serveur si la page est fermée ; le verrou existant sérialise les imports lorsqu'une
synchronisation est déjà en cours. Les identifiants générés sont aléatoires pour
éviter qu'un second ajout dans la même seconde remplace le premier.

L'interface annonce le démarrage automatique et conserve le journal de
progression. Les compteurs locaux sont relus même lorsque les informations du
panel sont en cache : un import terminé ne reste plus affiché comme vide pendant
cinq minutes. Versions des ressources web et du service worker incrémentées.

108 tests passent en local et dans une copie isolée du VPS, dont l'import après
ajout sans requête /api/sync, l'ajout pendant une synchronisation, le refus d'un
abonnement invalide et l'actualisation des compteurs malgré le cache.
Déployé sur la base principale et ami1 à ami4, avec sauvegarde, services
vérifiés et configurations conservées. Le catalogue d'ami3 était déjà importé ;
aucun abonnement n'a été recréé pour les essais.

## Rafraîchissement périodique des catalogues

Le serveur vérifie désormais chaque minute les échéances de ses abonnements.
Par défaut, il importe les catalogues de plus de six heures, ainsi que ceux
qui n'ont jamais été importés. Les dates SQLite servent de référence après un
redémarrage. `catalog_refresh_hours` règle cet intervalle (minimum une heure,
zéro pour désactiver uniquement la périodicité).

Le travail s'exécute dans le service Streamly, sans navigateur ni tâche Codex.
Le verrou existant sérialise les imports et l'échéance est revérifiée après
l'attente, afin de ne pas répéter une synchronisation manuelle qui vient de finir.
Les fournisseurs désactivés sont ignorés. Un échec d'import du direct garde la
copie précédente et impose quinze minutes entre deux tentatives automatiques.
L'accès au compte est vérifié avant l'import. Les imports des films et séries
et la reconstruction du guide suivent celui du direct.

119 tests passent sur une copie isolée du VPS. Les scénarios supplémentaires
couvrent les échéances, le redémarrage, un catalogue jamais importé, le verrou,
les échecs et le délai de nouvelle tentative, la désactivation/configuration,
le maintien du catalogue après une panne ou un refus d'authentification et la
prise en compte des ajouts/retraits de chaînes.

Déployé sur l'instance principale et ami1 à ami4 après 119 tests réussis en
local et sur la copie isolée du VPS. L'accès SSH a été rétabli en imposant
l'authentification par mot de passe. Sauvegarde des fichiers précédents dans
`/tmp/streamly-periodic-backup-lss7q66q` sur le VPS. Aucune lecture active
au moment des redémarrages ; les cinq services répondent avec un intervalle
de six heures. Les empreintes des configurations sont inchangées. Les
ressources web v33 et le service worker v15 servent l'indication de périodicité.

Premier cycle automatique observé sans appel manuel de synchronisation :
à 20 h 09 min 57 s UTC (Bamako), les catalogues échus de l'instance principale
et d'ami1 ont été réimportés, puis les films et séries traités sans erreur
signalée. Le direct principal passe de 27 926 à 28 055 chaînes ; ami1 conserve
3 674 chaînes. Les catalogues récents d'ami2 et ami3 sont correctement ignorés,
ainsi qu'ami4 sans fournisseur. Vérification des cinq API à 20 h 10 min 21 s.
