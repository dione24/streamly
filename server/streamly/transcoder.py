"""Transcodage a la demande vers une echelle HLS multi-debits.

Choix de conception, chacun issu d'une mesure sur un panel reel :

  - Tous les barreaux sont **encodes**, y compris le plus haut. Recopier le
    flux source (`-c:v copy`) est gratuit en CPU mais produit des segments
    calques sur les keyframes de la source, donc irreguliers (mesure : 0,68 a
    4,96 s). Les frontieres ne coincident alors plus entre barreaux et la
    bascule ABR hoquete. Tout encoder avec `-g` force donne des segments a
    2,000 s exactement — et s'est revele *moins* couteux (0,94 vCPU contre
    1,30) car on decode alors une source 720p au lieu d'une 1080p.

  - On ingere de preference le barreau 720p du panel plutot que le 1080p :
    2,77 vCPU -> 0,94 vCPU pour un rendu final identique.

  - Un seul flux a la fois par defaut : un abonnement Xtream est souvent
    limite a une connexion simultanee.
"""
import os
import re
import shutil
import subprocess
import threading
import time

# Profils H.264 annonces dans la playlist maitre selon la definition.
_PROFILE_HIGH = "avc1.64001f"   # High @ 3.1
_PROFILE_MAIN = "avc1.64001e"   # High @ 3.0


