#!/usr/bin/env python3
"""Faux panel Xtream pour essayer le lecteur sans abonnement réel.

    python3 apple/tools/fake_panel.py            # http://127.0.0.1:8765, compte demo / demo

Il se comporte comme les vrais panels sur ce qui piège un lecteur :

  - une seule connexion à la fois, et un 401 pendant 1,5 s après la fermeture de la précédente ;
  - des nombres écrits en chaînes et des titres de guide en base64 ;
  - du MPEG-TS brut sur HTTP, que le lecteur natif d'Apple ne sait pas lire ;
  - une variante en panne (la FHD de « Démo Sport »), pour vérifier le passage à la suivante ;
  - des films servis par plages sous la même règle : chaque saut rouvre une connexion, refusée
    si elle est trop rapprochée de la précédente.

Les images sont des mires générées par FFmpeg : aucun contenu réel.
"""
import argparse
import base64
import json
import os
import subprocess
import tempfile
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
# Chaînes dont le fournisseur garde les programmes passés.
ARCHIVED = {102: 3, 201: 1}

VOD_CATEGORIES = [
    {"category_id": "11", "category_name": "Action", "parent_id": 0},
    {"category_id": "12", "category_name": "Documentaires", "parent_id": 0},
    {"category_id": "13", "category_name": "ADULTES", "parent_id": 0},
]
# id, nom, catégorie, note, extension.
MOVIES = [
    (501, "FR| La Mire de l'Aube 4K (2021)", "11", "7.8", "mkv"),
    (502, "FR| Démonstration Finale HD (2019)", "11", "6.4", "mkv"),
    (503, "FR| Couleurs du Monde (2023)", "12", "8.2", "mkv"),
    (504, "Film réservé", "13", "0", "mkv"),
]
SERIES_CATEGORIES = [{"category_id": "21", "category_name": "Drames", "parent_id": 0}]
SERIES = {
    601: ("FR| La Série d'Essai", "21", "8.0"),
    602: ("FR| Courte Série", "21", "0"),
}
EPISODES = {
    601: {"1": [(6011, 1, "Le départ"), (6012, 2, "La suite")], "2": [(6021, 1, "Le retour")]},
    602: {"1": [(6101, 1, "Pilote")]},
}


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
MEDIA_DIR = os.path.join(tempfile.gettempdir(), "streamly-fake-panel")
os.makedirs(MEDIA_DIR, exist_ok=True)
media_lock = threading.Lock()


def media_file():
    """Un vrai film de test : deux pistes audio, des sous-titres, et un index en fin de fichier."""
    path = os.path.join(MEDIA_DIR, "film.mkv")
    with media_lock:
        if not os.path.exists(path):
            subtitles = os.path.join(MEDIA_DIR, "film.srt")
            with open(subtitles, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:01,000 --> 00:01:50,000\nSous-titre de démonstration\n")
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25",
                "-f", "lavfi", "-i", "sine=frequency=330",
                "-f", "lavfi", "-i", "sine=frequency=550",
                "-i", subtitles, "-t", "120",
                "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-g", "25", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-c:s", "srt",
                "-metadata:s:a:0", "language=fre", "-metadata:s:a:1", "language=eng",
                "-metadata:s:s:0", "language=fre", path,
            ], check=True)
    return path


