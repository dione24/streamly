#!/usr/bin/env python3
"""Faux panel Xtream pour essayer le lecteur sans abonnement réel.

    python3 apple/tools/fake_panel.py            # http://127.0.0.1:8765, compte demo / demo

Il se comporte comme les vrais panels sur ce qui piège un lecteur :

  - une seule connexion à la fois, et un 401 pendant 1,5 s après la fermeture de la précédente ;
  - des nombres écrits en chaînes et des titres de guide en base64 ;
  - du MPEG-TS brut sur HTTP, que le lecteur natif d'Apple ne sait pas lire ;
  - une variante en panne (la FHD de « Démo Sport »), pour vérifier le passage à la suivante.

Les images sont des mires générées par FFmpeg : aucun contenu réel.
"""
import argparse
import base64
import json
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "demo", "demo"
CATEGORIES = [
    {"category_id": "1", "category_name": "Sport", "parent_id": 0},
    {"category_id": "2", "category_name": "Cinéma", "parent_id": 0},
    {"category_id": "3", "category_name": "Formats difficiles", "parent_id": 0},
]
# id, nom, catégorie, codec vidéo, codec audio, hauteur. codec None = variante en panne.
STREAMS = [
    (101, "FR| Démo Sport FHD", "1", None, None, 1080),
    (102, "FR| Démo Sport HD", "1", "libx264", "aac", 720),
    (103, "FR| Démo Sport [BK]", "1", "libx264", "aac", 480),
    (201, "FR| Démo Cinéma HD", "2", "libx264", "ac3", 720),
    (202, "FR| Démo Cinéma SD", "2", "libx264", "aac", 480),
    (203, "EN| Demo Movies HD", "2", "libx264", "aac", 720),
    (301, "FR| Démo Nature HEVC", "3", "libx265", "aac", 720),
    (302, "FR| Démo Audio MP2 HD", "3", "libx264", "mp2", 720),
    (303, "AR| قناة تجريبية HD", "3", "libx264", "aac", 720),
]
COLORS = {"1": "testsrc2", "2": "smptebars", "3": "testsrc"}


class Connections:
    """Une connexion à la fois, avec la période de refus qui suit, comme un abonnement réel."""
    cooldown = 1.5

    def __init__(self):
        self.lock = threading.Lock()
        self.active = None
        self.closed_at = 0.0

    def acquire(self, token):
        with self.lock:
            if self.active is not None or time.time() - self.closed_at < self.cooldown:
                return False
            self.active = token
            return True

    def release(self, token):
        with self.lock:
            if self.active == token:
                self.active = None
                self.closed_at = time.time()


connections = Connections()


def epg_rows(stream_id):
    now = int(time.time())
    start = now - now % 1800
    titles = ["Journal de démonstration", "Match d'essai", "Film de test", "Magazine fictif"]
    rows = []
    for index in range(4):
        title = titles[(index + stream_id) % len(titles)]
        rows.append({
            "title": base64.b64encode(title.encode()).decode(),
            "description": base64.b64encode(f"Programme fictif : {title}.".encode()).decode(),
            "start_timestamp": str(start + index * 1800),
            "stop_timestamp": str(start + (index + 1) * 1800),
        })
    return rows


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_status(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/player_api.php":
            return self.api(query)
        if url.path == "/get.php":
            return self.m3u(query)
        parts = url.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == USER and parts[1] == PASSWORD:
            return self.stream(parts[2])
        return self.send_status(404)

    def api(self, query):
        if query.get("username") != USER or query.get("password") != PASSWORD:
            return self.send_json({"user_info": {"auth": 0}})
        action = query.get("action")
        if action == "get_live_categories":
            return self.send_json(CATEGORIES)
        if action == "get_live_streams":
            return self.send_json([
                {"num": str(index + 1), "name": name, "stream_id": stream_id, "category_id": category,
                 "stream_icon": None, "epg_channel_id": f"demo{stream_id}"}
                for index, (stream_id, name, category, *_rest) in enumerate(STREAMS)
            ])
        if action == "get_short_epg":
            return self.send_json({"epg_listings": epg_rows(int(query.get("stream_id", 0)))})
        return self.send_json({
            "user_info": {
                "auth": 1, "status": "Active", "exp_date": str(int(time.time()) + 90 * 86400),
                "max_connections": "1", "active_cons": "0",
            },
            "server_info": {"url": "127.0.0.1"},
        })

    def m3u(self, query):
        if query.get("username") != USER or query.get("password") != PASSWORD:
            return self.send_status(401)
        host = self.headers.get("Host")
        lines = ["#EXTM3U"]
        for stream_id, name, category, *_rest in STREAMS:
            group = next(c["category_name"] for c in CATEGORIES if c["category_id"] == category)
            lines += [f'#EXTINF:-1 tvg-id="demo{stream_id}" group-title="{group}",{name}',
                      f"http://{host}/{USER}/{PASSWORD}/{stream_id}"]
        body = "\n".join(lines).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, raw_id):
        entry = next((s for s in STREAMS if str(s[0]) == raw_id.split(".")[0]), None)
        if entry is None:
            return self.send_status(404)
        stream_id, _name, category, video, audio, height = entry
        if video is None:
            return self.send_status(404)
        token = object()
        if not connections.acquire(token):
            print(f"{time.strftime('%X')} flux {stream_id} : 401", flush=True)
            return self.send_status(401)
        print(f"{time.strftime('%X')} flux {stream_id} : ouvert", flush=True)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", f"{COLORS[category]}=s={height * 16 // 9 // 2 * 2}x{height}:r=25",
            "-f", "lavfi", "-i", f"sine=frequency={200 + stream_id}:sample_rate=48000",
            "-c:v", video, "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p",
            "-c:a", audio, "-b:a", "128k", "-ac", "2",
            "-f", "mpegts", "pipe:1",
        ]
        if video == "libx265":
            command[command.index("-g"):command.index("-g")] = ["-x265-params", "log-level=none"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                # read1 renvoie ce qui est disponible : une mire compresse si bien qu'attendre un
                # bloc plein retarderait de plusieurs secondes la détection d'un client parti.
                chunk = process.stdout.read1(188 * 64)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.kill()
            process.wait()
            connections.release(token)
            print(f"{time.strftime('%X')} flux {stream_id} : fermé", flush=True)
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Faux panel : http://127.0.0.1:{args.port}  (identifiant {USER}, mot de passe {PASSWORD})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
