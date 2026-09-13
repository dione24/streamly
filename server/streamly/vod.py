"""Bounded movie preparation: compatible MP4 downloads and adaptive HLS.

No provider credentials are persisted in job metadata. Preparation is explicit,
reserves an upstream connection, and exposes only completed files.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from .transcoder import probe, CapacityError, _bps


class Movies:
    def __init__(self, cfg, root, catalog, transcoder):
        self.cfg, self.root, self.catalog, self.transcoder = cfg, root, catalog, transcoder
        self.jobs, self.lock = {}, threading.RLock()
        os.makedirs(root, exist_ok=True)
        to_resume = []
        for name in os.listdir(root):
            try:
                with open(os.path.join(root, name, 'job.json')) as f:
                    job = json.load(f)
                if job['state'] == 'ready':
                    self.jobs[name] = job
                    continue
                if job['state'] == 'preparing':
                    job.setdefault('progress', 0)
                    job.setdefault('error', None)
                    job['state'] = 'preparing'
                    job['error'] = 'Reprise automatique de la préparation en cours…'
                    self.jobs[name] = job
                    to_resume.append(job)
                    self._save(job)
                    continue
                if job['state'] != 'ready':
                    job['state'] = 'failed'
                    job['error'] = job.get('error') or 'Préparation interrompue par un redémarrage.'
                self.jobs[name] = job
            except (OSError, ValueError, KeyError):
                continue
        for job in to_resume:
            try:
                _, source = self._movie_source(job['provider'], job['movie'])
            except Exception as exc:
                with self.lock:
                    job.update(state='failed', error=str(exc))
                    self._save(job)
                continue
            self._start(job, source, clear_files=True)

    def _provider(self, pid):
        for provider in self.cfg.get('providers', []):
            if provider.get('id') == pid:
                return provider
        return None

    def _movie_source(self, pid, sid):
        provider = self._provider(pid)
        movie = self.catalog.vod_get(pid, sid) if provider else None
        if not provider or not provider.get('enabled', True) or not movie:
            raise ValueError('film indisponible')
        container = (movie.get('container') or 'mp4').lstrip('.')
        if not re.fullmatch(r'[a-zA-Z0-9]+', container):
            raise ValueError('conteneur invalide')
        return movie, '%s/movie/%s/%s/%s.%s' % (
            provider['host'].rstrip('/'), provider['username'], provider['password'],
            int(sid), container)

    def _cleanup(self, path):
        for filename in os.listdir(path):
            if filename == 'job.json':
                continue
            try:
                os.unlink(os.path.join(path, filename))
            except OSError:
                pass

    def _start(self, job, source, clear_files=False):
        try:
            self.transcoder.reserve(job['id'], job['provider'])
        except CapacityError as exc:
            job.update(state='failed', error=str(exc), progress=0)
            self._save(job)
            return
        if clear_files:
            self._cleanup(os.path.join(self.root, job['id']))
        threading.Thread(target=self._run, args=(job, source), daemon=True).start()

    def _save(self, job):
        path = os.path.join(self.root, job['id'], 'job.json')
        with open(path + '.tmp', 'w') as f:
            json.dump(job, f)
        os.replace(path + '.tmp', path)

    def info(self, source):
        media = probe(source, self.cfg.get('user_agent', 'VLC/3.0.20'))
        return {'duration': media.get('duration'), 'verified': not media.get('unverified'),
                'audio': [{'index': s['index'], 'label': (s.get('tags') or {}).get('language', 'Piste %s' % s['index']), 'codec': s.get('codec_name')} for s in media['streams'] if s.get('codec_type') == 'audio'],
                'subtitles': [{'index': s['index'], 'label': (s.get('tags') or {}).get('language', 'Sous-titres %s' % s['index']),
                    'supported': s.get('codec_name') in ('subrip', 'ass', 'ssa', 'webvtt', 'mov_text')} for s in media['streams'] if s.get('codec_type') == 'subtitle']}

    def start(self, movie, source, height=480, audio=None, subtitle=None):
        if height not in (240, 360, 480, 720):
            raise ValueError('Qualité invalide')
        with self.lock:
            same = [j for j in self.jobs.values() if j['provider'] == movie['provider_id'] and j['movie'] == movie['stream_id'] and j['height'] == height and j.get('audio') == audio and j.get('subtitle') == subtitle and j['state'] in ('preparing', 'ready')]
            if same:
                return same[0]
            for key, j in list(self.jobs.items()):
                if j['state'] != 'preparing' and time.time() - max(j['created'], j.get('last_access', 0)) > 7 * 86400:
                    shutil.rmtree(os.path.join(self.root, key), ignore_errors=True)
                    del self.jobs[key]
            used = sum(os.path.getsize(os.path.join(p, f)) for p, _, fs in os.walk(self.root) for f in fs)
            if used > int(self.cfg.get('vod_cache_bytes', 8_000_000_000)) * .75 or shutil.disk_usage(self.root).free < 2_000_000_000:
                raise CapacityError('Espace de préparation insuffisant. Libérez une ancienne préparation.')
            jid = secrets.token_urlsafe(18)
            job = dict(id=jid, provider=movie['provider_id'], movie=movie['stream_id'], title=movie['title'],
                       height=height, audio=audio, subtitle=subtitle, state='preparing', created=time.time(), progress=0)
            self.jobs[jid] = job
            os.makedirs(os.path.join(self.root, jid))
            self._save(job)
            self._start(job, source)
            return dict(job)

    def _execute(self, cmd, job, duration=0):
        path = os.path.join(self.root, job['id'])
        with open(os.path.join(path, 'progress.txt'), 'w') as progress:
            proc = subprocess.Popen(cmd, cwd=path, stdout=progress, stderr=subprocess.DEVNULL)
        started = time.time()
        try:
            while proc.poll() is None:
                time.sleep(1)
                if shutil.disk_usage(path).free < 1_000_000_000 or time.time() - started > 4 * 3600:
                    raise RuntimeError('Préparation arrêtée : limite de temps ou espace disque atteint.')
                used = sum(os.path.getsize(os.path.join(p, f)) for p, _, fs in os.walk(self.root) for f in fs)
                if used > int(self.cfg.get('vod_cache_bytes', 8_000_000_000)):
                    raise RuntimeError('Limite du cache vidéo atteinte.')
                if duration:
                    try:
                        text = open(os.path.join(path, 'progress.txt')).read()
                        times = re.findall(r'out_time_us=(\d+)', text)
                        if times:
                            job['progress'] = min(95, round(int(times[-1]) / 1e6 / duration * 95))
                    except OSError:
                        pass
            if proc.returncode:
                raise RuntimeError('Source indisponible ou format non pris en charge.')
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def _run(self, job, source):
        path = os.path.join(self.root, job['id'])
        try:
            media = probe(source, self.cfg.get('user_agent', 'VLC/3.0.20'))
            if media.get('unverified'):
                raise RuntimeError('Impossible de vérifier le format du film.')
            audio = [s['index'] for s in media['streams'] if s['codec_type'] == 'audio']
            sub = {s['index']: s.get('codec_name') for s in media['streams'] if s['codec_type'] == 'subtitle'}
            if job['audio'] is not None and job['audio'] not in audio:
                raise RuntimeError('Piste audio invalide.')
            if job['subtitle'] is not None and sub.get(job['subtitle']) not in ('subrip', 'ass', 'ssa', 'webvtt', 'mov_text'):
                raise RuntimeError('Ces sous-titres ne sont pas disponibles au format texte.')
            rungs = [r for r in self.cfg['ladder'] if r['height'] <= job['height']]
            cmd = ['ffmpeg', '-y', '-nostdin', '-v', 'error', '-progress', 'pipe:1', '-rw_timeout', '15000000',
                   '-user_agent', self.cfg.get('user_agent', 'VLC/3.0.20'), '-i', source]
            # Each MP4 is independently resumable. HLS below only repackages it.
            for i, r in enumerate(rungs):
                h = min(r['height'], media['height']) // 2 * 2
                cmd += ['-map', '0:v:0']
                if audio:
                    cmd += ['-map', '0:%d' % (job['audio'] if job['audio'] is not None else audio[0])]
                cmd += ['-vf', 'scale=-2:%d,setsar=1' % h, '-c:v', 'libx264', '-threads', '1', '-preset', 'veryfast',
                        '-pix_fmt', 'yuv420p', '-b:v', r['bitrate'], '-maxrate', r['maxrate'], '-bufsize', r['bufsize'],
                        '-force_key_frames', 'expr:gte(t,n_forced*4)', '-sc_threshold', '0',
                        '-c:a', 'aac', '-b:a', '96k', '-ac', '2', '-movflags', '+faststart', 'q%d.mp4' % i]
            if job['subtitle'] is not None:
                cmd += ['-map', '0:%d' % job['subtitle'], '-c:s', 'webvtt', 'subtitles.vtt']
            duration = float(media.get('duration') or 0)
            self._execute(cmd, job, duration)
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            for i, r in enumerate(rungs):
                self._execute(['ffmpeg', '-y', '-nostdin', '-v', 'error', '-i', 'q%d.mp4' % i, '-c', 'copy',
                    '-f', 'hls', '-hls_time', '4', '-hls_playlist_type', 'vod', '-hls_segment_filename', 'q%d_%%06d.ts' % i, 'q%d.m3u8' % i], job)
                h = min(r['height'], media['height']) // 2 * 2
                width = round(h * media['width'] / media['height'] / 2) * 2
                lines += ['#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d' % (int((_bps(r['maxrate']) + 96000) * 1.08), width, h), 'q%d.m3u8' % i]
            with open(os.path.join(path, 'master.m3u8'), 'w') as f:
                f.write('\n'.join(lines) + '\n')
            job.update(state='ready', progress=100, duration=duration,
                       size_bytes=os.path.getsize(os.path.join(path, 'q0.mp4')), download='q0.mp4',
                       qualities=[r['height'] for r in rungs], subtitles=job['subtitle'] is not None)
        except Exception as exc:
            job.update(state='failed', error=str(exc) if isinstance(exc, RuntimeError) else 'Échec de la préparation.')
            self._cleanup(path)
        finally:
            self._save(job)
            self.transcoder.unreserve(job['id'])

    def retry(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return None
            if job['state'] == 'preparing':
                return dict(job)
            try:
                _, source = self._movie_source(job['provider'], job['movie'])
            except Exception as exc:
                job.update(state='failed', error=str(exc))
                self._save(job)
                return dict(job)
            job.update(state='preparing', progress=0, error='Reprise demandée par l’utilisateur…', created=time.time())
            self._save(job)
        self._start(job, source, clear_files=True)
        return dict(job)

    def list(self):
        with self.lock:
            return [dict(j) for j in sorted(self.jobs.values(), key=lambda j: j['created'], reverse=True)]