def _bps(value):
    """'1500k' -> 1500000"""
    s = str(value).strip().lower()
    if s.endswith("k"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("m"):
        return int(float(s[:-1]) * 1000000)
    return int(float(s))


class Transcoder:
    def __init__(self, cfg, hls_dir, log_dir):
        self.cfg = cfg
        self.hls_dir = hls_dir
        self.log_dir = log_dir
        self.ladder = cfg["ladder"]
        self._lock = threading.RLock()
        self._current = None      # {"key","proc","started","last","source"}
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ------------------------------------------------------------ commande

    def _command(self, source_url):
        c = self.cfg
        seg = int(c.get("segment_seconds", 2))
        fps_guess = 25                      # GOP = seg * fps
        gop = str(seg * fps_guess)

        split = "".join("[v%d]" % i for i in range(len(self.ladder)))
        chains = ";".join(
            "[v%d]scale=-2:%d[o%d]" % (i, r["height"], i)
            for i, r in enumerate(self.ladder))
        fcomplex = "[0:v]split=%d%s;%s" % (len(self.ladder), split, chains)

        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-user_agent", c.get("user_agent", "VLC/3.0.20 LibVLC/3.0.20"),
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            # Par defaut ffmpeg analyse le flux d'entree pendant ~5 s avant
            # d'emettre quoi que ce soit. Sur un MPEG-TS a flux unique c'est
            # inutile et cela retarde d'autant l'apparition de l'image.
            "-analyzeduration", "2000000", "-probesize", "2000000",
            "-i", source_url,
            "-filter_complex", fcomplex,
        ]

        for i, rung in enumerate(self.ladder):
            cmd += [
                "-map", "[o%d]" % i,
                "-c:v:%d" % i, "libx264",
                "-preset:v:%d" % i, c.get("x264_preset", "veryfast"),
                "-b:v:%d" % i, rung["bitrate"],
                "-maxrate:v:%d" % i, rung["maxrate"],
                "-bufsize:v:%d" % i, rung["bufsize"],
            ]

        # Keyframes forcees : c'est ce qui garantit des segments alignes
        # entre tous les barreaux, donc une bascule ABR sans coupure.
        cmd += ["-g", gop, "-keyint_min", gop, "-sc_threshold", "0"]

        for _ in self.ladder:
            cmd += ["-map", "a:0"]
        cmd += ["-c:a", "aac", "-b:a", c.get("audio_bitrate", "96k"), "-ac", "2"]

        var_map = " ".join("v:%d,a:%d" % (i, i) for i in range(len(self.ladder)))
        cmd += [
            "-f", "hls",
            "-hls_time", str(seg),
            "-hls_list_size", str(c.get("playlist_size", 6)),
            "-hls_flags", "delete_segments+independent_segments+omit_endlist",
            "-hls_segment_type", "mpegts",
            "-var_stream_map", var_map,
            "s_%v.m3u8",
        ]
        return cmd

    def master_playlist(self, base_url=""):
        """Playlist maitre generee par nos soins.

        ffmpeg annonce mal la bande passante (et pas du tout pour un flux
        copie), ce qui fait choisir le mauvais barreau au lecteur. Comme on
        connait l'echelle, on ecrit l'annonce exacte.
        """
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        audio = _bps(self.cfg.get("audio_bitrate", "96k"))
        for i, rung in enumerate(self.ladder):
            h = int(rung["height"])
            w = int(round(h * 16 / 9 / 2) * 2)
            bw = _bps(rung["maxrate"]) + audio
            codec = _PROFILE_HIGH if h >= 720 else _PROFILE_MAIN
            lines.append(
                '#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,'
                'CODECS="%s,mp4a.40.2",NAME="%s"' % (bw, w, h, codec, rung["name"]))
            lines.append("%ss_%d.m3u8" % (base_url, i))
        return "\n".join(lines) + "\n"

    # -------------------------------------------------------------- cycle

    def start(self, key, sources, meta=None):
        """Lance (ou reutilise) le transcodage pour `key`.

        `sources` est la liste ordonnee des URLs candidates pour cette chaine :
        variantes de qualite, flux de secours du panel, puis memes chaines chez
        les autres providers. Si un flux meurt, on passe au suivant.
        """
        if isinstance(sources, str):
            sources = [sources]
        with self._lock:
            cur = self._current
            if cur and cur["key"] == key and cur["proc"].poll() is None:
                cur["last"] = time.time()
                return cur

            self._stop_locked()
            self._current = {
                "key": key, "sources": list(sources), "index": 0,
                "meta": meta or {}, "last": time.time(), "proc": None,
                "started": 0, "source": None, "failovers": 0,
            }
            self._spawn_locked()
            return self._current

    def _spawn_locked(self):
        """Demarre ffmpeg sur la source candidate courante."""
        cur = self._current
        key = cur["key"]
        source = cur["sources"][cur["index"]]

        outdir = os.path.join(self.hls_dir, key)
        shutil.rmtree(outdir, ignore_errors=True)
        os.makedirs(outdir, exist_ok=True)

        logfile = os.path.join(self.log_dir,
                               "ffmpeg_%s.log" % re.sub(r"\W+", "_", key))
        handle = open(logfile, "ab")
        cur["proc"] = subprocess.Popen(self._command(source), cwd=outdir,
                                       stdout=handle, stderr=handle)
        cur["started"] = time.time()
        cur["source"] = source
        cur["log"] = logfile

    def _failover_locked(self):
        """Passe a la source suivante. Retourne False s'il n'en reste plus."""
        cur = self._current
        if not cur or cur["index"] + 1 >= len(cur["sources"]):
            return False
        cur["index"] += 1
        cur["failovers"] += 1
        self._spawn_locked()
        return True

    def touch(self, key):
        with self._lock:
            if self._current and self._current["key"] == key:
                self._current["last"] = time.time()

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        cur = self._current
        if cur:
            proc = cur.get("proc")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            shutil.rmtree(os.path.join(self.hls_dir, cur["key"]), ignore_errors=True)
        self._current = None

    def status(self):
        with self._lock:
            cur = self._current
            if not cur:
                return {"running": False}
            proc = cur.get("proc")
            return {
                "running": bool(proc and proc.poll() is None),
                "key": cur["key"],
                "meta": cur["meta"],
                "uptime_s": round(time.time() - cur["started"], 1),
                "idle_s": round(time.time() - cur["last"], 1),
                "ladder": [r["name"] for r in self.ladder],
                "source_index": cur["index"],
                "sources_total": len(cur["sources"]),
                "failovers": cur["failovers"],
            }

    def is_alive(self, key):
        with self._lock:
            cur = self._current
            if not cur or cur["key"] != key:
                return False
            proc = cur.get("proc")
            # Un basculement en cours ne doit pas etre vu comme un arret.
            return bool(proc and proc.poll() is None) or cur["index"] + 1 < len(cur["sources"])

    def _watchdog(self):
        """Surveille le flux courant.

        Deux roles : couper un flux inactif, ce qui libere la connexion unique
        du provider ; et basculer sur la source suivante si celle en cours
        meurt alors que quelqu'un regarde encore — le cas typique d'un flux
        qui lache en plein match.
        """
        timeout = int(self.cfg.get("idle_timeout_seconds", 120))
        while True:
            time.sleep(2)
            with self._lock:
                cur = self._current
                if not cur:
                    continue
                idle = time.time() - cur["last"]
                proc = cur.get("proc")
                if idle > timeout:
                    self._stop_locked()
                elif proc and proc.poll() is not None:
                    if not self._failover_locked():
                        self._stop_locked()
