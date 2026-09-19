"""Client de l'API Xtream Codes.

Deux particularites constatees sur des panels reels et traitees ici :

  - Le panel peut refuser les requetes selon le User-Agent : on envoie
    systematiquement celui configure.
  - `get_*_categories` peut renvoyer une liste vide alors que les flux
    portent bien un `category_id`. On reconstruit alors le libelle des
    categories a partir des `group-title` de l'export M3U.
"""
import json
import re
import unicodedata
import urllib.parse
import urllib.request

# Tokens de qualite rencontres dans les noms de chaines, du moins bon au meilleur.
QUALITY_TOKENS = r"\b(4K|UHD|FHD|FULLHD|1080P?|720P?|576P|480P|360P|HD|SD|HEVC|H265|LQ|MQ|HQ|RAW)\b"

# Decorations purement typographiques ajoutees par certains panels autour des
# noms : « ####### CANAL+ LIVE ####### ».
_DECORATION_RE = re.compile(r"[#*=~_·•▬═■□◆♦]+")

QUALITY_HEIGHT = {
    "SD": 480, "LQ": 360, "MQ": 480, "HQ": 720,
    "360P": 360, "480P": 480, "576P": 576, "720P": 720, "1080P": 1080,
    "HEVC": 720, "H265": 720,   # souvent un 720p, malgre l'etiquette
    "HD": 720, "FHD": 1080, "FULLHD": 1080, "1080": 1080,
    "4K": 2160, "UHD": 2160,
}

_PREFIX_RE = re.compile(r"^\s*([A-Z]{2,4})\s*[-|:]\s*")


class XtreamError(RuntimeError):
    pass


