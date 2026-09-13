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
import urllib.parse
import urllib.request

# Tokens de qualite rencontres dans les noms de chaines, du moins bon au meilleur.
QUALITY_TOKENS = r"\b(4K|UHD|FHD|FULLHD|1080P?|720P?|576P|480P|360P|HD|SD|HEVC|H265|LQ|MQ|HQ)\b"

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

    def hls_url(self, stream_id):
        return "%s/live/%s/%s/%s.m3u8" % (self.host, self.username, self.password, stream_id)

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


# ------------------------------------------------------------ analyse des noms

def parse_name(name):
    """Extrait (langue, nom canonique, token de qualite) d'un nom de chaine.

    'FR| CANAL+ SPORT FHD' -> ('FR', 'CANAL SPORT', 'FHD')
    """
    upper = (name or "").upper()
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
