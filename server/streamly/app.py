"""HTTP API, device cookies and short-lived media tickets. Admin-only writes."""
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
from .transcoder import Transcoder, CapacityError
from .auth import Sessions
from .vod import Movies
from .xtream import XtreamClient

STARTUP_TIMEOUT = 25      # secondes d'attente de la premiere playlist
ACCESS_LOG = []           # journal d'acces circulaire, expose via /api/access


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
        self.transcoder = Transcoder(self.cfg, cfgmod.HLS_DIR, cfgmod.LOG_DIR)
        self.sessions = Sessions(self.cfg)
        self.movies = Movies(self.cfg, os.path.join(cfgmod.DATA_DIR, "movies"), self.catalog, self.transcoder)
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
            if not provider or not provider.get("enabled", True) or (pid, sid) in seen:
                return
            seen.add((pid, sid))
            urls.append({"provider": pid, "url": self.client(provider).live_url(sid)})

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
                               self.client_address[0], re.sub(r"(/(?:s|v|media)/)[^/ ?]+", r"\1[redacted]", fmt % args))
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
        self._raw(code, json.dumps(obj, ensure_ascii=False, indent=1),
                  "application/json; charset=utf-8", headers)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    # -------------------------------------------------------------- jeton

    def _session_cookie(self, sid):
        secure = '; Secure' if STATE.cfg.get('secure_cookies') else ''
        return 'streamly_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800%s' % (sid, secure)

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
        try:
            sid, role = STATE.sessions.login(token, self.client_address[0])
        except ValueError:
            self._request_session = None
            return None
        self._cookie = self._session_cookie(sid)
        session = dict(id=sid, role=role)
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
        if path.startswith("/api/"):
            if not self._api_authed(params):
                return self._err(401, "jeton invalide ou absent")
            try:
                return self._api_get(path, params)
            except (ValueError, TypeError):
                return self._err(400, "paramètres invalides")
        return self._serve_web(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        params = urllib.parse.parse_qs(parsed.query)
        origin = self.headers.get('Origin')
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get('Host'):
            return self._err(403, 'origine refusée')
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
                sid, role = STATE.sessions.login(body.get('token'), self.client_address[0])
            except ValueError as exc:
                return self._err(401, str(exc))
            self._cookie = self._session_cookie(sid)
            return self._json({'role': role}, extra={'Cache-Control': 'no-store'})
        if not self._api_authed(params):
            return self._err(401, 'connexion requise')
        if path == '/api/logout':
            sid = self._api_authed(None)['id']
            STATE.transcoder.release_owner(sid)
            STATE.sessions.logout(sid)
            self._cookie = self._clear_cookie()
            return self._raw(200, '{}', 'application/json', {'Cache-Control': 'no-store'})
        if path.startswith('/api/providers') or path in ('/api/sync', '/api/viewer-token'):
            if not self._admin():
                return self._err(403, 'accès administrateur requis')
        try:
            return self._api_post(path, params, body)
        except (ValueError, TypeError):
            return self._err(400, 'paramètres invalides')

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
        parts = path.strip('/').split('/')
        if len(parts) != 3:
            return self._err(404, 'flux introuvable')
        _, ticket, fname = parts
        t = STATE.transcoder.ticket(ticket)
        if not t:
            return self._err(401, 'session de lecture expirée')
        status = STATE.transcoder.status(ticket)
        if not status or status['state'] == 'failed':
            return self._err(502, 'sources indisponibles')
        if fname == 'master.m3u8':
            deadline = time.time() + 10
            while status['state'] == 'starting' and time.time() < deadline:
                time.sleep(.1)
                status = STATE.transcoder.status(ticket)
                if not status:
                    return self._err(410, 'lecture arrêtée')
            return self._raw(200, STATE.transcoder.master_playlist(ticket), 'application/vnd.apple.mpegurl', {'Cache-Control': 'no-store'})
        match = re.fullmatch(r'g(\d+)_(?:s_(\d+)\.m3u8|(\d+)_\d+\.ts)', fname)
        if not match:
            return self._err(404, 'segment invalide')
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
        if not job or job['state'] != 'ready' or not re.fullmatch(r'(master|q\d+)\.m3u8|q\d+\.mp4|q\d+_\d+\.ts|subtitles\.vtt', name):
            return self._err(404, 'préparation indisponible')
        job['last_access'] = time.time()
        target = os.path.join(STATE.movies.root, jid, name)
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

    # --------------------------------------------------------------- API

    def _api_get(self, path, params):
        one = lambda k, d=None: (params.get(k) or [d])[0]
        cat = STATE.catalog

        if path == '/api/me':
            return self._json({'role': self._session()['role']})
        if path == '/api/playback':
            ticket = one('ticket')
            if not STATE.transcoder.ticket(ticket, self._session()['id'], touch=False):
                return self._err(404, 'lecture expirée')
            return self._json(STATE.transcoder.status(ticket))
        if path == "/api/status":
            return self._json({
                "stream": STATE.transcoder.status(),
                "providers": [
                    {"id": p["id"], "name": p.get("name", p["id"]),
                     "enabled": p.get("enabled", True)}
                    for p in STATE.cfg.get("providers", [])],
                "sync": cat.stats(),
                "sync_log": STATE.sync_log[-30:] if self._admin() else [],
                "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
            })

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

        if path == '/api/preparations':
            return self._json(STATE.movies.list())
        if path == '/api/vod/tracks':
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
            movie, source = self._movie_source(body.get('provider'), body.get('id'))
            try:
                job = STATE.movies.start(movie, source, int(body.get('height', 480)),
                    int(body['audio']) if body.get('audio') is not None else None,
                    int(body['subtitle']) if body.get('subtitle') is not None else None)
            except CapacityError as exc:
                return self._err(409, str(exc))
            return self._json(job)
        if path == '/api/prepare/retry':
            job = STATE.movies.retry(body.get('job_id') or body.get('id'))
            if not job:
                return self._err(404, 'Préparation introuvable.')
            return self._json(job)
        if path == "/api/providers":
            pid = (body.get("id") or "").strip() or ("p%d" % int(time.time()))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", pid):
                return self._err(400, "identifiant provider invalide")
            if urllib.parse.urlparse(body.get("host", "")).scheme not in ("http", "https"):
                return self._err(400, "adresse HTTP ou HTTPS requise")
            if not all(body.get(k) for k in ("host", "username", "password")):
                return self._err(400, "host, username et password sont requis")
            provider = {
                "id": pid,
                "name": body.get("name") or pid,
                "host": body["host"].rstrip("/"),
                "username": body["username"],
                "password": body["password"],
                "enabled": True,
                "max_connections": max(1, min(10, int(body.get("max_connections", 1)))),
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
            # Sans cette purge, les chaines de l'abonnement supprime restent
            # listees et ne peuvent plus rien jouer.
            dropped = cat.purge_absent([p["id"] for p in providers])
            return self._json({"ok": True, "purged": dropped})

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

        if path == '/api/play':
            lang, canonical = body.get('lang') or None, body.get('canonical') or ''
            best = cat.pick_source(lang, canonical, int(STATE.cfg.get('preferred_source_height', 720)))
            if not best:
                return self._err(404, 'chaîne introuvable')
            mode = body.get('mode', 'balanced')
            ceiling = {'eco': 650000, 'balanced': 1150000, 'sport': 0}.get(mode, 1150000)
            budget = 0
            if mode == 'budget':
                mb, minutes = float(body.get('budget_mb', 800)), float(body.get('minutes', 120))
                if not (20 <= mb <= 50000 and 5 <= minutes <= 1440):
                    return self._err(400, 'budget ou durée hors limites')
                budget = int(mb * 1000000 * .97)
                ceiling = int(budget * 8 / (minutes * 60))
            if not STATE.transcoder.allowed_levels({'ceiling': ceiling}):
                return self._err(400, 'Budget insuffisant pour la durée choisie. Augmentez le volume ou réduisez la durée.')
            try:
                ticket = STATE.transcoder.open(self._session()['id'], (lang or '') + '|' + canonical,
                    STATE.candidate_urls(best['provider_id'], best['stream_id']), canonical, ceiling, budget)
            except CapacityError as exc:
                return self._err(409, str(exc))
            return self._json({'ticket': ticket, 'play_url': '/s/%s/master.m3u8' % ticket, 'ceiling': ceiling, 'budget': budget})
        if path == '/api/stop':
            STATE.transcoder.release(body.get('ticket'), self._session()['id'])
            return self._json({'ok': True})
        if path == '/api/viewer-token':
            return self._json({'token': STATE.cfg.get('viewer_token')})

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
    print("Streamly sur http://%s:%d/" % (host, port))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
