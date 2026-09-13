"""API HTTP et service des flux.

Le jeton d'acces est place dans le *chemin* des URLs de lecture
(`/s/<jeton>/...`) et non en parametre : les playlists HLS referencent leurs
sous-playlists et leurs segments en relatif, qui heritent donc du jeton sans
qu'on ait a reecrire quoi que ce soit.
"""
import json
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
from .transcoder import Transcoder
from .xtream import XtreamClient

STARTUP_TIMEOUT = 25      # secondes d'attente de la premiere playlist
ACCESS_LOG = []           # journal d'acces circulaire, expose via /api/access


class State:
    def __init__(self):
        self.cfg = cfgmod.load()
        self.catalog = Catalog(os.path.join(cfgmod.DATA_DIR, "catalog.db"))
        self.transcoder = Transcoder(self.cfg, cfgmod.HLS_DIR, cfgmod.LOG_DIR)
        self.sync_lock = threading.Lock()
        self.sync_log = []

    def provider(self, pid):
        for p in self.cfg.get("providers", []):
            if p.get("id") == pid:
                return p
        return None

    def client(self, provider):
        return XtreamClient(provider["host"], provider["username"],
                            provider["password"],
                            self.cfg.get("user_agent", "VLC/3.0.20"))

    def candidate_urls(self, provider_id, stream_id):
        """URLs a essayer pour une chaine, dans l'ordre.

        La source demandee d'abord, puis les autres variantes de la meme
        chaine : flux principaux avant flux de secours, et toutes providers
        confondus. C'est ce qui permet de basculer quand un flux lache en
        plein direct sans que l'utilisateur ait a chercher une autre entree.
        """
        urls, seen = [], set()

        def add(pid, sid):
            provider = self.provider(pid)
            if not provider or (pid, sid) in seen:
                return
            seen.add((pid, sid))
            urls.append(self.client(provider).live_url(sid))

        add(provider_id, int(stream_id))

        channel = self.catalog.channel(provider_id, int(stream_id))
        if channel and channel.get("canonical"):
            target = int(self.cfg.get("preferred_source_height", 720))
            alts = self.catalog.sources(channel.get("lang"), channel["canonical"])
            alts.sort(key=lambda s: (s["is_backup"],
                                     abs((s["height"] or 0) - target)))
            for alt in alts:
                add(alt["provider_id"], alt["stream_id"])
        return urls