def epg_rows(stream_id, first=0, count=12):
    now = int(time.time())
    start = now - now % 1800
    titles = ["Journal de démonstration", "Match d'essai", "Film de test", "Magazine fictif"]
    rows = []
    for index in range(first, first + count):
        title = titles[(index + stream_id) % len(titles)]
        stop = start + (index + 1) * 1800
        rows.append({
            "title": base64.b64encode(title.encode()).decode(),
            "description": base64.b64encode(f"Programme fictif : {title}.".encode()).decode(),
            "start_timestamp": str(start + index * 1800),
            "stop_timestamp": str(stop),
            "has_archive": 1 if stream_id in ARCHIVED and stop <= now else 0,
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
        if len(parts) == 4 and parts[0] in ("movie", "series") and parts[1] == USER and parts[2] == PASSWORD:
            return self.on_demand(parts[0], parts[3])
        if len(parts) == 6 and parts[0] == "timeshift" and parts[1] == USER and parts[2] == PASSWORD:
            print(f"{time.strftime('%X')} rattrapage {parts[5]} depuis {parts[4]} pendant {parts[3]} min", flush=True)
            return self.send_file()
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
                 "stream_icon": None, "epg_channel_id": f"demo{stream_id}",
                 "tv_archive": 1 if stream_id in ARCHIVED else 0, "tv_archive_duration": ARCHIVED.get(stream_id, 0)}
                for index, (stream_id, name, category, *_rest) in enumerate(STREAMS)
            ])
        if action == "get_short_epg":
            return self.send_json({"epg_listings": epg_rows(int(query.get("stream_id", 0)))})
        if action == "get_simple_data_table":
            return self.send_json({"epg_listings": epg_rows(int(query.get("stream_id", 0)), first=-8, count=12)})
        if action == "get_vod_categories":
            return self.send_json(VOD_CATEGORIES)
        if action == "get_vod_streams":
            return self.send_json([
                {"num": index + 1, "name": name, "stream_id": movie_id, "category_id": category,
                 "stream_icon": None, "rating": rating, "container_extension": ext}
                for index, (movie_id, name, category, rating, ext) in enumerate(MOVIES)
            ])
        if action == "get_vod_info":
            movie = next((m for m in MOVIES if str(m[0]) == query.get("vod_id")), None)
            if movie is None:
                return self.send_json([])
            return self.send_json({
                "info": {"plot": f"Un film de démonstration, sans contenu réel : {movie[1]}.",
                         "genre": "Démonstration", "director": "FFmpeg", "cast": "Mire, Sinusoïde",
                         "releasedate": "2021-03-14", "duration_secs": 120, "rating": movie[3],
                         "backdrop_path": []},
                "movie_data": {"stream_id": movie[0], "name": movie[1], "container_extension": movie[4]},
            })
        if action == "get_series_categories":
            return self.send_json(SERIES_CATEGORIES)
        if action == "get_series":
            return self.send_json([
                {"series_id": series_id, "name": name, "category_id": category, "cover": None, "rating": rating,
                 "plot": "Une série de démonstration."}
                for series_id, (name, category, rating) in SERIES.items()
            ])
        if action == "get_series_info":
            episodes = EPISODES.get(int(query.get("series_id", 0)), {})
            return self.send_json({
                "info": {"plot": "Une série de démonstration, sans contenu réel.", "genre": "Démonstration",
                         "cast": "Mire", "releaseDate": "2022-09-01", "backdrop_path": []},
                "episodes": {
                    season: [{"id": str(eid), "episode_num": number, "title": title, "season": int(season),
                              "container_extension": "mkv",
                              "info": {"duration_secs": 120, "plot": f"Épisode de démonstration : {title}."}}
                             for eid, number, title in rows]
                    for season, rows in episodes.items()
                },
            })
        return self.send_json({
            "user_info": {
                "auth": 1, "status": "Active", "exp_date": str(int(time.time()) + 90 * 86400),
                "max_connections": "1", "active_cons": "0",
            },
            "server_info": {"url": "127.0.0.1", "timezone": "Europe/Paris"},
        })

    def on_demand(self, section, raw_id):
        identifier = raw_id.split(".")[0]
        known = [str(m[0]) for m in MOVIES] if section == "movie" else [
            str(e[0]) for seasons in EPISODES.values() for rows in seasons.values() for e in rows]
        return self.send_file() if identifier in known else self.send_status(404)

    def send_file(self):
        """Fichier servi par plages, avec la règle d'une connexion à la fois, comme un film réel."""
        path = media_file()
        size = os.path.getsize(path)
        token = object()
        if not connections.acquire(token):
            print(f"{time.strftime('%X')} fichier : 401", flush=True)
            return self.send_status(401)
        print(f"{time.strftime('%X')} fichier : ouvert, plage {self.headers.get('Range', 'complète')}", flush=True)
        try:
            start, end = 0, size - 1
            wanted = self.headers.get("Range", "")
            if wanted.startswith("bytes="):
                first, _, last = wanted[6:].partition("-")
                start = int(first or 0)
                end = min(int(last), size - 1) if last else size - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Connection", "close")
            self.end_headers()
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connections.release(token)
            self.close_connection = True

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
    media_file()  # generated once, kept between runs: a first request must not wait for FFmpeg
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Faux panel : http://127.0.0.1:{args.port}  (identifiant {USER}, mot de passe {PASSWORD})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
