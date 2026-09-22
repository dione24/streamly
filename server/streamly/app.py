"""HTTP API, device cookies and short-lived media tickets. Admin-only writes."""
import gzip
import json
import math
import mimetypes
import os
import posixpath
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as cfgmod
from .catalog import Catalog
from .transcoder import Transcoder, CapacityError, MODE_CEILINGS, capped_ceiling
from .auth import Sessions
from . import player as playermod
from .epg import Guide
from . import relay as relaymod
from .logos import Logos
from .vod import Movies
from .m3u import M3UClient, PlaylistError, detect_xtream
from .xtream import XtreamClient, parse_series_info
from .refresh import CatalogRefresh, catalog_due, refresh_seconds
from .metadata import Omdb, panel_extra, omdb_due

STARTUP_TIMEOUT = 25      # secondes d'attente de la premiere playlist
ACCESS_LOG = []           # journal d'acces circulaire, expose via /api/access
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
# Un appareil associe ne fait que lire : rien d'autre de l'API ne lui est ouvert.
DEVICE_GET = ("/api/me", "/api/playback")
DEVICE_POST = ("/api/relay", "/api/stop")
# Donnees aleatoires, donc incompressibles : un proxy qui compresse ne fausse pas la mesure.
SPEEDTEST_PAYLOAD = os.urandom(200000)
GZIP_MIN_BYTES = 1400     # en dessous, l'en-tete gzip coute plus qu'il ne gagne
PLAYER_PATHS = ("/get.php", "/player_api.php", "/panel_api.php", "/xmltv.php")
# Tickets, chemins de lecteur et identifiants en query ne vont jamais au journal.
REDACTIONS = (
    (re.compile(r"(/(?:s|v|media)/)[^/ ?]+"), r"\1[redacted]"),
    (re.compile(r"(/live/)[^/ ?]+/[^/ ?]+"), r"\1[redacted]"),
    (re.compile(r"(\"[A-Z]+ /)(?!live/)[^/ ?\"]+/[^/ ?\"]+(/\d+(?:\.[a-z0-9]+)?[ ?])"), r"\1[redacted]\2"),
    (re.compile(r"((?:username|password|ticket|token)=)[^& \"\\]+", re.I), r"\1[redacted]"),
)


