"""Facade Xtream / M3U pour les lecteurs externes (TiviMate, Smarters, VLC).

Le lecteur recoit un hote, un identifiant et un mot de passe Streamly, jamais
ceux de l'abonnement d'origine. Le bouquet sort de SQLite : lister les chaines
ne touche ni le panel ni FFmpeg. L'encodage ne demarre qu'a l'ouverture reelle
d'une chaine.
"""
import hashlib
import threading
import time
import urllib.parse

UNCATEGORIZED = 'Autres'
INTENT_SECONDS = 30    # un master demande ouvre la chaine dans ce delai
ZAP_SECONDS = 0.8      # attente apres le master : un zap rapide n'encode rien
PLAYER_IDLE = 45       # sans requete, la chaine s'arrete et libere l'abonnement


class Evicted(Exception):
    """Un autre appareil du meme compte a ouvert une autre chaine."""


def _id31(text):
    # Les lecteurs Xtream attendent un entier, souvent signe sur 32 bits.
    return int(hashlib.sha1(text.encode('utf-8')).hexdigest()[:8], 16) & 0x7FFFFFFF


def stream_id(lang, canonical):
    """Numero stable d'une chaine : il survit aux synchros et aux changements
    de source, contrairement au stream_id du panel, propre a une variante."""
    return _id31('%s|%s' % (lang or '', canonical)) or 1


def category_id(name):
    return _id31(name) or 1


def owner(player):
    """Proprietaire des tickets d'un compte lecteur : une chaine a la fois."""
    return 'player:' + player['username']


def _attr(value):
    # Une valeur d'attribut EXTINF ne peut contenir ni guillemet ni saut de ligne.
    return str(value or '').replace('"', "'").replace('\r', ' ').replace('\n', ' ')