STATE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Streamly"

    def log_message(self, fmt, *args):
        # Journal d'acces minimal : indispensable pour distinguer « la requete
        # n'arrive pas » de « le serveur repond mal ».
        line = "%s  %s  %s" % (time.strftime("%H:%M:%S"),
                               self.client_address[0], fmt % args)
        ACCESS_LOG.append(line)
        del ACCESS_LOG[:-400]

    # ------------------------------------------------------------ reponses

    def _raw(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._raw(code, json.dumps(obj, ensure_ascii=False, indent=1),
                  "application/json; charset=utf-8",
                  {"Cache-Control": "no-store"})

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    # -------------------------------------------------------------- jeton

    def _token_ok(self, given):
        return bool(given) and secrets.compare_digest(
            str(given), str(STATE.cfg.get("token", "")))

    def _api_authed(self, params):
        given = self.headers.get("X-Token") or (params.get("t") or [None])[0]
        return self._token_ok(given)

    # ------------------------------------------------------------ routage

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/s/"):
            return self._serve_stream(path)
        if path.startswith("/v/"):
            return self._serve_vod(path)
        if path.startswith("/api/"):
            if not self._api_authed(params):
                return self._err(401, "jeton invalide ou absent")
            return self._api_get(path, params)
        return self._serve_web(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)
        if not path.startswith("/api/"):
            return self._err(404, "introuvable")
        if not self._api_authed(params):
            return self._err(401, "jeton invalide ou absent")

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._err(400, "corps JSON invalide")
        return self._api_post(path, params, body)

    # -------------------------------------------------------- fichiers web

    def _serve_web(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = posixpath.normpath(rel)
        if rel.startswith("..") or os.path.isabs(rel):
            return self._err(403, "chemin refuse")
        full = os.path.join(cfgmod.WEB_DIR, rel)
        if not os.path.isfile(full):
            return self._err(404, "introuvable")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            # Sans cela, une mise a jour du client reste invisible derriere le
            # cache du navigateur — symptome classique d'une interface « morte ».
            self._raw(200, fh.read(), ctype, {"Cache-Control": "no-cache"})

    # -------------------------------------------------------------- flux

    def _serve_stream(self, path):
        # /s/<jeton>/<provider>/<stream_id>/<fichier>
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            return self._err(404, "chemin de flux invalide")
        _, token, provider_id, stream_id, fname = parts

        if not self._token_ok(token):
            return self._err(401, "jeton invalide")

        provider = STATE.provider(provider_id)
        if not provider:
            return self._err(404, "provider inconnu")
        if not re.fullmatch(r"\d+", stream_id):
            return self._err(400, "identifiant de flux invalide")
        fname = os.path.basename(fname)

        key = "%s-%s" % (provider_id, stream_id)
        if STATE.transcoder.is_alive(key):
            # Cas courant : une requete de segment sur un flux deja lance.
            # On evite de recalculer la liste des candidats a chaque segment.
            STATE.transcoder.touch(key)
        else:
            STATE.transcoder.start(key, STATE.candidate_urls(provider_id, stream_id),
                                   meta={"provider": provider_id,
                                         "stream_id": int(stream_id)})

        if fname == "master.m3u8":
            return self._raw(200, STATE.transcoder.master_playlist(),
                             "application/vnd.apple.mpegurl",
                             {"Cache-Control": "no-store"})

        target = os.path.join(cfgmod.HLS_DIR, key, fname)
        deadline = time.time() + STARTUP_TIMEOUT
        while not os.path.exists(target) and time.time() < deadline:
            if not STATE.transcoder.is_alive(key):
                return self._err(502, "le transcodage s'est arrete")
            time.sleep(0.2)
        if not os.path.exists(target):
            return self._err(504, "delai depasse au demarrage du flux")

        ctype = ("application/vnd.apple.mpegurl" if fname.endswith(".m3u8")
                 else "video/mp2t")
        try:
            with open(target, "rb") as fh:
                self._raw(200, fh.read(), ctype, {"Cache-Control": "no-store"})
        except OSError:
            self._err(404, "segment expire")

    # ---------------------------------------------------------------- VOD

    def _serve_vod(self, path):
        """Sert un film. Deux strategies selon le conteneur.

        Un MP4 est relaye tel quel, en repercutant les requetes Range : le
        navigateur lit et se deplace nativement, sans aucun transcodage.

        Un MKV n'est pas lisible en navigateur ; on le remultiplexe a la volee
        en MP4 fragmente. La video est recopiee (`-c:v copy`, cout nul) et
        seul l'audio est reencode, car les pistes E-AC3 frequentes sur ces
        fichiers ne sont pas decodables par les navigateurs.
        """
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return self._err(404, "chemin invalide")
        _, token, provider_id, stream_id = parts
        if not self._token_ok(token):
            return self._err(401, "jeton invalide")
        if not re.fullmatch(r"\d+", stream_id):
            return self._err(400, "identifiant invalide")

        provider = STATE.provider(provider_id)
        movie = STATE.catalog.vod_get(provider_id, stream_id)
        if not provider or not movie:
            return self._err(404, "film introuvable")

        container = (movie.get("container") or "mp4").lstrip(".")
        client = STATE.client(provider)
        source = "%s/movie/%s/%s/%s.%s" % (
            client.host, client.username, client.password, stream_id, container)

        if container.lower() in ("mp4", "m4v"):
            return self._proxy_range(source, client.user_agent)
        return self._remux(source, client.user_agent)

    def _proxy_range(self, source, user_agent):
        headers = {"User-Agent": user_agent}
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        req = urllib.request.Request(source, headers=headers)
        try:
            upstream = urllib.request.urlopen(req, timeout=30)
        except Exception as exc:
            return self._err(502, "source indisponible : %s" % exc)

        with upstream:
            code = upstream.status
            self.send_response(code)
            for name in ("Content-Type", "Content-Length", "Content-Range"):
                value = upstream.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while True:
                    chunk = upstream.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass    # le lecteur a change de position ou ferme l'onglet

    def _remux(self, source, user_agent):
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-user_agent", user_agent, "-i", source,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        # Flux de longueur inconnue : on ferme la connexion en fin d'envoi.
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                chunk = proc.stdout.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc.poll() is None:
                proc.kill()

    # --------------------------------------------------------------- API

    def _api_get(self, path, params):
        one = lambda k, d=None: (params.get(k) or [d])[0]
        cat = STATE.catalog

        if path == "/api/status":
            return self._json({
                "stream": STATE.transcoder.status(),
                "providers": [
                    {"id": p["id"], "name": p.get("name", p["id"]),
                     "enabled": p.get("enabled", True)}
                    for p in STATE.cfg.get("providers", [])],
                "sync": cat.stats(),
                "sync_log": STATE.sync_log[-30:],
            })

        if path == "/api/access":
            return self._json(ACCESS_LOG[-120:])

        if path == "/api/languages":
            return self._json(cat.languages())

        if path == "/api/categories":
            return self._json(cat.categories(one("lang")))

        if path == "/api/channels":
            return self._json(cat.browse(
                lang=one("lang"), category=one("category"), query=one("q"),
                limit=min(int(one("limit", 200)), 1000),
                offset=int(one("offset", 0))))

        if path == "/api/favorites":
            return self._json(cat.favorites())

        if path == "/api/vod":
            return self._json(cat.vod_browse(
                lang=one("lang"), category=one("category"), query=one("q"),
                limit=min(int(one("limit", 120)), 500),
                offset=int(one("offset", 0))))

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
            if not movie.get("container"):
                provider = STATE.provider(pid)
                if provider:
                    try:
                        info = STATE.client(provider).vod_info(sid)
                        md = info.get("movie_data") or {}
                        detail = info.get("info") or {}
                        cat.vod_set_details(
                            pid, sid, md.get("container_extension") or "mp4",
                            int(detail.get("bitrate") or 0),
                            detail.get("duration") or "",
                            (detail.get("plot") or detail.get("description") or "")[:1200])
                        movie = cat.vod_get(pid, sid)
                    except Exception as exc:
                        return self._err(502, "metadonnees indisponibles : %s" % exc)

            movie["size_bytes"] = _estimated_size(movie)
            movie["play_url"] = "/v/%s/%s/%s" % (STATE.cfg["token"], pid, sid)
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
                "play_url": "/s/%s/%s/%s/master.m3u8" % (
                    STATE.cfg["token"], best["provider_id"], best["stream_id"]),
            })

        return self._err(404, "endpoint inconnu")

    def _api_post(self, path, params, body):
        cat = STATE.catalog

        if path == "/api/providers":
            pid = (body.get("id") or "").strip() or ("p%d" % int(time.time()))
            if not all(body.get(k) for k in ("host", "username", "password")):
                return self._err(400, "host, username et password sont requis")
            provider = {
                "id": pid,
                "name": body.get("name") or pid,
                "host": body["host"].rstrip("/"),
                "username": body["username"],
                "password": body["password"],
                "enabled": True,
            }
            try:
                STATE.client(provider).account_info()
            except Exception as exc:
                return self._err(400, "connexion refusee : %s" % exc)

            providers = [p for p in STATE.cfg.get("providers", [])
                         if p.get("id") != pid]
            providers.append(provider)
            STATE.cfg["providers"] = providers
            cfgmod.save({"providers": providers})
            return self._json({"ok": True, "provider": pid})

        if path == "/api/providers/delete":
            pid = body.get("id")
            providers = [p for p in STATE.cfg.get("providers", [])
                         if p.get("id") != pid]
            STATE.cfg["providers"] = providers
            cfgmod.save({"providers": providers})
            return self._json({"ok": True})

        if path == "/api/sync":
            if STATE.sync_lock.locked():
                return self._json({"ok": False, "message": "synchro deja en cours"})
            threading.Thread(target=_run_sync, args=(body.get("id"),),
                             daemon=True).start()
            return self._json({"ok": True, "message": "synchro demarree"})

        if path == "/api/favorites":
            cat.add_favorite(body.get("lang") or "", body.get("canonical") or "",
                             body.get("label") or "")
            return self._json({"ok": True})

        if path == "/api/favorites/delete":
            cat.remove_favorite(body.get("lang") or "", body.get("canonical") or "")
            return self._json({"ok": True})

        if path == "/api/stop":
            STATE.transcoder.stop()
            return self._json({"ok": True})

        return self._err(404, "endpoint inconnu")


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


def _run_sync(only_id=None):
    """Synchronise un provider (ou tous) en tache de fond."""
    with STATE.sync_lock:
        def log(msg):
            STATE.sync_log.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
            print(msg, flush=True)

        for provider in STATE.cfg.get("providers", []):
            if only_id and provider.get("id") != only_id:
                continue
            if not provider.get("enabled", True):
                continue
            client = STATE.client(provider)
            try:
                count, note = STATE.catalog.sync_provider(provider, client, log)
                log("[%s] direct : %d chaines (%s)" % (provider["id"], count, note))
            except Exception as exc:
                log("[%s] ECHEC direct : %s" % (provider.get("id"), exc))
                continue
            try:
                nvod = STATE.catalog.sync_vod(provider, client, log)
                log("[%s] termine : %d chaines, %d films" %
                    (provider["id"], count, nvod))
            except Exception as exc:
                log("[%s] ECHEC VOD : %s" % (provider.get("id"), exc))


def main():
    global STATE
    STATE = State()
    cfg = STATE.cfg
    host = cfg.get("listen_host", "0.0.0.0")
    port = int(cfg.get("listen_port", 8088))
    print("Streamly sur http://%s:%d/  (jeton : %s)" % (host, port, cfg["token"]))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