def _as_bool(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def _parse_epg_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            try:
                return int(text)
            except ValueError:
                return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(time.mktime(time.strptime(text[:19], fmt)))
            except (ValueError, OverflowError):
                continue
        try:
            return int(value.replace("Z", "")[:10])
        except (AttributeError, ValueError, TypeError):
            return None
    return None


def _panel_int(value):
    """Entier tolerant, ou None. Les panels renvoient des nombres en chaine,
    des chaines vides, ou `null` — et 0 n'a pas le meme sens qu'inconnu."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _duration_seconds(text):
    """« 01:42:10 » en secondes ; 0 si le panel ne donne rien d'exploitable."""
    try:
        parts = [int(float(x)) for x in str(text or '').split(':')]
    except ValueError:
        return 0
    if len(parts) < 2:
        return 0
    total = 0
    for n in parts:
        total = total * 60 + n
    return total


_WEB_CACHE = {}
_WEB_LOCK = threading.Lock()


def _web_file(full, mtime, packed=False):
    """Fichier de l'interface, et sa version gzip, gardes en memoire.

    Compresser app.js a chaque visite couterait plus que l'envoyer : on le
    fait une fois par version du fichier.
    """
    key = (full, mtime, packed)
    with _WEB_LOCK:
        if key in _WEB_CACHE:
            return _WEB_CACHE[key]
    with open(full, "rb") as fh:
        data = fh.read()
    if packed:
        data = gzip.compress(data, 9)
    with _WEB_LOCK:
        for old in [k for k in _WEB_CACHE if k[0] == full and k[1] != mtime]:
            del _WEB_CACHE[old]
        _WEB_CACHE[key] = data
    return data


class State:
    def __init__(self):
        self.cfg = cfgmod.load()
        self.catalog = Catalog(os.path.join(cfgmod.DATA_DIR, "catalog.db"))
        # Un abonnement retire de la configuration laissait ses chaines dans le
        # catalogue : elles restaient proposees alors qu'aucune source ne
        # pouvait plus les servir. On repart toujours d'un catalogue coherent.
        dropped = self.catalog.purge_absent([p["id"] for p in self.cfg.get("providers", [])])
        if dropped:
            print("catalogue purge des providers absents : %s" % dropped, flush=True)
        self.transcoder = Transcoder(self.cfg, cfgmod.HLS_DIR, cfgmod.LOG_DIR, state_dir=cfgmod.DATA_DIR)
        self.sessions = Sessions(self.cfg, os.path.join(cfgmod.DATA_DIR, "sessions.json"))
        self.player = playermod.PlayerFacade(self.catalog, self.transcoder)
        self.guide = Guide(os.path.join(cfgmod.DATA_DIR, "guide.xml.gz"), self.guide_sources,
                           lambda: {c["epg_id"] for c in self.player.index()[0]},
                           self.cfg.get("epg_refresh_hours", 6),
                           log=lambda msg: print(msg, flush=True))
        self.guide.ensure_fresh()
        self.devices = relaymod.Devices(self.cfg, cfgmod.save)
        self.logos = Logos(os.path.join(cfgmod.DATA_DIR, "logos"), self.catalog.has_icon,
                           self.cfg.get("user_agent", "VLC/3.0.20"),
                           _as_bool(self.cfg.get("relay_allow_private")))
        self.movies = Movies(self.cfg, os.path.join(cfgmod.DATA_DIR, "movies"), self.catalog, self.transcoder)
        # Notes IMDb / Rotten Tomatoes : seulement si une cle OMDb est configuree.
        self.omdb = Omdb(self.cfg.get("omdb_api_key"))
        self.sync_lock = threading.Lock()
        self.sync_log = []
        self._details, self._details_lock = {}, threading.Lock()
        self.catalog_refresh = CatalogRefresh(
            self.cfg, self.catalog.stats,
            lambda pid: _run_sync(pid, self, due_only=True), self.sync_lock.locked)

    def provider(self, pid):
        for p in self.cfg.get("providers", []):
            if p.get("id") == pid:
                return p
        return None

    def client(self, provider):
        ua = self.cfg.get("user_agent", "VLC/3.0.20")
        if provider.get("kind") == "m3u":
            return M3UClient(provider["url"], ua)
        return XtreamClient(provider["host"], provider["username"],
                            provider["password"], ua)

    def provider_details(self, provider, force=False):
        """Etat d'un abonnement : validite, connexions, formats.

        Interroger le panel a chaque affichage des reglages le ferait a
        chaque rafraichissement, soit toutes les huit secondes : on garde
        la reponse quelques minutes.
        """
        pid = provider["id"]
        ttl = float(self.cfg.get("account_cache_seconds", 300))
        with self._details_lock:
            cached = self._details.get(pid)
            if cached and not force and time.time() - cached["at"] < ttl:
                # Le cache concerne le panel, pas le catalogue local qui peut
                # venir d'etre importe par la synchronisation automatique.
                return dict(cached["data"], catalog=self.catalog.provider_counts(pid))
        data = {"id": pid, "kind": provider.get("kind", "xtream"), "reachable": False}
        try:
            info = self.client(provider).account_info()
        except Exception as exc:
            data["error"] = str(exc)[:200]
        else:
            user = info.get("user_info") or {}
            server = info.get("server_info") or {}
            data.update({
                "reachable": True,
                "status": user.get("status") or "",
                "trial": str(user.get("is_trial") or "") == "1",
                # exp_date vide ou 0 = sans echeance, ce qui n'est pas la meme
                # chose qu'une date inconnue : on distingue None de 0.
                "expires_at": _panel_int(user.get("exp_date")),
                "created_at": _panel_int(user.get("created_at")),
                "active_connections": _panel_int(user.get("active_cons")) or 0,
                "max_connections": _panel_int(user.get("max_connections")) or 0,
                "formats": user.get("allowed_output_formats") or [],
                "timezone": server.get("timezone") or "",
            })
        data["catalog"] = self.catalog.provider_counts(pid)
        with self._details_lock:
            self._details[pid] = {"at": time.time(), "data": data}
        return data

    def guide_sources(self):
        """Guides XMLTV des panels Xtream actifs. Une playlist M3U n'en a pas."""
        return [(p["id"], self.client(p).xmltv_url(), self.cfg.get("user_agent", "VLC/3.0.20"))
                for p in self.cfg.get("providers", [])
                if p.get("enabled", True) and p.get("kind") != "m3u"]

    def candidate_urls(self, provider_id, stream_id):
        """URLs a essayer pour une chaine, dans l'ordre.

        La source demandee d'abord, puis les autres variantes de la meme
        chaine : flux principaux avant flux de secours, et toutes providers
        confondus. C'est ce qui permet de basculer quand un flux lache en
        plein direct sans que l'utilisateur ait a chercher une autre entree.
        """
        urls, seen = [], set()

        def add(pid, sid, url=None):
            provider = self.provider(pid)
            if not provider or not provider.get("enabled", True) or (pid, sid) in seen:
                return
            seen.add((pid, sid))
            # Une entree de playlist n'a pas d'URL reconstructible : celle
            # enregistree a la synchro est la seule utilisable. On evite ainsi
            # de retelecharger la playlist a chaque lecture.
            if not url and provider.get("kind") == "m3u":
                row = self.catalog.channel(pid, sid) or {}
                url = row.get("url")
                if not url:
                    return
            urls.append({"provider": pid, "url": url or self.client(provider).live_url(sid)})

        first = self.catalog.channel(provider_id, int(stream_id)) or {}
        add(provider_id, int(stream_id), first.get("url"))

        channel = self.catalog.channel(provider_id, int(stream_id))
        if channel and channel.get("canonical"):
            target = int(self.cfg.get("preferred_source_height", 720))
            alts = self.catalog.sources(channel.get("lang"), channel["canonical"])
            alts.sort(key=lambda s: (s["is_backup"],
                                     abs((s["height"] or 0) - target)))
            for alt in alts:
                add(alt["provider_id"], alt["stream_id"], alt.get("url"))
        return urls


STATE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Streamly"

    def handle_one_request(self):
        # Une connexion persistante sert plusieurs requetes avec le meme objet,
        # et un proxy partage ses connexions entre visiteurs : sans cette
        # remise a zero, la session (et le cookie a poser) d'une requete
        # passait a la suivante, donc a quelqu'un d'autre.
        self._request_session = None
        self._cookie = None
        super().handle_one_request()

    def log_message(self, fmt, *args):
        # Journal d'acces minimal : indispensable pour distinguer « la requete
        # n'arrive pas » de « le serveur repond mal ».
        text = fmt % args
        # Distinguer une TV, un navigateur et nos sondes dans le diagnostic.
        # JSON echappe les retours a la ligne d'un User-Agent malveillant.
        agent = self.headers.get('User-Agent', '') if hasattr(self, 'headers') else ''
        text += ' ua=' + json.dumps(agent[:160], ensure_ascii=True)
        for pattern, replacement in REDACTIONS:
            text = pattern.sub(replacement, text)
        line = "%s  %s  %s" % (time.strftime("%H:%M:%S"), self._client_ip(), text)
        ACCESS_LOG.append(line)
        del ACCESS_LOG[:-400]

    # ------------------------------------------------------------ reponses

    def _raw(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        cookie = getattr(self, "_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        headers = {"Cache-Control": "no-store"}
        if extra:
            headers.update(extra)
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # Une page de catalogue ou l'historique pesent quelques dizaines de Ko :
        # compresses, ils arrivent bien plus vite sur une connexion mobile.
        if len(body) > GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            body = gzip.compress(body, 5)
            headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        self._raw(code, body, "application/json; charset=utf-8", headers)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _compact(self, obj):
        # Le bouquet complet pese plusieurs Mo : pas d'indentation ici.
        self._listing(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                      "application/json; charset=utf-8")

    def _listing(self, body, ctype, extra=None):
        """Catalogue pour un lecteur externe, compresse s'il l'accepte.

        Le bouquet complet fait 5 a 8 Mo en clair, environ six fois moins en
        gzip : c'est a chaque rafraichissement de la liste dans TiviMate.
        """
        headers = dict(extra or {}, **{"Cache-Control": "no-store", "Vary": "Accept-Encoding"})
        body = body.encode("utf-8")
        accepted = (self.headers.get("Accept-Encoding") or "").lower()
        if len(body) > GZIP_MIN_BYTES and "gzip" in accepted:
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
        self._raw(200, body, ctype, headers)

    # ---------------------------------------------------------- adresse

    def _behind_proxy(self):
        # Les en-tetes X-Forwarded-* ne sont crus que si Streamly n'ecoute
        # qu'en local : il n'est alors joignable qu'a travers le proxy.
        # trust_proxy couvre la transition : proxy HTTPS en place, port HTTP
        # encore ouvert. Seule une requete venue de la boucle locale est crue.
        return ((STATE.cfg.get("listen_host") in LOCAL_HOSTS or _as_bool(STATE.cfg.get("trust_proxy")))
                and self.client_address[0] in ("127.0.0.1", "::1"))

    def _client_ip(self):
        """Adresse du client. Derriere Apache, toutes les requetes viennent de
        127.0.0.1 : la limite de tentatives bloquerait tout le monde a la fois."""
        headers = getattr(self, "headers", None)
        if headers is not None and self._behind_proxy():
            forwarded = (headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if forwarded:
                return forwarded
        return self.client_address[0]

    def _public_base(self):
        """Adresse que les lecteurs doivent appeler, sans barre finale."""
        configured = (STATE.cfg.get("public_url") or "").strip().rstrip("/")
        if configured:
            return configured
        proto = "https" if STATE.cfg.get("secure_cookies") else "http"
        host = self.headers.get("Host") or ""
        if self._behind_proxy():
            proto = (self.headers.get("X-Forwarded-Proto") or proto).split(",")[0].strip()
            host = (self.headers.get("X-Forwarded-Host") or host).split(",")[0].strip()
        if proto not in ("http", "https"):
            proto = "http"
        if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?", host or ""):
            host = "%s:%s" % (self.server.server_address[0], self.server.server_address[1])
        return "%s://%s" % (proto, host)

    # ----------------------------------------------------- lecteurs externes

    def _serve_player(self, path, params):
        """get.php, player_api.php, xmltv.php : ce qu'attend un lecteur Xtream.

        Identifiants du compte lecteur, jamais le jeton admin ni ceux du
        panel d'origine. Aucune de ces routes ne demarre FFmpeg.
        """
        one = lambda k: (params.get(k) or [""])[0]
        player = STATE.sessions.player(one("username"), one("password"), self._client_ip())
        if path in ("/player_api.php", "/panel_api.php"):
            if not player:
                # Contrat Xtream : un refus reste un 200. Smarters traite un
                # 401 comme un serveur injoignable.
                return self._compact({"user_info": {"auth": 0}})
            if one("action") in ("get_short_epg", "get_simple_data_table"):
                return self._compact({"epg_listings": self._player_epg(one("stream_id"), one("limit"))})
            active = STATE.transcoder.owner_streams(playermod.owner(player))
            return self._compact(STATE.player.api(player, one("action"), params,
                                                  self._public_base(), active))
        if not player:
            return self._raw(401, "identifiants refusés\n", "text/plain; charset=utf-8",
                             {"Cache-Control": "no-store"})
        if path == "/get.php":
            return self._listing(STATE.player.m3u(player, self._public_base()),
                                 "audio/x-mpegurl; charset=utf-8",
                                 {"Content-Disposition": 'inline; filename="streamly.m3u"'})
        STATE.guide.ensure_fresh()
        packed = STATE.guide.read()
        if packed is None:
            # Premier demarrage : le guide se construit en tache de fond.
            return self._listing(STATE.player.xmltv(), "application/xml; charset=utf-8")
        headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
        if "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            headers["Content-Encoding"] = "gzip"
            return self._raw(200, packed, "application/xml; charset=utf-8", headers)
        return self._raw(200, gzip.decompress(packed), "application/xml; charset=utf-8", headers)

    def _player_epg(self, sid, limit):
        """Guide court d'une chaine, tel que le panel le renvoie (titres en
        base64, comme l'attendent les lecteurs Xtream). Vide en cas d'echec :
        un guide absent ne doit pas bloquer la lecture."""
        try:
            channel = STATE.player.channel(sid)
        except (TypeError, ValueError):
            channel = None
        if not channel:
            return []
        best = STATE.catalog.pick_source(channel["lang"], channel["canonical"],
                                         int(STATE.cfg.get("preferred_source_height", 720)))
        provider = STATE.provider(best["provider_id"]) if best else None
        if not provider or provider.get("kind") == "m3u":
            return []
        try:
            items = STATE.client(provider).live_epg(best["stream_id"], epg_channel_id=channel["epg_id"] or None)
        except Exception:
            return []
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = 0
        return items[:count] if count > 0 else items

    def _serve_live(self, path):
        """/live/{user}/{pass}/{id}.m3u8 et /live/{user}/{pass}/{id}/{niveau}.m3u8.

        L'URL d'une chaine reste la meme d'une lecture a l'autre ; le ticket
        interne, lui, change. Le master ne demarre rien : les lecteurs le
        demandent aussi au survol. Seule la playlist d'un niveau ouvre la
        chaine, et les segments passent ensuite par /s/{ticket}/.
        """
        parts = path[len("/live/"):].split("/")
        if len(parts) not in (3, 4):
            return self._err(404, "flux introuvable")
        player = STATE.sessions.player(parts[0], parts[1], self._client_ip())
        if not player:
            return self._raw(401, "identifiants refusés\n", "text/plain; charset=utf-8",
                             {"Cache-Control": "no-store"})
        owner = playermod.owner(player)
        ceiling = capped_ceiling(STATE.cfg, MODE_CEILINGS.get(player.get("mode"), MODE_CEILINGS["balanced"]))
        mpegurl = "application/vnd.apple.mpegurl"
        match = re.fullmatch(r"(\d{1,10})(?:\.(m3u8|ts))?", parts[2]) if len(parts) == 3 else \
            re.fullmatch(r"(\d{1,10})", parts[2])
        channel = STATE.player.channel(match[1]) if match else None
        if not channel:
            return self._err(404, "chaîne introuvable")

        if len(parts) == 3:
            if match[2] != "m3u8":
                # Un flux TS a debit fixe ne s'adapterait plus a la connexion :
                # on renvoie vers le HLS, que VLC et TiviMate suivent.
                location = "/live/%s/%s/%d.m3u8" % (urllib.parse.quote(parts[0], safe=""),
                                                    urllib.parse.quote(parts[1], safe=""), channel["id"])
                return self._raw(302, "", "text/plain; charset=utf-8",
                                 {"Location": location, "Cache-Control": "no-store"})
            if self.command == "GET":
                STATE.player.want(owner, channel["id"])
            body = STATE.transcoder.preview_master(ceiling, lambda i: "%d/%d.m3u8" % (channel["id"], i))
            return self._raw(200, body, mpegurl, {"Cache-Control": "no-store"})

        level = re.fullmatch(r"(\d{1,2})\.m3u8", parts[3])
        if not level or int(level[1]) not in STATE.transcoder.allowed_levels({"ceiling": ceiling}):
            return self._err(404, "qualité inconnue")
        if self.command == "HEAD":
            return self._raw(200, "", mpegurl, {"Cache-Control": "no-store"})
        best = STATE.catalog.pick_source(channel["lang"], channel["canonical"],
                                         int(STATE.cfg.get("preferred_source_height", 720)))
        if not best:
            return self._err(404, "chaîne introuvable")

        def open_channel():
            return STATE.transcoder.open(
                owner, (channel["lang"] or "") + "|" + channel["canonical"],
                STATE.candidate_urls(best["provider_id"], best["stream_id"]),
                channel["canonical"], ceiling, 0, idle=playermod.PLAYER_IDLE)
        try:
            ticket = STATE.player.ticket(owner, channel["id"], open_channel)
        except playermod.Evicted:
            return self._err(410, "chaîne reprise par un autre appareil de ce compte")
        except CapacityError as exc:
            # Pas de JSON 409 ici : un lecteur n'en lit rien. 503 + Retry-After
            # est ce qu'il sait traiter.
            return self._raw(503, str(exc) + "\n", "text/plain; charset=utf-8",
                             {"Retry-After": "10", "Cache-Control": "no-store"})
        return self._live_media(ticket, int(level[1]))

    def _live_media(self, ticket, level):
        """Playlist d'un niveau pour un lecteur externe (voir LiveTimeline).

        Repond toujours tout de suite : ecran de preparation pendant les 4 a
        11 s de demarrage de l'encodeur, direct ensuite, ecran de reprise si
        la source se fige, et ecran « fournisseur » si toutes les sources de
        la chaine ont echoue — plutot qu'une erreur brute ou une image figee.
        """
        t = STATE.transcoder.ticket(ticket, touch=False)
        status = STATE.transcoder.status(ticket)
        if not t or not status:
            return self._err(410, "lecture arrêtée")
        failed = status["state"] == "failed"
        generation, served, reals, target = status["generation"], level, [], 2
        if status["state"] != "starting":
            # Un remux n'a qu'une variante : on la sert quel que soit le
            # niveau annonce, faute de quoi le lecteur n'aurait rien.
            served = 0 if status["passthrough"] else level
            if served not in STATE.transcoder.allowed_levels(t) and not failed:
                return self._err(404, "qualité indisponible")
            try:
                with open(os.path.join(STATE.transcoder.hls_dir, t["key"],
                                       "g%d_s_%d.m3u8" % (generation, served)), encoding="utf-8") as fh:
                    head, segments = _hls_segments(fh.read())
            except OSError:
                head, segments = [], []
            for line in head:
                if line.startswith("#EXT-X-TARGETDURATION:"):
                    target = int(line.split(":")[1])
            for segment in segments:
                number = re.search(r"_(\d+)\.ts$", segment[-1])
                duration = re.search(r"#EXTINF:([\d.]+)", "\n".join(segment))
                if number and duration:
                    reals.append((generation, int(number[1]), float(duration[1])))
        timeline = STATE.player.timeline(ticket)
        with timeline.lock:
            # Fige au-dela de trois segments sans nouveaute : la source hoquette.
            timeline.sync(time.time(), failed, reals, max(8, 3 * target),
                          forced_level=0 if status["passthrough"] else None)
            body = timeline.render(lambda gen, number, forced: "/s/%s/g%d_%d_%09d.ts" % (
                ticket, gen, level if forced is None else forced, number))
        return self._raw(200, body, "application/vnd.apple.mpegurl", {"Cache-Control": "no-store"})

    def _serve_slate(self, name):
        with open(os.path.join(ASSETS_DIR, name), "rb") as fh:
            self._raw(200, fh.read(), "video/mp2t", {"Cache-Control": "public, max-age=86400"})

    def _player_credentials(self, player):
        base = self._public_base()
        query = urllib.parse.urlencode({"username": player["username"], "password": player["password"],
                                        "type": "m3u_plus", "output": "m3u8"})
        return {"username": player["username"], "password": player["password"],
                "mode": player.get("mode", "balanced"), "server": base,
                "max_mode": STATE.cfg.get("max_mode") or "sport",
                "m3u_url": "%s/get.php?%s" % (base, query)}

    # -------------------------------------------------------------- jeton

    def _session_cookie(self, sid, remember=True):
        """Cookie de session.

        Sans Max-Age, le cookie meurt avec le navigateur : c'est ce que
        promet « Rester connecte sur cet appareil » quand la case est
        decochee. La session reste bornee cote serveur dans les deux cas.
        """
        secure = '; Secure' if STATE.cfg.get('secure_cookies') else ''
        age = '; Max-Age=604800' if remember else ''
        return 'streamly_session=%s; HttpOnly; SameSite=Strict; Path=/%s%s' % (sid, age, secure)

    def _clear_cookie(self):
        return 'streamly_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/'

    def _api_token(self):
        token = self.headers.get('X-Streamly-Token')
        if token:
            return token.strip()
        auth = self.headers.get('Authorization') or ''
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        return None

    def _session(self):
        session = getattr(self, "_request_session", None)
        if session is not None:
            return session
        return STATE.sessions.get(self.headers.get('Cookie'))

    def _api_authed(self, params):
        session = self._session() or None
        if session is not None:
            self._request_session = session
            return session
        token = self._api_token()
        if not token:
            self._request_session = None
            return None
        address = self._client_ip()
        device = None if STATE.sessions.blocked(address) else STATE.devices.find(token)
        if device:
            # L'app envoie son jeton a chaque requete : pas de cookie, et un
            # proprietaire stable pour « une lecture a la fois par appareil ».
            session = dict(id='device:' + device['id'], role='device')
            self._request_session = session
            return session
        try:
            sid, role = STATE.sessions.login(token, self._client_ip())
        except ValueError:
            self._request_session = None
            return None
        self._cookie = self._session_cookie(sid)
        session = dict(id=sid, role=role, account='')
        self._request_session = session
        return session

    def _admin(self):
        return (self._api_authed(None) or {}).get('role') == 'admin'

    # ------------------------------------------------------------ routage

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/media/"):
            return self._serve_prepared(path, params)
        if path.startswith("/s/"):
            return self._serve_stream(path)
        if path.startswith("/v/"):
            return self._serve_vod(path)
        if path in PLAYER_PATHS:
            return self._serve_player(path, params)
        if path in playermod.SLATES.values():
            return self._serve_slate(path.lstrip("/"))
        if path.startswith("/live/"):
            return self._serve_live(path)
        if re.fullmatch(r"/[^/]+/[^/]+/\d{1,10}(?:\.(?:ts|m3u8))?", path):
            # Forme courte des panels Xtream, /{user}/{pass}/{id}, sans
            # /live/ ni extension : beaucoup d'apps IPTV l'emploient.
            return self._serve_live("/live" + path)
        if path.startswith("/api/"):
            session = self._api_authed(params)
            if not session:
                return self._err(401, "jeton invalide ou absent")
            if session['role'] == 'device' and path not in DEVICE_GET:
                return self._err(403, "réservé à l'interface web")
            try:
                return self._api_get(path, params)
            except (ValueError, TypeError):
                return self._err(400, "paramètres invalides")
        return self._serve_web(path, params)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)
        origin = self.headers.get('Origin')
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
            return self._err(403, 'origine refusée')
        if path in ('/player_api.php', '/panel_api.php'):
            # Smarters envoie ses identifiants en formulaire, pas en JSON.
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16384:
                return self._err(413, 'requête trop grande')
            form = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
            return self._serve_player(path, dict(params, **form))
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length < 0 or length > 16384:
                return self._err(413, 'requête trop grande')
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            return self._err(400, 'corps JSON invalide')
        if path == '/api/login':
            try:
                sid, role = STATE.sessions.login(
                    body.get('token'), self._client_ip(),
                    username=body.get('username'), password=body.get('password'))
            except ValueError as exc:
                return self._err(401, str(exc))
            # La case est cochee par defaut dans l'interface ; une absence de
            # champ vaut donc « se souvenir », comme avant ce changement.
            self._cookie = self._session_cookie(sid, remember=_as_bool(body.get('remember', True)))
            return self._json({'role': role}, extra={'Cache-Control': 'no-store'})
        if path == '/api/pair':
            address = self._client_ip()
            if STATE.sessions.blocked(address):
                return self._err(429, 'Trop de tentatives. Réessayez dans cinq minutes.')
            paired = STATE.devices.pair(body.get('code'), body.get('name'))
            if not paired:
                STATE.sessions.failed(address)
                return self._err(401, 'Code inconnu ou expiré.')
            return self._json(paired)
        session = self._api_authed(params)
        if not session:
            return self._err(401, 'connexion requise')
        if session['role'] == 'device' and path not in DEVICE_POST:
            return self._err(403, "réservé à l'interface web")
        if path == '/api/logout':
            sid = self._api_authed(None)['id']
            STATE.transcoder.release_owner(sid)
            STATE.sessions.logout(sid)
            self._cookie = self._clear_cookie()
            return self._raw(200, '{}', 'application/json', {'Cache-Control': 'no-store'})
        if path.startswith('/api/providers') or path.startswith('/api/devices') or path in ('/api/sync', '/api/viewer-token', '/api/player-credentials', '/api/pair-code'):
            if not self._admin():
                return self._err(403, 'accès administrateur requis')
        try:
            return self._api_post(path, params, body)
        except (ValueError, TypeError):
            return self._err(400, 'paramètres invalides')

    # -------------------------------------------------------- fichiers web

    def _serve_web(self, path, params=None):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith("..") or os.path.isabs(rel):
            return self._err(403, "chemin refuse")
        full = os.path.join(cfgmod.WEB_DIR, rel)
        if not os.path.isfile(full):
            return self._err(404, "introuvable")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        stat = os.stat(full)
        etag = '"%x-%x"' % (int(stat.st_mtime), stat.st_size)
        # Une adresse versionnee (app.js?v=...) ne change jamais de contenu : le
        # navigateur la garde un an. Le reste (index.html, sw.js) est revalide a
        # chaque visite, sans retelecharger s'il n'a pas bouge (304). Sans cela,
        # une mise a jour du client reste invisible derriere le cache.
        cache = "public, max-age=31536000, immutable" if (params or {}).get("v") else "no-cache"
        headers = {"Cache-Control": cache, "ETag": etag, "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            return
        body = _web_file(full, stat.st_mtime)
        textual = ctype.startswith("text/") or ctype in ("application/javascript", "application/json",
                                                           "image/svg+xml", "application/manifest+json")
        if textual and len(body) > GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            body = _web_file(full, stat.st_mtime, packed=True)
            headers["Content-Encoding"] = "gzip"
        self._raw(200, body, ctype, headers)

    # -------------------------------------------------------------- flux

    def _serve_stream(self, path):
        parts = path.strip('/').split('/')
        if len(parts) != 3:
            return self._err(404, 'flux introuvable')
        _, ticket, fname = parts
        t = STATE.transcoder.ticket(ticket)
        if not t:
            return self._err(401, 'session de lecture expirée')
        status = STATE.transcoder.status(ticket)
        # Source en echec : les segments deja produits restent servis. Le
        # lecteur les a encore en file ; les refuser le faisait abandonner
        # avant d'atteindre l'ecran qui explique la panne.
        produced = fname != 'master.m3u8' and os.path.isfile(os.path.join(cfgmod.HLS_DIR, t['key'], fname))
        if not status or (status['state'] == 'failed' and not produced):
            return self._err(502, 'sources indisponibles')
        if fname == 'master.m3u8':
            deadline = time.time() + 10
            while status['state'] == 'starting' and time.time() < deadline:
                time.sleep(.1)
                status = STATE.transcoder.status(ticket)
                if not status:
                    return self._err(410, 'lecture arrêtée')
            return self._raw(200, STATE.transcoder.master_playlist(ticket), 'application/vnd.apple.mpegurl', {'Cache-Control': 'no-store'})
        match = re.fullmatch(r'g(\d+)_(?:s_(\d+)\.m3u8|a\.m3u8|(\d+)_\d+\.ts|a_\d+\.ts)', fname)
        if not match:
            return self._err(404, 'segment invalide')
        if fname.endswith('a.m3u8') or fname.endswith('a_') or '_a_' in fname:
            level = 0
        else:
            level = int(match[2] if match[2] is not None else match[3])
        if level not in STATE.transcoder.allowed_levels(t):
            return self._err(403, 'qualité supérieure au budget sélectionné')
        target = os.path.join(cfgmod.HLS_DIR, t['key'], fname)
        deadline = time.time() + STARTUP_TIMEOUT
        while not os.path.isfile(target) and time.time() < deadline:
            current = STATE.transcoder.status(ticket)
            if not current or current['state'] == 'failed' or current['generation'] != int(match[1]):
                return self._err(410, 'source renouvelée')
            time.sleep(.15)
        try:
            with open(target, 'rb') as fh:
                data = fh.read()
        except OSError:
            return self._err(504, 'source trop lente')
        if self.command != 'HEAD' and not STATE.transcoder.charge(ticket, len(data)):
            return self._err(402, 'budget vidéo atteint')
        self._raw(200, data, 'application/vnd.apple.mpegurl' if fname.endswith('.m3u8') else 'video/mp2t', {'Cache-Control': 'no-store'})

    # ---------------------------------------------------------------- VOD

    def _serve_vod(self, path):
        return self._err(410, 'Préparez une version adaptée depuis la fiche du film.')

    def _episode_source(self, pid, eid):
        """Fichier d'un episode. Les series ne sont pas servies sous /movie/."""
        provider = STATE.provider(pid)
        episode = STATE.catalog.episode_get(pid, eid) if provider else None
        if not provider or not provider.get('enabled', True) or not episode:
            raise ValueError('episode indisponible')
        if provider.get('kind') == 'm3u':
            raise ValueError('les series exigent un abonnement Xtream')
        container = (episode.get('container') or 'mp4').lstrip('.')
        if not re.fullmatch(r'[a-zA-Z0-9]+', container):
            raise ValueError('conteneur invalide')
        show = STATE.catalog.series_get(pid, episode['series_id']) or {}
        label = show.get('title') or 'Serie'
        if episode.get('season') or episode.get('episode'):
            label = '%s S%02dE%02d' % (label, episode.get('season') or 0, episode.get('episode') or 0)
        # Beaucoup de panels donnent comme titre le numero lui-meme : « S01E04 ».
        extra = (episode.get('title') or '').strip()
        code = '%s S%02dE%02d' % (show.get('title') or '', episode.get('season') or 0, episode.get('episode') or 0)
        if extra and not re.fullmatch(r'(?:S\d+\s*E\d+|(?:episode|épisode)\s*\d+)', extra, re.I) and extra not in code:
            label = '%s — %s' % (label, extra)
        # Movies.start() attend une fiche de film : un episode en presente une
        # equivalente, ce qui evite un second chemin de preparation.
        movie = {'provider_id': pid, 'stream_id': int(eid), 'title': label, 'kind': 'episode',
                 'container': container, 'bitrate': 0,
                 'duration': episode.get('duration') or ''}
        return movie, STATE.client(provider).episode_url(int(eid), container)

    def _movie_source(self, pid, sid):
        provider = STATE.provider(pid)
        movie = STATE.catalog.vod_get(pid, sid)
        if not provider or not provider.get('enabled', True) or not movie:
            raise ValueError('film indisponible')
        client = STATE.client(provider)
        container = (movie.get('container') or 'mp4').lstrip('.')
        if not re.fullmatch(r'[a-zA-Z0-9]+', container):
            raise ValueError('conteneur invalide')
        source = '%s/movie/%s/%s/%s.%s' % (client.host, client.username, client.password, int(sid), container)
        return movie, source

    def _serve_prepared(self, path, params):
        if not self._session():
            return self._err(401, 'connexion requise')
        parts = path.strip('/').split('/')
        if len(parts) != 3:
            return self._err(404, 'fichier introuvable')
        _, jid, name = parts
        job = STATE.movies.jobs.get(jid)
        if not job or not re.fullmatch(r'(master|q\d+)\.m3u8|q\d+\.mp4|q\d+_\d+\.ts|subtitles\.vtt', name):
            return self._err(404, 'préparation indisponible')
        progressive = job.get('format') == 2
        if not (job['state'] == 'ready' or (progressive and job['state'] == 'preparing' and job.get('playable'))):
            return self._err(404, 'préparation indisponible')
        job['last_access'] = time.time()
        target = os.path.join(STATE.movies.root, jid, name)
        if progressive:
            # Playlists calculees, segments attendus le temps de leur encodage.
            if name == 'master.m3u8':
                return self._raw(200, STATE.movies.master(job).encode(), 'application/vnd.apple.mpegurl', {'Cache-Control': 'no-cache'})
            if name.endswith('.m3u8'):
                rung = int(name[1:-5])
                if rung >= len(job.get('qualities') or []):
                    return self._err(404, 'qualité inconnue')
                return self._raw(200, STATE.movies.variant(job, rung).encode(), 'application/vnd.apple.mpegurl', {'Cache-Control': 'no-cache'})
            if name.endswith('.ts'):
                rung, n = (int(x) for x in name[1:-3].split('_'))
                target = STATE.movies.segment(jid, rung, n)
                if not target and n >= STATE.movies._total(job):
                    # Film plus court qu'annonce : fin de lecture, pas d'attente.
                    return self._err(404, 'fin du film')
                if not target:
                    return self._raw(503, b'segment en preparation', 'text/plain', {'Retry-After': '2', 'Cache-Control': 'no-store'})
            elif name == 'subtitles.vtt' and not job.get('subtitles_ready'):
                return self._err(404, 'sous-titres pas encore disponibles')
            elif name.endswith('.mp4') and job['state'] != 'ready':
                return self._err(404, 'téléchargement disponible à la fin de la préparation')
        try:
            fh = open(target, 'rb')
        except OSError:
            return self._err(404, 'fichier introuvable')
        with fh:
            size = os.fstat(fh.fileno()).st_size
            start, end, code = 0, size - 1, 200
            rng = self.headers.get('Range')
            if rng:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', rng)
                if not match or not any(match.groups()):
                    return self._raw(416, b'', 'application/octet-stream', {'Content-Range': 'bytes */%d' % size})
                if match[1]:
                    start = int(match[1]); end = min(size - 1, int(match[2])) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start > end or start >= size:
                    return self._raw(416, b'', 'application/octet-stream', {'Content-Range': 'bytes */%d' % size})
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', {'m3u8': 'application/vnd.apple.mpegurl', 'ts': 'video/mp2t', 'vtt': 'text/vtt', 'mp4': 'video/mp4'}[name.rsplit('.', 1)[-1]])
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'private, max-age=3600')
            if code == 206:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            if 'download' in params and name.endswith('.mp4'):
                self.send_header('Content-Disposition', 'attachment; filename="streamly-%sp.mp4"' % job['height'])
            self.end_headers()
            if self.command == 'HEAD':
                return
            fh.seek(start)
            left = end - start + 1
            try:
                while left:
                    chunk = fh.read(min(65536, left))
                    if not chunk: break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------------------------------------------------------- historique

    def _history_entry(self, body):
        """Ligne d'historique a partir de ce que le lecteur envoie.

        Titres et affiches des films et des episodes viennent du catalogue,
        pas du navigateur : l'accueil reste juste meme si le client se trompe.
        """
        text = lambda v, n=200: str(v or '').strip()[:n] or None
        kind = body.get('kind')
        # Bornes : une valeur infinie casserait le JSON de l'accueil.
        seconds = lambda v: min(max(0.0, float(v or 0)), 172800.0) if math.isfinite(float(v or 0)) else 0.0
        position, duration = seconds(body.get('position')), seconds(body.get('duration'))
        if kind == 'live':
            canonical = text(body.get('canonical'))
            if not canonical:
                return None
            lang = text(body.get('lang'), 20) or ''
            icon = text(body.get('icon'), 500)
            data = {'lang': lang, 'canonical': canonical, 'label': text(body.get('label')) or canonical,
                    'epg_id': text(body.get('epg_id')), 'category': text(body.get('category'))}
            ref = lang + '|' + canonical
            return dict(kind='live', ref=ref, grp='live:' + ref, title=data['label'],
                        icon=icon if icon and re.match(r'https?://', icon) else None,
                        data=data, position=0, duration=0, finished=False)
        if kind not in ('movie', 'episode'):
            return None
        pid, sid = text(body.get('provider'), 64), _panel_int(body.get('id'))
        if not pid or sid is None or not STATE.provider(pid):
            return None
        tracks = {k: _panel_int(body.get(k)) for k in ('audio', 'subtitle')}
        height = _panel_int(body.get('height'))
        data = {'provider_id': pid, 'stream_id': sid,
                'height': height if height in (240, 360, 480, 720) else 480, **tracks}
        if kind == 'movie':
            movie = STATE.catalog.vod_get(pid, sid)
            if not movie:
                return None
            title, icon, grp = movie.get('title') or movie.get('name'), movie.get('icon'), 'movie:%s:%d' % (pid, sid)
            duration = duration or _duration_seconds(movie.get('duration'))
        else:
            episode = STATE.catalog.episode_get(pid, sid)
            if not episode:
                return None
            show = STATE.catalog.series_get(pid, episode['series_id']) or {}
            title, icon = show.get('title') or show.get('name') or 'Série', show.get('icon')
            grp = 'series:%s:%d' % (pid, episode['series_id'])
            data.update(series_id=episode['series_id'], season=episode.get('season') or 0,
                        episode=episode.get('episode') or 0, episode_title=text(episode.get('title')))
            duration = duration or _duration_seconds(episode.get('duration'))
        # Generique de fin : passe 92 %, le film est considere comme vu.
        finished = bool(body.get('finished')) or (duration > 0 and position >= 0.92 * duration)
        return dict(kind=kind, ref='%s:%d' % (pid, sid), grp=grp, title=title, icon=icon,
                    data=data, position=position, duration=duration, finished=finished)

    def _ratings(self, kind, pid, iid):
        """Notes OMDb d'un film ou d'une serie, demandees une fois puis gardees."""
        cat = STATE.catalog
        row = cat.series_get(pid, iid) if kind == 'series' else cat.vod_get(pid, iid)
        # Fiche du panel pas encore lue : on ne la masquerait qu'a moitie.
        if not row or row.get('extra') is None:
            return {}
        extra = row['extra']
        omdb = getattr(STATE, 'omdb', None)
        if omdb and omdb_due(extra):
            try:
                found = omdb.lookup(extra.get('original_title') or row.get('title') or row.get('name'),
                                    extra.get('year'), kind)
            except Exception:
                found = None                     # reseau : on reessaiera a la prochaine fiche
            if found is not None:
                extra = dict(extra, omdb=found, omdb_at=int(time.time()))
                (cat.series_set_extra if kind == 'series' else cat.vod_set_extra)(pid, iid, extra)
        return extra.get('omdb') or {}

    def _sheet(self, kind, pid, iid):
        """Image de fond et resume de la fiche, pour l'accueil.

        `sheet` manque si la fiche du panel n'a jamais ete lue ; `sheet.omdb`
        manque si OMDb n'a pas encore ete interroge : le client sait alors
        quoi demander.
        """
        row = STATE.catalog.series_get(pid, iid) if kind == 'series' else STATE.catalog.vod_get(pid, iid)
        if not row:
            return {}
        extra = row.get('extra')
        found = {k: extra[k] for k in ('backdrop', 'backdrop_small') if (extra or {}).get(k)}
        if extra is not None:
            sheet = {k: extra[k] for k in ('year', 'genre', 'rating') if extra.get(k)}
            plot = str(row.get('plot') or '').strip()
            if plot:
                sheet['plot'] = plot[:320]
            if extra.get('omdb_at'):
                sheet['omdb'] = {k: v for k, v in (extra.get('omdb') or {}).items()
                                 if k in ('imdb_rating', 'rotten_tomatoes', 'metacritic', 'rated')}
            found['sheet'] = sheet
        return found

    def _history(self, account):
        """Historique du compte, et l'episode a suivre de chaque serie terminee."""
        configured = {p.get('id') for p in STATE.cfg.get('providers', [])}
        items, latest = [], {}
        for item in STATE.catalog.history(account):
            pid = item['data'].get('provider_id')
            if item['kind'] != 'live' and pid not in configured:
                continue
            # Fond, notes et resume de la fiche, si elle a deja ete lue :
            # l'accueil en fait son affiche plein cadre.
            if item['kind'] == 'movie':
                item.update(self._sheet('movie', pid, item['data'].get('stream_id')))
            elif item['kind'] == 'episode' and item['data'].get('series_id') is not None:
                item.update(self._sheet('series', pid, item['data']['series_id']))
            items.append(item)
            if item['kind'] == 'episode':
                latest.setdefault(item['grp'], item)
        upcoming = {}
        for grp, item in latest.items():
            if not item['finished']:
                continue
            d = item['data']
            nxt = STATE.catalog.next_episode(d['provider_id'], d['series_id'], d.get('season'), d.get('episode'))
            if nxt:
                upcoming[grp] = dict(nxt, provider_id=d['provider_id'], series_id=d['series_id'],
                                     height=d.get('height'), series_title=item['title'], icon=item['icon'],
                                     **self._sheet('series', d['provider_id'], d['series_id']))
        return {'items': items, 'next': upcoming}

    # --------------------------------------------------------------- API

    def _api_get(self, path, params):
        one = lambda k, d=None: (params.get(k) or [d])[0]
        cat = STATE.catalog

        if path == '/api/me':
            # max_mode : l'interface ne propose pas une qualite que l'instance
            # refuserait ensuite en silence.
            return self._json({'role': self._session()['role'], 'max_mode': STATE.cfg.get('max_mode') or 'sport'})
        if path == '/api/playback':
            ticket = one('ticket')
            if not STATE.transcoder.ticket(ticket, self._session()['id'], touch=False):
                return self._err(404, 'lecture expirée')
            return self._json(STATE.transcoder.status(ticket))
        if path == '/api/epg':
            pid = one("provider")
            sid = one("stream_id")
            if not pid or not sid:
                return self._err(400, 'provider et stream_id requis')
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return self._err(400, 'stream_id invalide')
            provider = STATE.provider(pid)
            if not provider:
                return self._err(404, 'provider introuvable')
            channel = cat.channel(pid, sid)
            if not channel:
                return self._err(404, 'chaîne introuvable')
            try:
                entries = STATE.client(provider).live_epg(sid, epg_channel_id=channel.get('epg_id'))
            except Exception:
                return self._json({'has_epg': False, 'stream_id': sid, 'provider': pid})
            now = time.time()
            current = None
            upcoming = None
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                def pick(key_candidates):
                    for key in key_candidates:
                        value = entry.get(key)
                        parsed = _parse_epg_time(value)
                        if parsed is not None:
                            return parsed
                    return None
                start = pick(('start', 'start_time', 'start_timestamp', 'start_ts'))
                end = pick(('end', 'end_time', 'end_timestamp', 'end_ts'))
                if start is None or end is None:
                    continue
                if start <= now < end:
                    current = entry.copy()
                    current.update({
                        'start_ts': start,
                        'end_ts': end,
                        'progress': max(0.0, min(1.0, (now - start) / max(1, end - start))),
                        'remaining': int(end - now),
                    })
                    break
                if start > now and upcoming is None:
                    upcoming = entry.copy()
                    upcoming.update({'start_ts': start, 'end_ts': end})
            if not current and upcoming:
                upcoming.update({'remaining': int(upcoming['start_ts'] - now)})
            return self._json({
                'provider': pid,
                'stream_id': sid,
                'has_epg': bool(current or upcoming),
                'current': current,
                'next': upcoming,
            })
        if path == "/api/status":
            return self._json({
                "stream": STATE.transcoder.status(),
                "providers": [
                    {"id": p["id"], "name": p.get("name", p["id"]),
                     "enabled": p.get("enabled", True),
                     "kind": p.get("kind", "xtream")}
                    for p in STATE.cfg.get("providers", [])],
                "sync": cat.stats(),
                "catalog_refresh_hours": refresh_seconds(STATE.cfg) / 3600,
                "sync_log": STATE.sync_log[-30:] if self._admin() else [],
                "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
            })

        if path == '/api/devices':
            if not self._admin():
                return self._err(403, "accès administrateur requis")
            return self._json(STATE.devices.list())
        if path == '/api/player-credentials':
            # Lecture seule comprise : c'est ce qu'on colle dans son lecteur.
            return self._json({'players': [self._player_credentials(p) for p in STATE.cfg.get('players', [])]})

        if path == "/api/access":
            if not self._admin():
                return self._err(403, "accès administrateur requis")
            return self._json(ACCESS_LOG[-120:])

        if path == "/api/languages":
            return self._json(cat.languages(provider=one("provider")))

        if path == "/api/categories":
            return self._json(cat.categories(one("lang"), provider=one("provider")))

        if path == "/api/channels":
            return self._json(cat.browse(
                lang=one("lang"), category=one("category"), query=one("q"),
                provider=one("provider"),
                limit=max(1, min(int(one("limit", 200)), 1000)),
                offset=max(0, int(one("offset", 0)))))

        if path == "/api/favorites":
            return self._json(cat.favorites())

        if path == '/api/speedtest':
            # Mesure du debit du spectateur pendant que l'encodeur demarre.
            # Derriere une session : servi a tous, ce serait de la bande
            # passante offerte a n'importe qui.
            return self._raw(200, SPEEDTEST_PAYLOAD, 'application/octet-stream',
                             {'Cache-Control': 'no-store', 'Timing-Allow-Origin': '*'})
        if path == '/api/logo':
            found = STATE.logos.get(one('u'))
            if not found:
                return self._raw(404, b'', 'text/plain', {'Cache-Control': 'private, max-age=3600'})
            return self._raw(200, found[0], found[1], {
                'Cache-Control': 'private, max-age=604800',
                'Content-Security-Policy': "default-src 'none'"})
        if path == '/api/guide/now':
            ids = [i for i in (one('ids') or '').split(',') if i][:300]
            return self._compact(STATE.guide.now_next(ids))

        if path == '/api/history':
            return self._json(self._history(self._session().get('account') or ''))
        if path == '/api/preparations':
            return self._json(STATE.movies.list())
        if path == '/api/vod/tracks':
            if one('kind') == 'episode':
                movie, source = self._episode_source(one('provider'), one('id'))
            else:
                movie, source = self._movie_source(one('provider'), one('id'))
            reservation = secrets.token_urlsafe(12)
            try:
                STATE.transcoder.reserve(reservation, movie['provider_id'])
            except CapacityError as exc:
                return self._err(409, str(exc))
            try:
                return self._json(STATE.movies.info(source))
            finally:
                STATE.transcoder.unreserve(reservation)
        if path == "/api/vod":
            return self._json(cat.vod_browse(
                lang=one("lang"), category=one("category"), query=one("q"),
                limit=max(1, min(int(one("limit", 120)), 500)),
                offset=max(0, int(one("offset", 0)))))

        if path == "/api/providers/info":
            if not self._admin():
                return self._err(403, "accès administrateur requis")
            pid = one("id")
            wanted = [p for p in STATE.cfg.get("providers", []) if not pid or p.get("id") == pid]
            if pid and not wanted:
                return self._err(404, "abonnement introuvable")
            refresh = _as_bool(one("refresh"))
            return self._json([STATE.provider_details(p, force=refresh) for p in wanted])

        if path == "/api/series":
            return self._json(cat.series_browse(
                lang=one("lang"), category=one("category"), query=one("q"),
                limit=max(1, min(int(one("limit", 120)), 500)),
                offset=max(0, int(one("offset", 0)))))

        if path == "/api/series/categories":
            return self._json(cat.series_categories(one("lang")))

        if path == "/api/series/languages":
            return self._json(cat.series_languages())

        if path == "/api/series/info":
            pid, sid = one("provider"), one("id")
            show = cat.series_get(pid, sid)
            if not show:
                return self._err(404, "serie introuvable")
            # Saisons et episodes coutent un appel reseau par serie : on le
            # fait a l'ouverture de la fiche, puis on le garde. La meme reponse
            # porte la fiche (fond, genre, annee) : lue une fois aussi.
            if not show.get("episodes_at") or show.get("extra") is None:
                provider = STATE.provider(pid)
                if not provider:
                    return self._err(404, "abonnement introuvable")
                try:
                    payload = STATE.client(provider).series_info(sid)
                    plot, episodes = parse_series_info(payload)
                except Exception as exc:
                    if not show.get("episodes_at"):
                        return self._err(502, "episodes indisponibles : %s" % exc)
                    payload, episodes = None, []
                if payload is not None:
                    if episodes:
                        cat.series_set_episodes(pid, sid, plot, episodes)
                    elif not show.get("episodes_at"):
                        return self._err(502, "aucun episode annonce pour cette serie")
                    cat.series_set_extra(pid, sid, panel_extra((payload or {}).get("info")))
                show = cat.series_get(pid, sid)
            episodes = cat.series_episodes(pid, sid)
            seasons = {}
            for e in episodes:
                seasons.setdefault(e["season"] or 0, []).append(e)
            show["seasons"] = [{"season": n, "episodes": seasons[n]} for n in sorted(seasons)]
            show["episode_count"] = len(episodes)
            return self._json(show)

        if path == '/api/ratings':
            return self._json(self._ratings('series' if one('kind') == 'series' else 'movie', one('provider'), one('id')))

        if path == "/api/vod/categories":
            return self._json(cat.vod_categories(one("lang")))

        if path == "/api/vod/info":
            pid, sid = one("provider"), one("id")
            movie = cat.vod_get(pid, sid)
            if not movie:
                return self._err(404, "film introuvable")

            # Le conteneur et le debit ne sont pas dans get_vod_streams : il
            # faut un appel par film. On le fait a l'ouverture de la fiche,
            # puis on le garde.
            # La meme reponse porte la fiche TMDB (fond, genre, annee...) : les
            # films lus avant son ajout sont relus une fois pour la recuperer.
            if not movie.get("container") or movie.get("extra") is None:
                provider = STATE.provider(pid)
                if provider:
                    try:
                        info = STATE.client(provider).vod_info(sid)
                        md = info.get("movie_data") or {}
                        detail = info.get("info") or {}
                        cat.vod_set_details(
                            pid, sid, md.get("container_extension") or movie.get("container") or "mp4",
                            int(detail.get("bitrate") or 0),
                            detail.get("duration") or movie.get("duration") or "",
                            (detail.get("plot") or detail.get("description") or movie.get("plot") or "")[:1200])
                        cat.vod_set_extra(pid, sid, panel_extra(detail))
                        movie = cat.vod_get(pid, sid)
                    except Exception as exc:
                        if not movie.get("container"):
                            return self._err(502, "metadonnees indisponibles : %s" % exc)

            movie["size_bytes"] = _estimated_size(movie)
            movie["play_url"] = "/v/session/%s/%s" % (pid, sid)
            return self._json(movie)

        if path == "/api/resolve":
            lang = one("lang") or None
            canonical = one("canonical") or ""
            target = int(STATE.cfg.get("preferred_source_height", 720))
            best = cat.pick_source(lang, canonical, target)
            if not best:
                return self._err(404, "chaine introuvable")
            return self._json({
                "chosen": best,
                "alternatives": cat.sources(lang, canonical),
                "requires_session": True,
            })

        return self._err(404, "endpoint inconnu")

    def _api_post(self, path, params, body):
        cat = STATE.catalog

        if path == '/api/prepare':
            if body.get('kind') == 'episode':
                movie, source = self._episode_source(body.get('provider'), body.get('id'))
            else:
                movie, source = self._movie_source(body.get('provider'), body.get('id'))
            try:
                job = STATE.movies.start(movie, source, int(body.get('height', 480)),
                    int(body['audio']) if body.get('audio') is not None else None,
                    int(body['subtitle']) if body.get('subtitle') is not None else None,
                    account=self._session().get('account') or '')
            except CapacityError as exc:
                return self._err(409, str(exc))
            return self._json(job)
        if path == '/api/prepare/delete':
            if not STATE.movies.delete(str(body.get('id') or '')):
                return self._err(404, 'Préparation introuvable.')
            return self._json({'ok': True})
        if path == '/api/prepare/retry':
            try:
                job = STATE.movies.retry(body.get('job_id') or body.get('id'), self._session().get('account') or '')
            except CapacityError as exc:
                return self._err(409, str(exc))
            if not job:
                return self._err(404, 'Préparation introuvable.')
            return self._json(job)
        if path == "/api/providers":
            pid = (body.get("id") or "").strip() or ("p" + secrets.token_hex(8))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", pid):
                return self._err(400, "identifiant provider invalide")
            link = (body.get("url") or "").strip()
            common = {
                "id": pid,
                "name": body.get("name") or pid,
                "enabled": True,
                "max_connections": max(1, min(10, int(body.get("max_connections", 1)))),
            }
            if link:
                if urllib.parse.urlparse(link).scheme not in ("http", "https"):
                    return self._err(400, "lien HTTP ou HTTPS requis")
                # Un lien get.php porte des identifiants Xtream : l'enregistrer
                # comme panel plutot que comme playlist plate conserve l'EPG,
                # les films et les libelles de categories.
                xt = detect_xtream(link)
                if xt:
                    host, user, password = xt
                    candidate = dict(common, host=host, username=user, password=password)
                    try:
                        STATE.client(candidate).account_info()
                        provider = candidate
                    except Exception:
                        provider = dict(common, kind="m3u", url=link)
                else:
                    provider = dict(common, kind="m3u", url=link)
            else:
                if urllib.parse.urlparse(body.get("host", "")).scheme not in ("http", "https"):
                    return self._err(400, "adresse HTTP ou HTTPS requise")
                if not all(body.get(k) for k in ("host", "username", "password")):
                    return self._err(400, "renseignez un lien M3U, ou host, username et password")
                provider = dict(common, host=body["host"].rstrip("/"),
                                username=body["username"], password=body["password"])
            try:
                STATE.client(provider).account_info()
            except PlaylistError as exc:
                return self._err(400, str(exc))
            except Exception as exc:
                return self._err(400, "connexion refusee : %s" % exc)

            providers = [p for p in STATE.cfg.get("providers", [])
                         if p.get("id") != pid]
            providers.append(provider)
            STATE.cfg["providers"] = providers
            cfgmod.save({"providers": providers})
            _schedule_sync(pid)
            return self._json({"ok": True, "provider": pid, "sync_scheduled": True,
                               "kind": provider.get("kind", "xtream")})

        if path == "/api/providers/delete":
            pid = body.get("id")
            providers = [p for p in STATE.cfg.get("providers", [])
                         if p.get("id") != pid]
            STATE.cfg["providers"] = providers
            cfgmod.save({"providers": providers})
            # Sans cette purge, les chaines de l'abonnement supprime restent
            # listees et ne peuvent plus rien jouer.
            dropped = cat.purge_absent([p["id"] for p in providers])
            STATE.player.invalidate()
            return self._json({"ok": True, "purged": dropped})

        if path == "/api/sync":
            if STATE.sync_lock.locked():
                return self._json({"ok": False, "message": "synchro deja en cours"})
            _schedule_sync(body.get("id"))
            return self._json({"ok": True, "message": "synchro demarree"})

        if path == '/api/history':
            entry = self._history_entry(body)
            if not entry:
                return self._err(404, 'élément introuvable')
            cat.history_record(self._session().get('account') or '', **entry)
            return self._json({'ok': True, 'finished': entry['finished']})

        if path == '/api/history/delete':
            grp = str(body.get('grp') or '')[:200]
            if not grp:
                return self._err(400, 'élément manquant')
            cat.history_forget(self._session().get('account') or '', grp)
            return self._json({'ok': True})

        if path == "/api/favorites":
            cat.add_favorite(body.get("lang") or "", body.get("canonical") or "",
                             body.get("label") or "")
            return self._json({"ok": True})

        if path == "/api/favorites/delete":
            cat.remove_favorite(body.get("lang") or "", body.get("canonical") or "")
            return self._json({"ok": True})

        if path == '/api/play':
            lang, canonical = body.get('lang') or None, body.get('canonical') or ''
            best = cat.pick_source(lang, canonical, int(STATE.cfg.get('preferred_source_height', 720)))
            if not best:
                return self._err(404, 'chaîne introuvable')
            audio_only = _as_bool(body.get('audio_only'))
            ceiling, budget, refusal = _playback_limits(body)
            if refusal:
                return self._err(400, refusal)
            try:
                ticket = STATE.transcoder.open(self._session()['id'], (lang or '') + '|' + canonical,
                    STATE.candidate_urls(best['provider_id'], best['stream_id']), canonical, ceiling, budget, audio_only=audio_only)
            except CapacityError as exc:
                return self._err(409, str(exc))
            return self._json({
                'ticket': ticket,
                'play_url': '/s/%s/master.m3u8' % ticket,
                'ceiling': ceiling,
                'budget': budget,
                'provider': best.get('provider_id'),
                'stream_id': best.get('stream_id'),
                'audio_only': audio_only,
                'epg_id': best.get('epg_id'),
            })
        if path == '/api/relay':
            # L'app detient la playlist ; elle confie ici un flux a compresser.
            try:
                host = relaymod.check_source(body.get('source'), _as_bool(STATE.cfg.get('relay_allow_private')))
            except ValueError as exc:
                return self._err(400, str(exc))
            source = body['source'].strip()
            audio_only = _as_bool(body.get('audio_only'))
            ceiling, budget, refusal = _playback_limits(body)
            if refusal:
                return self._err(400, refusal)
            identity = 'relay|' + relaymod._digest(source)
            try:
                # Un hote = un abonnement, souvent a une seule connexion : deux
                # flux du meme hote ne s'ouvrent pas en parallele.
                ticket = STATE.transcoder.open(self._session()['id'], identity,
                    [{'provider': 'relay:' + host, 'url': source}],
                    str(body.get('label') or 'Relais')[:80], ceiling, budget, audio_only=audio_only)
            except CapacityError as exc:
                return self._err(409, str(exc))
            return self._json({'ticket': ticket, 'play_url': '/s/%s/master.m3u8' % ticket,
                               'ceiling': ceiling, 'budget': budget, 'audio_only': audio_only})
        if path == '/api/pair-code':
            code, seconds = STATE.devices.new_code()
            return self._json({'code': code, 'expires_in': seconds})
        if path == '/api/devices/delete':
            device_id = str(body.get('id') or '')
            STATE.transcoder.release_owner('device:' + device_id)
            STATE.devices.remove(device_id)
            return self._json({'ok': True})
        if path == '/api/stop':
            STATE.transcoder.release(body.get('ticket'), self._session()['id'])
            return self._json({'ok': True})
        if path == '/api/viewer-token':
            return self._json({'token': STATE.cfg.get('viewer_token')})
        if path == '/api/player-credentials':
            players = [dict(p) for p in STATE.cfg.get('players', [])]
            name = body.get('username') or (players[0]['username'] if players else '')
            target = next((p for p in players if p.get('username') == name), None)
            if not target:
                return self._err(404, 'compte lecteur introuvable')
            if 'mode' in body:
                if body['mode'] not in cfgmod.PLAYER_MODES:
                    return self._err(400, 'mode inconnu')
                target['mode'] = body['mode']
            if _as_bool(body.get('regenerate')):
                # L'ancien mot de passe cesse aussitot de fonctionner : le
                # lecteur devra etre reconfigure.
                target['password'] = cfgmod.player_password()
            STATE.cfg['players'] = players
            cfgmod.save({'players': players})
            return self._json(self._player_credentials(target))

        return self._err(404, "endpoint inconnu")


def _playback_limits(body):
    """(plafond, budget, refus) d'une demande de lecture, web ou app."""
    mode = body.get('mode', 'balanced')
    ceiling = MODE_CEILINGS.get(mode, MODE_CEILINGS['balanced'])
    budget = 0
    if mode == 'budget':
        mb, minutes = float(body.get('budget_mb', 800)), float(body.get('minutes', 120))
        if not (20 <= mb <= 50000 and 5 <= minutes <= 1440):
            return 0, 0, 'budget ou durée hors limites'
        budget = int(mb * 1000000 * .97)
        ceiling = int(budget * 8 / (minutes * 60))
    ceiling = capped_ceiling(STATE.cfg, ceiling)
    if not STATE.transcoder.allowed_levels({'ceiling': ceiling}):
        return 0, 0, 'Budget insuffisant pour la durée choisie. Augmentez le volume ou réduisez la durée.'
    return ceiling, budget, None


def _hls_segments(text):
    """(en-tete, segments) d'une playlist de media ; un segment est la liste de
    ses lignes, l'URI en dernier."""
    head, segments, pending = [], [], []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            continue
        if line.startswith("#") and not segments and not pending and not line.startswith(("#EXTINF", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-DISCONTINUITY")):
            head.append(line)
        elif line.startswith("#"):
            pending.append(line)
        else:
            segments.append(pending + [line])
            pending = []
    return head, segments


def _estimated_size(movie):
    """Poids estime d'un film, a partir du debit et de la duree.

    Interroger la taille reelle demanderait une requete HEAD par film. Cette
    estimation suffit a prevenir l'utilisateur avant de lancer plusieurs
    gigaoctets sur une connexion facturee au volume.
    """
    bitrate = movie.get("bitrate") or 0
    parts = str(movie.get("duration") or "").split(":")
    try:
        seconds = sum(int(p) * m for p, m in zip(reversed(parts), (1, 60, 3600)))
    except ValueError:
        seconds = 0
    return int(bitrate * 1000 * seconds / 8) if bitrate and seconds else 0


def _schedule_sync(only_id=None):
    # Le verrou de _run_sync serialise les imports : l'ajout attend son tour
    # si une autre synchronisation est en cours, sans perdre la demande.
    # Capturer l'instance evite qu'un fil utilise un autre etat global.
    threading.Thread(target=_run_sync, args=(only_id, STATE), daemon=True).start()


def _run_sync(only_id=None, state=None, due_only=False):
    """Synchronise un provider (ou tous) en tache de fond."""
    state = state or STATE
    with state.sync_lock:
        changed = False
        def log(msg):
            state.sync_log.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
            print(msg, flush=True)

        for provider in state.cfg.get("providers", []):
            if only_id and provider.get("id") != only_id:
                continue
            if not provider.get("enabled", True):
                continue
            # Une synchro manuelle peut avoir fini pendant l'attente du verrou.
            if due_only and not catalog_due(state.cfg, state.catalog.stats(), provider['id'], time.time()):
                continue
            try:
                client = state.client(provider)
                # Un abonnement expire peut renvoyer des listes vides : verifier
                # l'acces avant de remplacer un catalogue deja disponible.
                client.account_info()
                count, note = state.catalog.sync_provider(provider, client, log)
                changed = True
                log("[%s] direct : %d chaines (%s)" % (provider["id"], count, note))
            except Exception as exc:
                log("[%s] ECHEC direct : %s" % (provider.get("id"), exc))
                continue
            nvod = 0
            try:
                nvod = state.catalog.sync_vod(provider, client, log)
            except Exception as exc:
                log("[%s] ECHEC VOD : %s" % (provider.get("id"), exc))
            # Un panel sans series ne doit pas faire echouer la synchro : une
            # playlist M3U n'en a jamais, et certains abonnements non plus.
            nseries = 0
            try:
                nseries = state.catalog.sync_series(provider, client, log)
            except Exception as exc:
                log("[%s] ECHEC series : %s" % (provider.get("id"), exc))
            log("[%s] termine : %d chaines, %d films, %d series" %
                (provider["id"], count, nvod, nseries))
        if changed:
            state.player.invalidate()
            state.guide.ensure_fresh(force=True)


def main():
    global STATE
    STATE = State()
    cfg = STATE.cfg
    host = cfg.get("listen_host", "0.0.0.0")
    port = int(cfg.get("listen_port", 8088))
    print("Streamly sur http://%s:%d/" % (host, port))
    server = ThreadingHTTPServer((host, port), Handler)
    STATE.catalog_refresh.start()
    try:
        server.serve_forever()
    finally:
        STATE.catalog_refresh.close()
        server.server_close()


if __name__ == "__main__":
    main()