class XtreamClient:
    def __init__(self, host, username, password, user_agent, timeout=120):
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.user_agent = user_agent
        self.timeout = timeout

    # ---------------------------------------------------------------- reseau

    def _get(self, url, raw=False):
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read()
        if raw:
            return data.decode("utf-8", "replace")
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise XtreamError("reponse non-JSON de %s" % self.host) from exc

    def _api(self, action=None, **params):
        q = {"username": self.username, "password": self.password}
        if action:
            q["action"] = action
        q.update(params)
        return self._get("%s/player_api.php?%s" % (self.host, urllib.parse.urlencode(q)))

    # ------------------------------------------------------------- endpoints

    def account_info(self):
        info = self._api()
        if not isinstance(info, dict) or str((info.get("user_info") or {}).get("auth")) != "1":
            raise XtreamError("authentification refusee")
        return info

    def live_streams(self):
        return self._api("get_live_streams") or []

    def live_categories(self):
        return self._api("get_live_categories") or []

    def vod_streams(self):
        return self._api("get_vod_streams") or []

    def vod_info(self, vod_id):
        return self._api("get_vod_info", vod_id=vod_id) or {}

    def series(self):
        return self._api("get_series") or []

    def series_categories(self):
        return self._api("get_series_categories") or []

    def series_info(self, series_id):
        """Saisons et episodes d'une serie. Un appel reseau par serie."""
        return self._api("get_series_info", series_id=series_id) or {}

    def m3u(self):
        """Export M3U complet. Lent (le panel le regenere a la volee)."""
        q = urllib.parse.urlencode({
            "username": self.username, "password": self.password,
            "type": "m3u_plus", "output": "mpegts",
        })
        return self._get("%s/get.php?%s" % (self.host, q), raw=True)

    # ------------------------------------------------------------------ URLs

    def live_url(self, stream_id):
        return "%s/%s/%s/%s" % (self.host, self.username, self.password, stream_id)

    def xmltv_url(self):
        q = urllib.parse.urlencode({"username": self.username, "password": self.password})
        return "%s/xmltv.php?%s" % (self.host, q)

    def hls_url(self, stream_id):
        return "%s/live/%s/%s/%s.m3u8" % (self.host, self.username, self.password, stream_id)

    def episode_url(self, episode_id, container):
        # Les episodes ne sont pas servis sous /movie/ : un panel renvoie 404
        # sur ce chemin, et l'erreur ne dit pas pourquoi.
        return "%s/series/%s/%s/%s.%s" % (
            self.host, self.username, self.password, episode_id, container)

    def _coerce_epg_items(self, payload):
        if not payload:
            return []
        candidates = payload
        if isinstance(payload, dict):
            for key in ("epg_listings", "epg", "programs", "data", "listings", "events", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
        if isinstance(candidates, dict):
            if isinstance(candidates.get("data"), list):
                candidates = candidates["data"]
            elif isinstance(candidates.get("programs"), list):
                candidates = candidates["programs"]
            else:
                return []
        if not isinstance(candidates, list):
            return []
        return [item for item in candidates if isinstance(item, dict)]

    def live_epg(self, stream_id, epg_channel_id=None):
        candidates = []
        if epg_channel_id:
            candidates.append(("get_short_epg", {"stream_id": str(epg_channel_id)}))
            candidates.append(("get_simple_data_table", {"stream_id": str(epg_channel_id)}))
        candidates.append(("get_short_epg", {"stream_id": str(stream_id)}))
        candidates.append(("get_simple_data_table", {"stream_id": str(stream_id)}))
        last_err = None
        for action, args in candidates:
            try:
                payload = self._api(action, **args)
            except Exception as exc:
                last_err = exc
                continue
            items = self._coerce_epg_items(payload)
            if items:
                return items
        if last_err is not None:
            raise last_err
        return []

    # ------------------------------------------------ reconstruction categories

    def category_names_from_m3u(self):
        """Associe stream_id -> group-title en lisant l'export M3U.

        Sert quand `get_live_categories` renvoie une liste vide.
        """
        text = self.m3u()
        mapping = {}
        blocks = re.findall(r'group-title="([^"]*)",[^\n]*\n(\S+)', text)
        for group, url in blocks:
            m = re.search(r"/(\d+)(?:\.\w+)?$", url.strip())
            if m:
                mapping[m.group(1)] = group
        return mapping


def parse_series_info(payload):
    """Extrait (resume, episodes) d'une reponse `get_series_info`.

    Le champ `episodes` n'a pas de forme stable : la plupart des panels
    renvoient un objet indexe par numero de saison, certains une liste de
    listes. Le numero de saison lui-meme est tantot dans l'episode, tantot
    seulement porte par la cle. On accepte les deux plutot que de renvoyer
    une serie vide sur un panel un peu different.
    """
    if not isinstance(payload, dict):
        return "", []
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    plot = info.get("plot") or info.get("description") or ""

    raw = payload.get("episodes")
    buckets = []
    if isinstance(raw, dict):
        buckets = list(raw.items())
    elif isinstance(raw, list):
        buckets = [(str(i + 1), group) for i, group in enumerate(raw)]

    episodes = []
    for season_key, group in buckets:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            try:
                episode_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            detail = item.get("info") if isinstance(item.get("info"), dict) else {}
            episodes.append({
                "episode_id": episode_id,
                "season": _as_int(item.get("season"), _as_int(season_key, 0)),
                "episode": _as_int(item.get("episode_num"), 0),
                "title": (item.get("title") or "").strip(),
                # Sans extension exacte, l'URL renvoie 404 : mp4 est le defaut
                # le plus frequent, mais on prend ce que le panel annonce.
                "container": (item.get("container_extension") or "mp4").lstrip("."),
                "duration": detail.get("duration") or "",
                "plot": detail.get("plot") or detail.get("description") or "",
            })
    episodes.sort(key=lambda e: (e["season"], e["episode"]))
    return plot, episodes


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ analyse des noms

def parse_name(name):
    """Extrait (langue, nom canonique, token de qualite) d'un nom de chaine.

    'FR| CANAL+ SPORT FHD' -> ('FR', 'CANAL SPORT', 'FHD')
    """
    # Certains panels ecrivent la qualite en exposants Unicode : « FRANCE 2 ᴴᴰ ».
    # Sans normalisation, ces caracteres ne sont pas reconnus comme des tokens
    # de qualite : la chaine forme un groupe a part, ne peut plus servir de
    # source de secours a sa jumelle, et apparait en double dans la liste.
    # NFKC ramene ᴴᴰ a HD, ʰᵉᵛᶜ a hevc, ᴿᴬᵂ a RAW.
    upper = unicodedata.normalize("NFKC", name or "").upper()
    upper = _DECORATION_RE.sub(" ", upper)
    m = _PREFIX_RE.match(upper)
    lang = m.group(1) if m else None

    body = _PREFIX_RE.sub("", upper)
    tokens = re.findall(QUALITY_TOKENS, body)
    quality = tokens[-1] if tokens else None

    canonical = re.sub(QUALITY_TOKENS, "", body)
    canonical = re.sub(r"\[[^\]]*\]", "", canonical)      # tags [BK], [4K]...
    canonical = " ".join("".join(c if c.isalnum() else " " for c in canonical).split())

    return lang, canonical, quality


def quality_height(token):
    """Hauteur approximative d'un token de qualite, pour choisir la source."""
    return QUALITY_HEIGHT.get(token or "", 0)


def is_backup(name):
    """Les flux de secours sont suffixes [BK], [BK1]... par les panels."""
    return bool(re.search(r"\[BK\d*\]", (name or "").upper()))