class PlayerFacade:
    def __init__(self, catalog, transcoder=None):
        self.catalog, self.transcoder = catalog, transcoder
        self._lock = threading.Lock()
        self._key = None
        self._channels, self._by_id, self._categories = [], {}, []
        self._live_lock = threading.Lock()
        self._intents = {}      # owner -> (id de chaine, instant du master)
        self._bindings = {}     # owner -> (id de chaine, ticket)
        self._owner_locks = {}
        self._slates = {}       # ticket -> ecran d'attente (voir slate())

    def invalidate(self):
        with self._lock:
            self._key = None

    def index(self):
        """(chaines, {id: chaine}, categories), reconstruits apres une synchro."""
        key = self.catalog.signature()
        with self._lock:
            if key != self._key:
                self._build()
                self._key = key
            return self._channels, self._by_id, self._categories

    def channel(self, sid):
        return self.index()[1].get(int(sid))

    def _build(self):
        channels, by_id, cat_ids = [], {}, {}
        for row in self.catalog.player_channels():
            sid = stream_id(row['lang'], row['canonical'])
            # Collision de hachage : rare, mais deux chaines ne doivent jamais
            # partager un numero. Le decalage est deterministe, l'ordre des
            # lignes l'etant aussi.
            while sid in by_id:
                sid = sid % 0x7FFFFFFF + 1
            name = (row['category'] or '').strip() or UNCATEGORIZED
            if name not in cat_ids:
                cid, taken = category_id(name), set(cat_ids.values())
                while cid in taken:
                    cid = cid % 0x7FFFFFFF + 1
                cat_ids[name] = cid
            channel = {'id': sid, 'lang': row['lang'], 'canonical': row['canonical'],
                       'name': row['label'] or row['canonical'], 'icon': row['icon'] or '',
                       'epg_id': row['epg_id'] or '', 'category': name,
                       'category_id': cat_ids[name]}
            by_id[sid] = channel
            channels.append(channel)
        channels.sort(key=lambda c: (c['category'].casefold(), c['name'].casefold(), c['id']))
        self._channels, self._by_id = channels, by_id
        self._categories = sorted(({'id': cid, 'name': name} for name, cid in cat_ids.items()),
                                  key=lambda c: c['name'].casefold())

    # ---------------------------------------------------------- direct

    def want(self, owner, sid):
        """Le lecteur a demande le master d'une chaine : c'est l'intention de
        la regarder. Seule la plus recente compte."""
        with self._live_lock:
            self._intents[owner] = (sid, time.time())

    def ticket(self, owner, sid, open_fn):
        """Ticket de lecture d'une chaine pour un compte lecteur.

        Le lecteur rafraichit la playlist d'une chaine toutes les deux
        secondes. Si ce rafraichissement suffisait a (r)ouvrir la chaine, deux
        appareils du meme compte sur deux chaines se la reprendraient a chaque
        fois, et FFmpeg redemarrerait en boucle sur l'unique connexion de
        l'abonnement. Une chaine ne s'ouvre donc que sur une intention
        fraiche (master demande), ou si le compte ne regarde plus rien (simple
        expiration apres une pause).
        """
        with self._live_lock:
            lock = self._owner_locks.setdefault(owner, threading.Lock())
        with lock:
            with self._live_lock:
                bound = self._bindings.get(owner)
                intent = self._intents.get(owner)
            fresh = bool(intent and intent[0] == sid and time.time() - intent[1] < INTENT_SECONDS)
            ticket = bound[1] if bound and bound[0] == sid else None
            if ticket and self.transcoder.ticket(ticket, owner):
                status = self.transcoder.status(ticket)
                # Une chaine en echec ne se relance que si on la redemande.
                if status and not (status['state'] == 'failed' and fresh):
                    return ticket
            if ticket:
                self.transcoder.release(ticket, owner)
            if not fresh and self.transcoder.owner_streams(owner):
                raise Evicted()
            if fresh:
                wait = ZAP_SECONDS - (time.time() - intent[1])
                if wait > 0:
                    time.sleep(wait)
                with self._live_lock:
                    if self._intents.get(owner) != intent:
                        # L'utilisateur est deja passe a une autre chaine.
                        raise Evicted()
            ticket = open_fn()
            with self._live_lock:
                self._bindings[owner] = (sid, ticket)
                if self._intents.get(owner) == intent:
                    self._intents.pop(owner, None)
            return ticket

    def slate(self, ticket):
        """Etat de l'ecran d'attente d'une lecture : debut, segments d'attente
        montres, premier vrai segment servi. Sert a numeroter la playlist sans
        rupture entre l'attente et le direct."""
        with self._live_lock:
            if ticket not in self._slates and len(self._slates) > 64:
                alive = {t for _, t in self._bindings.values()}
                self._slates = {k: v for k, v in self._slates.items() if k in alive}
            return self._slates.setdefault(ticket, {'opened': time.time(), 'count': 0, 'first': None})

    # ---------------------------------------------------------- M3U

    def m3u(self, player, base):
        user = urllib.parse.quote(player['username'], safe='')
        password = urllib.parse.quote(player['password'], safe='')
        lines = ['#EXTM3U']
        for c in self.index()[0]:
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" group-title="%s",%s' % (
                _attr(c['epg_id']), _attr(c['name']), _attr(c['icon']), _attr(c['category']),
                _attr(c['name'])))
            # Toujours du HLS : un flux TS a debit fixe ne s'adapterait plus a
            # la connexion du lecteur, ce qui est tout l'interet de Streamly.
            lines.append('%s/live/%s/%s/%d.m3u8' % (base, user, password, c['id']))
        return '\n'.join(lines) + '\n'

    def xmltv(self):
        # Pas de grille locale : aller chercher celle du panel lierait un scan
        # du lecteur a un gros telechargement. Le guide court passe par l'API.
        return '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="Streamly"></tv>\n'

    # ---------------------------------------------------- player_api

    def api(self, player, action, params, base, active=0):
        one = lambda k: (params.get(k) or [''])[0]
        channels, _, categories = self.index()
        if not action:
            return self.account(player, base, active)
        if action == 'get_live_categories':
            return [{'category_id': str(c['id']), 'category_name': c['name'], 'parent_id': 0}
                    for c in categories]
        if action == 'get_live_streams':
            wanted = one('category_id')
            return [{
                'num': n, 'name': c['name'], 'stream_type': 'live', 'stream_id': c['id'],
                'stream_icon': c['icon'], 'epg_channel_id': c['epg_id'], 'added': '0',
                'is_adult': '0', 'category_id': str(c['category_id']),
                'category_ids': [c['category_id']], 'custom_sid': '', 'tv_archive': 0,
                # Vide expres : une URL ici ferait lire l'origine sans Streamly.
                'direct_source': '', 'tv_archive_duration': 0,
            } for n, c in enumerate(
                (c for c in channels if not wanted or str(c['category_id']) == wanted), 1)]
        if action in ('get_short_epg', 'get_simple_data_table'):
            return {'epg_listings': []}
        # Films et series : pas encore servis aux lecteurs externes.
        return []

    def account(self, player, base, active=0):
        parsed = urllib.parse.urlsplit(base)
        https = parsed.scheme == 'https'
        port = str(parsed.port or (443 if https else 80))
        now = time.time()
        return {
            'user_info': {
                'username': player['username'], 'password': player['password'],
                'message': '', 'auth': 1, 'status': 'Active', 'exp_date': None,
                'is_trial': '0', 'active_cons': str(active), 'created_at': None,
                'max_connections': '1', 'allowed_output_formats': ['m3u8'],
            },
            'server_info': {
                'url': parsed.hostname or '', 'port': '80' if https else port,
                'https_port': port if https else '443',
                'server_protocol': 'https' if https else 'http', 'rtmp_port': '0',
                'timezone': 'UTC', 'timestamp_now': int(now),
                'time_now': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now)),
            },
        }
