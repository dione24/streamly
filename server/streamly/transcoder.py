"""Shared, bounded live workers. Every playback ticket owns its byte budget.

A failed source starts a new generation, never overwriting segments a player
is still reading. Clients detect generations and reload the master playlist.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from fractions import Fraction

_FFMPEG_CAPABILITIES = None


def _ffmpeg_capabilities():
    global _FFMPEG_CAPABILITIES
    if _FFMPEG_CAPABILITIES is not None:
        return _FFMPEG_CAPABILITIES

    caps = {
        'filter_complex_threads': True,
        'force_key_frames_vstream_spec': True,
        'hls_flags': True,
        'hls_independent_segments': True,
        'hls_temp_file': True,
        'hls_delete_threshold': True,
    }

    try:
        full = subprocess.run(
            ['ffmpeg', '-hide_banner', '-h', 'full'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        text = (full.stdout or '') + (full.stderr or '')
        caps['filter_complex_threads'] = '-filter_complex_threads' in text
        caps['force_key_frames_vstream_spec'] = '-force_key_frames[:<stream_spec>]' in text

        muxer = subprocess.run(
            ['ffmpeg', '-hide_banner', '-h', 'muxer=hls'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        muxer_text = (muxer.stdout or '') + (muxer.stderr or '')
        caps['hls_flags'] = '-hls_flags' in muxer_text
        if caps['hls_flags']:
            caps['hls_independent_segments'] = 'independent_segments' in muxer_text
            caps['hls_temp_file'] = 'temp_file' in muxer_text
            caps['hls_delete_threshold'] = 'hls_delete_threshold' in muxer_text
        else:
            caps['hls_independent_segments'] = False
            caps['hls_temp_file'] = False
            caps['hls_delete_threshold'] = False
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        caps = {
            'filter_complex_threads': False,
            'force_key_frames_vstream_spec': False,
            'hls_flags': False,
            'hls_independent_segments': False,
            'hls_temp_file': False,
            'hls_delete_threshold': False,
        }

    _FFMPEG_CAPABILITIES = caps
    return caps

def _bps(value):
    value = str(value).lower()
    return int(float(value[:-1]) * {'k': 1000, 'm': 1000000}[value[-1]]) if value[-1:] in ('k', 'm') else int(float(value))


def probe(source, user_agent):
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-rw_timeout', '5000000',
            '-user_agent', user_agent, '-show_streams', '-show_format',
            '-of', 'json', source], capture_output=True, timeout=7, check=True)
        data = json.loads(result.stdout)
        video = next(s for s in data['streams'] if s['codec_type'] == 'video')
        fps = float(Fraction(video.get('avg_frame_rate') or video.get('r_frame_rate') or '25'))
        return {'height': int(video['height']), 'width': int(video['width']),
                'fps': max(1, min(60, fps or 25)), 'codec': video.get('codec_name'),
                'streams': data['streams'], 'duration': data.get('format', {}).get('duration')}
    except (OSError, ValueError, KeyError, StopIteration, ZeroDivisionError, subprocess.SubprocessError):
        return {'height': 720, 'width': 1280, 'fps': 25, 'unverified': True, 'streams': []}


class CapacityError(RuntimeError):
    pass


class Transcoder:
    def probe_last_error(self, key, max_lines=14):
        """Retourne une erreur ffmpeg récente, lisible par l'UI."""
        path = os.path.join(self.log_dir, 'ffmpeg_%s.log' % key)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                lines = [ln.strip() for ln in fh.readlines() if ln.strip()]
        except OSError:
            return None
        if not lines:
            return None
        for line in reversed(lines[-max_lines:]):
            lowered = line.lower()
            if 'error' in lowered or 'http error' in lowered:
                return line[:240]
        return lines[-1][:240]

    def __init__(self, cfg, hls_dir, log_dir, monitor=True):
        self.cfg, self.hls_dir, self.log_dir = cfg, hls_dir, log_dir
        self.ladder = cfg['ladder']
        self._ffmpeg_caps = _ffmpeg_capabilities()
        self._lock = threading.RLock()
        self.workers, self.tickets, self.reservations = {}, {}, {}
        self.failures = {}
        self._closed = threading.Event()
        os.makedirs(hls_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)
        if monitor:
            threading.Thread(target=self._watchdog, daemon=True).start()

    def _command(self, source, media, generation):
        seg = int(self.cfg.get('segment_seconds', 2))
        fps = float(media['fps'])
        ua = self.cfg.get('user_agent', 'VLC/3.0.20')
        # Never upscale. The manifest uses the same dimensions as the encoder.
        chains = []
        for i, r in enumerate(self.ladder):
            h = min(int(r['height']), media['height']) // 2 * 2
            chains.append('[v%d]scale=-2:%d,setsar=1[o%d]' % (i, h, i))
        fc = '[0:v]split=%d%s;%s' % (len(self.ladder), ''.join('[v%d]' % i for i in range(len(self.ladder))), ';'.join(chains))
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning',
               '-rw_timeout', '12000000', '-user_agent', ua,
               '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '3',
               '-analyzeduration', '2000000', '-probesize', '2000000', '-i', source]
        if self._ffmpeg_caps.get('filter_complex_threads'):
            cmd += ['-filter_complex_threads', '1']
        cmd += ['-filter_complex', fc]
        force_key_frame_expression = 'expr:gte(t,n_forced*%d)' % seg
        for i, r in enumerate(self.ladder):
            cmd += ['-map', '[o%d]' % i, '-c:v:%d' % i, 'libx264',
                    '-threads:v:%d' % i, '1', '-preset:v:%d' % i, self.cfg.get('x264_preset', 'veryfast'),
                    '-pix_fmt:v:%d' % i, 'yuv420p', '-profile:v:%d' % i, 'high',
                    '-b:v:%d' % i, r['bitrate'], '-maxrate:v:%d' % i, r['maxrate'],
                    '-bufsize:v:%d' % i, r['bufsize'], '-r:v:%d' % i, str(fps),
                    '-g:v:%d' % i, str(round(seg * fps)), '-keyint_min:v:%d' % i, str(round(seg * fps)),
                    '-sc_threshold:v:%d' % i, '0']
            if self._ffmpeg_caps.get('force_key_frames_vstream_spec'):
                cmd += ['-force_key_frames:v:%d' % i, force_key_frame_expression]
        if not self._ffmpeg_caps.get('force_key_frames_vstream_spec'):
            cmd += ['-force_key_frames', force_key_frame_expression]
        audio = any(s.get('codec_type') == 'audio' for s in media.get('streams', [])) or media.get('unverified')
        if audio:
            for _ in self.ladder:
                cmd += ['-map', '0:a:0']
            cmd += ['-c:a', 'aac', '-b:a', self.cfg.get('audio_bitrate', '96k'), '-ac', '2']
        variants = ' '.join('v:%d,a:%d' % (i, i) if audio else 'v:%d' % i for i in range(len(self.ladder)))
        hls_flags = ['delete_segments', 'omit_endlist']
        if self._ffmpeg_caps.get('hls_independent_segments'):
            hls_flags.append('independent_segments')
        if self._ffmpeg_caps.get('hls_temp_file'):
            hls_flags.append('temp_file')
        cmd += ['-f', 'hls', '-hls_time', str(seg), '-hls_list_size', str(max(36, int(self.cfg.get('playlist_size', 36))))]
        if self._ffmpeg_caps.get('hls_flags'):
            cmd += ['-hls_flags', '+'.join(hls_flags)]
        cmd += ['-hls_segment_type', 'mpegts', '-var_stream_map', variants]
        if self._ffmpeg_caps.get('hls_delete_threshold'):
            cmd += ['-hls_delete_threshold', '12']
        # Le motif de sortie et son nom de segment restent en dernier : toute
        # option inseree entre une option et sa valeur la detourne
        # silencieusement.
        cmd += ['-hls_segment_filename', 'g%d_%%v_%%09d.ts' % generation,
                'g%d_s_%%v.m3u8' % generation]
        return cmd

    def _provider_limit(self, pid):
        p = next((p for p in self.cfg.get('providers', []) if p['id'] == pid), {})
        return max(1, int(p.get('max_connections', 1)))

    def _available(self, pid, excluding=None):
        used = sum(w['provider'] == pid and w['key'] != excluding and w['state'] != 'failed' for w in self.workers.values())
        used += sum(p == pid for p in self.reservations.values())
        return used < self._provider_limit(pid)

    def reserve(self, job, provider):
        with self._lock:
            if len(self.workers) + len(self.reservations) >= max(1, int(self.cfg.get('max_concurrent_streams', 1))) or not self._available(provider):
                raise CapacityError('Une lecture ou préparation utilise déjà cette capacité. Réessayez après son arrêt.')
            self.reservations[job] = provider

    def unreserve(self, job):
        with self._lock:
            self.reservations.pop(job, None)

    def open(self, owner, identity, sources, label='', ceiling=0, budget=0):
        key = hashlib.sha256(identity.encode()).hexdigest()[:20]
        with self._lock:
            self._expire_locked()
            w = self.workers.get(key)
            if not w:
                if len(self.workers) + len(self.reservations) >= max(1, int(self.cfg.get('max_concurrent_streams', 1))):
                    raise CapacityError('Le serveur a atteint sa limite de chaînes simultanées. Une chaîne déjà ouverte peut être partagée.')
                sources = [s for s in sources if self._available(s['provider'])]
                sources.sort(key=lambda s: time.time() - self.failures.get(s['url'], 0) < 120)
                if not sources:
                    raise CapacityError('Toutes les connexions de ces abonnements sont occupées.')
                w = dict(key=key, sources=sources, index=0, provider=sources[0]['provider'],
                         generation=0, proc=None, state='starting', started=time.time(),
                         last=time.time(), label=label, failovers=0, media=None, error=None)
                self.workers[key] = w
                threading.Thread(target=self._spawn, args=(key,), daemon=True).start()
            elif w['state'] == 'failed':
                raise CapacityError('Les sources de cette chaîne sont indisponibles. Arrêtez la lecture puis réessayez.')
            ticket = secrets.token_urlsafe(24)
            self.tickets[ticket] = dict(owner=owner, key=key, last=time.time(), created=time.time(),
                                        bytes=0, ceiling=max(0, int(ceiling)), budget=max(0, int(budget)))
            return ticket

    def _spawn(self, key):
        with self._lock:
            w = self.workers.get(key)
            if not w:
                return
            generation = w['generation']
            source = w['sources'][w['index']]['url']
        media = probe(source, self.cfg.get('user_agent', 'VLC/3.0.20'))
        with self._lock:
            if self.workers.get(key) is not w or w['generation'] != generation:
                return
            outdir = os.path.join(self.hls_dir, key)
            os.makedirs(outdir, exist_ok=True)
            w['media'], w['started'], w['error'] = media, time.time(), None
            try:
                # Keep FFmpeg's credential-bearing errors private; cap each generation log.
                with open(os.path.join(self.log_dir, 'ffmpeg_%s.log' % key), 'wb') as log:
                    w['proc'] = subprocess.Popen(self._command(source, media, generation), cwd=outdir, stdout=log, stderr=log)
                w['state'] = 'buffering'
            except OSError:
                w['state'] = 'failed'

    def ticket(self, ticket, owner=None, touch=True):
        with self._lock:
            t = self.tickets.get(ticket)
            if not t or (owner is not None and t['owner'] != owner) or time.time() - t['last'] > 180:
                return None
            if touch:
                t['last'] = time.time()
            return dict(t)

    def charge(self, ticket, size):
        with self._lock:
            t = self.tickets.get(ticket)
            if not t or (t['budget'] and t['bytes'] + size > t['budget']):
                return False
            t['bytes'] += size
            t['last'] = time.time()
            return True

    def allowed_levels(self, t):
        ceiling = t.get('ceiling', 0)
        audio = _bps(self.cfg.get('audio_bitrate', '96k'))
        levels = [i for i, r in enumerate(self.ladder) if not ceiling or (_bps(r['maxrate']) + audio) * 1.08 <= ceiling]
        return levels

    def master_playlist(self, ticket):
        with self._lock:
            t = self.tickets[ticket]
            w = self.workers[t['key']]
            media = w['media'] or {'width': 1280, 'height': 720, 'fps': 25}
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            for i in self.allowed_levels(t):
                r = self.ladder[i]
                h = min(int(r['height']), media['height']) // 2 * 2
                width = round(h * media['width'] / media['height'] / 2) * 2
                # Include transport overhead; do not invent codec level strings.
                bw = int((_bps(r['maxrate']) + _bps(self.cfg.get('audio_bitrate', '96k'))) * 1.08)
                lines += ['#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,FRAME-RATE=%.3f' % (bw, width, h, media['fps']),
                          'g%d_s_%d.m3u8' % (w['generation'], i)]
            return '\n'.join(lines) + '\n'

    def status(self, ticket=None):
        with self._lock:
            def public(w):
                return {k: w[k] for k in ('key', 'label', 'generation', 'state', 'failovers', 'provider')} | {
                    'uptime_s': round(time.time() - w['started']), 'media': {
                        k: v for k, v in (w['media'] or {}).items() if k != 'streams'},
                    'viewers': sum(t['key'] == w['key'] for t in self.tickets.values()),
                    'error': w.get('error')}
            if ticket:
                t = self.tickets.get(ticket)
                if not t or t['key'] not in self.workers:
                    return None
                return dict(public(self.workers[t['key']]), bytes=t['bytes'], budget=t['budget'], ceiling=t['ceiling'])
            return {'running': bool(self.workers), 'workers': [public(w) for w in self.workers.values()],
                    'capacity': int(self.cfg.get('max_concurrent_streams', 1))}

    def release(self, ticket, owner=None):
        with self._lock:
            t = self.tickets.get(ticket)
            if t and (owner is None or owner == t['owner']):
                del self.tickets[ticket]
                if not any(x['key'] == t['key'] for x in self.tickets.values()):
                    self._stop_locked(t['key'])

    def release_owner(self, owner):
        with self._lock:
            for ticket in [k for k, t in self.tickets.items() if t['owner'] == owner]:
                self.release(ticket, owner)

    @staticmethod
    def _kill(proc):
        if proc:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()

    def _stop_locked(self, key):
        w = self.workers.pop(key, None)
        if w:
            self._kill(w['proc'])
            shutil.rmtree(os.path.join(self.hls_dir, key), ignore_errors=True)

    def _expire_locked(self):
        now = time.time()
        for ticket, t in list(self.tickets.items()):
            if now - t['last'] > 180:
                self.release(ticket)

    def _tick(self):
        with self._lock:
            self._expire_locked()
            for key, w in list(self.workers.items()):
                if w['state'] in ('starting', 'failed'):
                    continue
                outdir = os.path.join(self.hls_dir, key)
                files = [os.path.join(outdir, f) for f in os.listdir(outdir) if f.startswith('g%d_' % w['generation']) and f.endswith('.ts')]
                latest = max((os.path.getmtime(f) for f in files if os.path.exists(f)), default=w['started'])
                if files:
                    w['state'] = 'playing'
                proc = w['proc']
                stalled = time.time() - latest > int(self.cfg.get('stall_timeout_seconds', 20))
                if not (stalled or (proc and proc.poll() is not None)):
                    continue
                latest_error = self.probe_last_error(w['key'], max_lines=24)
                if latest_error:
                    w['error'] = latest_error
                self.failures[w['sources'][w['index']]['url']] = time.time()
                self.failures = {url: at for url, at in self.failures.items() if time.time() - at < 120}
                self._kill(proc)
                candidates = [i for i in range(w['index'] + 1, len(w['sources'])) if self._available(w['sources'][i]['provider'], key)]
                if not candidates:
                    w['state'] = 'failed'
                    continue
                w['index'] = candidates[0]
                w['provider'] = w['sources'][w['index']]['provider']
                w['generation'] += 1
                w['failovers'] += 1
                w['state'] = 'starting'
                # Retain the previous generation briefly; bounded by source count.
                for f in os.listdir(outdir):
                    m = re.match(r'g(\d+)_', f)
                    if m and int(m[1]) < w['generation'] - 1:
                        try: os.unlink(os.path.join(outdir, f))
                        except FileNotFoundError: pass
                threading.Thread(target=self._spawn, args=(key,), daemon=True).start()

    def _watchdog(self):
        while not self._closed.wait(2):
            try:
                self._tick()
            except OSError:
                continue

    def close(self):
        self._closed.set()
        with self._lock:
            for key in list(self.workers):
                self._stop_locked(key)
            self.tickets.clear()
