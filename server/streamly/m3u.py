"""Playlists M3U : lien direct, sans identifiants Xtream.

Beaucoup d'abonnements ne donnent qu'une URL de playlist. Deux cas se
presentent derriere cette meme forme, et les confondre coute cher :

  - Un lien `get.php?username=...&password=...` est en realite un panel
    Xtream. Le traiter comme une playlist plate ferait perdre l'EPG, les
    films et le rattrapage des categories. On extrait donc les identifiants
    et on enregistre un vrai provider Xtream.
  - Une playlist quelconque (fichier statique, agregateur, lien d'un
    fournisseur tiers). La seule source de verite est alors le fichier
    lui-meme : chaque entree porte son URL de lecture, qu'il faut conserver
    puisqu'aucune regle ne permet de la reconstruire.
"""
import hashlib
import re
import urllib.parse
import urllib.request

# Attributs d'une ligne #EXTINF : tvg-id="..." group-title="..." etc.
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_EXTINF_RE = re.compile(r'^#EXTINF:(?P<dur>-?[\d.]+)\s*(?P<attrs>[^,]*),(?P<name>.*)$')

# Une entree dont l'URL pointe un fichier ou un chemin de film n'est pas une
# chaine : l'importer en direct donnerait une lecture qui s'arrete toute seule.
_VOD_RE = re.compile(r'/(movie|series)/|\.(mp4|mkv|avi|m4v)(\?|$)', re.IGNORECASE)


class PlaylistError(RuntimeError):
    pass


def parse(text):
    """Decoupe une playlist en entrees {name, url, group, logo, epg_id}."""
    entries = []
    pending = None
    group_override = None
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF:'):
            m = _EXTINF_RE.match(line)
            if not m:
                pending = None
                continue
            attrs = dict(_ATTR_RE.findall(m.group('attrs')))
            pending = {
                'name': (m.group('name') or '').strip() or attrs.get('tvg-name', ''),
                'group': attrs.get('group-title', ''),
                'logo': attrs.get('tvg-logo', ''),
                'epg_id': attrs.get('tvg-id', ''),
            }
        elif line.startswith('#EXTGRP:'):
            # Certaines playlists placent le groupe sur sa propre ligne.
            group_override = line.split(':', 1)[1].strip()
        elif line.startswith('#'):
            continue
        elif pending is not None:
            if group_override and not pending['group']:
                pending['group'] = group_override
            pending['url'] = line
            entries.append(pending)
            pending, group_override = None, None
    return entries


def stream_id(url):
    """Identifiant stable pour une entree, derive de son URL.

    L'ordre d'une playlist n'est pas garanti d'une synchro a l'autre : un
    index de position ferait changer l'identifiant de chaque chaine des
    qu'une entree est ajoutee en tete.
    """
    return int(hashlib.sha1(url.encode('utf-8', 'replace')).hexdigest()[:12], 16)


def detect_xtream(url):
    """Reconnait un lien de panel Xtream et en extrait (host, user, password).

    Renvoie None si l'URL n'a pas cette forme.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    query = urllib.parse.parse_qs(parsed.query)
    user = (query.get('username') or [''])[0]
    password = (query.get('password') or [''])[0]
    if not (user and password):
        return None
    if not parsed.path.rstrip('/').endswith(('get.php', 'player_api.php', 'panel_api.php')):
        return None
    return '%s://%s' % (parsed.scheme, parsed.netloc), user, password


class M3UClient:
    """Expose la meme surface que XtreamClient, sur une playlist plate.

    Le catalogue et la synchronisation ne connaissent qu'une interface : en la
    respectant ici, une playlist se synchronise par le meme chemin qu'un
    panel, sans branchement supplementaire cote appelant.
    """

    def __init__(self, url, user_agent, timeout=120):
        self.url = url
        self.user_agent = user_agent
        self.timeout = timeout
        self._entries = None

    # ---------------------------------------------------------------- reseau

    def _fetch(self):
        if self._entries is not None:
            return self._entries
        req = urllib.request.Request(self.url, headers={'User-Agent': self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
        except Exception as exc:
            raise PlaylistError('playlist illisible : %s' % exc) from exc
        text = data.decode('utf-8', 'replace')
        if '#EXTINF' not in text:
            raise PlaylistError('ce lien ne renvoie pas une playlist M3U')
        self._entries = [e for e in parse(text) if not _VOD_RE.search(e['url'])]
        if not self._entries:
            raise PlaylistError('playlist sans aucune chaine exploitable')
        return self._entries

    # ------------------------------------------------------------- endpoints

    def account_info(self):
        entries = self._fetch()
        return {'user_info': {'auth': 1, 'status': 'Active',
                              'max_connections': 1, 'channels': len(entries)}}

    def live_streams(self):
        streams = []
        for e in self._fetch():
            streams.append({
                'stream_id': stream_id(e['url']),
                'name': e['name'],
                'category_id': e['group'] or 'divers',
                'stream_icon': e['logo'],
                'epg_channel_id': e['epg_id'],
                'url': e['url'],
            })
        return streams

    def live_categories(self):
        seen = []
        for e in self._fetch():
            group = e['group'] or 'divers'
            if group not in seen:
                seen.append(group)
        return [{'category_id': g, 'category_name': g} for g in seen]

    # Une playlist plate ne porte ni films ni grille de programmes. On renvoie
    # du vide plutot que de lever : l'appelant traite les deux pareil, et une
    # playlist sans EPG reste parfaitement utilisable.
    def vod_streams(self):
        return []

    def vod_info(self, vod_id):
        return {}

    def series(self):
        return []

    def series_categories(self):
        return []

    def series_info(self, series_id):
        return {}

    def live_epg(self, stream_id, epg_channel_id=None):
        return []

    def category_names_from_m3u(self):
        return {}

    def live_url(self, sid):
        for e in self._fetch():
            if stream_id(e['url']) == int(sid):
                return e['url']
        raise PlaylistError('chaine absente de la playlist')

    def hls_url(self, sid):
        return self.live_url(sid)
