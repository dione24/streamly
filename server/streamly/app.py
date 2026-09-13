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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as cfgmod
from .catalog import Catalog
from .transcoder import Transcoder
from .xtream import XtreamClient

STARTUP_TIMEOUT = 25      # secondes d'attente de la premiere playlist


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


STATE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Streamly"

    def log_message(self, *args):
        pass

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
            self._raw(200, fh.read(), ctype)

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
        source = STATE.client(provider).live_url(stream_id)
        STATE.transcoder.start(key, source, meta={
            "provider": provider_id, "stream_id": int(stream_id)})
        STATE.transcoder.touch(key)

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
            try:
                count, note = STATE.catalog.sync_provider(
                    provider, STATE.client(provider), log)
                log("[%s] termine : %d chaines (%s)" % (provider["id"], count, note))
            except Exception as exc:
                log("[%s] ECHEC : %s" % (provider.get("id"), exc))


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
