"""Films et episodes : lecture pendant la preparation, puis MP4 telechargeable.

Le serveur annonce d'emblee une playlist VOD complete (un segment toutes les
SEGMENT secondes sur toute la duree) et encode les segments dans l'ordre. Un
segment demande avant d'exister fait patienter le lecteur s'il arrive bientot ;
plus loin, l'encodage repart de ce point (`-ss`), le reste du film sera comble
ensuite. Chaque passe d'encodage (« run ») ecrit ses propres fichiers et sa
propre playlist ffmpeg : seul un segment liste par ffmpeg est complet, les
autres sont ignores puis effaces.

Aucun identifiant fournisseur n'est ecrit dans les metadonnees de preparation.
"""
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from .transcoder import probe, CapacityError, _bps

SEGMENT = 4
# Segments d'avance qu'on attend plutot que de relancer l'encodage.
LOOKAHEAD = 6
TEXT_SUBTITLES = ('subrip', 'ass', 'ssa', 'webvtt', 'mov_text')
_RUN_PLAYLIST = re.compile(r'^r(\d+)_q(\d+)\.m3u8$')
_RUN_SEGMENT = re.compile(r'^r(\d+)_q(\d+)_(\d+)\.ts$')


# Sans segment demande depuis ce delai, personne ne regarde la preparation :
# elle peut ceder sa connexion a une lecture qui la reclame.
IDLE_BEFORE_PAUSE = 60


class _Paused(Exception):
    """La preparation cede sa connexion ; elle reprendra ou elle en etait."""


class Movies:
    def __init__(self, cfg, root, catalog, transcoder):
        self.cfg, self.root, self.catalog, self.transcoder = cfg, root, catalog, transcoder
        self.jobs, self.lock = {}, threading.RLock()
        self.changed = threading.Condition(self.lock)
        # Etat d'encodage en memoire : index des segments complets, passe en cours.
        self.runtime = {}
        # Adresse de chaque preparation lancee : une pause repart sans relire le catalogue.
        self._sources = {}
        self._closed = threading.Event()
        os.makedirs(root, exist_ok=True)
        to_resume = []
        for name in os.listdir(root):
            try:
                with open(os.path.join(root, name, 'job.json')) as f:
                    job = json.load(f)
                if job['state'] == 'ready':
                    self.jobs[name] = job
                    if job.get('format') == 2:
                        self.runtime[name] = self._load_runtime(job)
                    continue
                if job['state'] in ('preparing', 'paused'):
                    job['state'] = 'preparing'
                    job.setdefault('progress', 0)
                    job['error'] = 'Reprise automatique de la préparation en cours…'
                    self.jobs[name] = job
                    to_resume.append(job)
                    self._save(job)
                    continue
                job['state'] = 'failed'
                job['error'] = job.get('error') or 'Préparation interrompue par un redémarrage.'
                self.jobs[name] = job
            except (OSError, ValueError, KeyError):
                continue
        for job in to_resume:
            try:
                source = self._job_source(job)
            except Exception as exc:
                with self.lock:
                    job.update(state='failed', error=str(exc))
                    self._save(job)
                continue
            # Une preparation au nouveau format reprend ses segments deja faits.
            self._start(job, source, clear_files=job.get('format') != 2)

    def _provider(self, pid):
        for provider in self.cfg.get('providers', []):
            if provider.get('id') == pid:
                return provider
        return None

    def _movie_source(self, pid, sid, kind='movie'):
        provider = self._provider(pid)
        # Film et episode ont chacun leur numerotation, et leur chemin chez le
        # panel : un episode relu sous /movie/ designerait un autre fichier.
        if kind == 'episode':
            movie = self.catalog.episode_get(pid, sid) if provider else None
        else:
            movie = self.catalog.vod_get(pid, sid) if provider else None
        if not provider or not provider.get('enabled', True) or not movie:
            raise ValueError('épisode indisponible' if kind == 'episode' else 'film indisponible')
        container = (movie.get('container') or 'mp4').lstrip('.')
        if not re.fullmatch(r'[a-zA-Z0-9]+', container):
            raise ValueError('conteneur invalide')
        return movie, '%s/%s/%s/%s/%s.%s' % (
            provider['host'].rstrip('/'), 'series' if kind == 'episode' else 'movie',
            provider['username'], provider['password'], int(sid), container)

    def _job_source(self, job):
        return self._movie_source(job['provider'], job['movie'], job.get('kind', 'movie'))[1]

    def _cleanup(self, path):
        for filename in os.listdir(path):
            if filename == 'job.json':
                continue
            try:
                os.unlink(os.path.join(path, filename))
            except OSError:
                pass

    def _start(self, job, source, clear_files=False, new=False, preempt=False):
        """Lance l'encodage si l'abonnement a une connexion libre.

        `preempt` : une demande de l'utilisateur met en pause les preparations
        qui tiennent la connexion sans etre regardees. Sans connexion, une
        nouvelle preparation leve CapacityError (rien n'est garde) ; une
        reprise se met en pause et repartira a la prochaine place libre.
        """
        try:
            self.transcoder.reserve(job['id'], job['provider'])
        except CapacityError as exc:
            if not (preempt and self._make_room(job['provider'], job.get('account'), keep=job['id'])):
                return self._no_room(job, exc, new)
            try:
                self.transcoder.reserve(job['id'], job['provider'])
            except CapacityError as exc:
                return self._no_room(job, exc, new)
        if clear_files:
            self._cleanup(os.path.join(self.root, job['id']))
        with self.lock:
            rt = self.runtime[job['id']] = self._load_runtime(job)
            rt['seen'] = time.time()
            self._sources[job['id']] = source
        threading.Thread(target=self._run, args=(job, source), daemon=True).start()

    def _no_room(self, job, exc, new):
        with self.lock:
            if new:
                self.jobs.pop(job['id'], None)
                shutil.rmtree(os.path.join(self.root, job['id']), ignore_errors=True)
                raise exc
            job.update(state='paused', error='En pause : la connexion de l’abonnement est occupée. Reprise automatique dès qu’elle se libère.')
            self._save(job)

    def _make_room(self, provider, account, keep=None):
        """Met en pause les preparations du meme abonnement que personne ne
        regarde, ou lancees par le meme compte (il est passe a autre chose).
        Rend vrai si une connexion a ete rendue."""
        now = time.time()
        with self.lock:
            paused = []
            for jid, j in self.jobs.items():
                rt = self.runtime.get(jid)
                if jid == keep or j['state'] != 'preparing' or j['provider'] != provider or not rt:
                    continue
                if (account is not None and j.get('account') == account) or now - rt.get('seen', 0) > IDLE_BEFORE_PAUSE:
                    rt['paused'] = True
                    for proc in (rt.get('proc'), rt.get('subs_proc')):
                        if proc and proc.poll() is None:
                            proc.kill()
                    paused.append(jid)
            self.changed.notify_all()
        # Le fil d'encodage voit la marque et rend sa connexion.
        deadline = time.time() + 10
        held = lambda: any(k in self.transcoder.reservations for jid in paused for k in (jid, jid + ':st'))
        while paused and held() and time.time() < deadline:
            time.sleep(.1)
        return bool(paused) and not held()

    def _resume_paused(self):
        """Une connexion vient de se liberer : la plus ancienne pause repart."""
        with self.lock:
            waiting = sorted((j for j in self.jobs.values() if j['state'] == 'paused'), key=lambda j: j['created'])
        for job in waiting:
            try:
                source = self._sources.get(job['id']) or self._job_source(job)
            except Exception:
                continue
            with self.lock:
                if job['state'] != 'paused' or self.jobs.get(job['id']) is not job:
                    continue
                job.update(state='preparing', error=None)
            self._start(job, source)
            if job['state'] == 'preparing':
                return

    def _save(self, job):
        path = os.path.join(self.root, job['id'], 'job.json')
        with open(path + '.tmp', 'w') as f:
            json.dump(job, f)
        os.replace(path + '.tmp', path)

    def _used_bytes(self):
        return sum(os.path.getsize(os.path.join(p, f)) for p, _, fs in os.walk(self.root) for f in fs)

    def info(self, source):
        media = probe(source, self.cfg.get('user_agent', 'VLC/3.0.20'))
        return {'duration': media.get('duration'), 'verified': not media.get('unverified'),
                'audio': [{'index': s['index'], 'label': (s.get('tags') or {}).get('language', 'Piste %s' % s['index']), 'codec': s.get('codec_name')} for s in media['streams'] if s.get('codec_type') == 'audio'],
                'subtitles': [{'index': s['index'], 'label': (s.get('tags') or {}).get('language', 'Sous-titres %s' % s['index']),
                    'supported': s.get('codec_name') in TEXT_SUBTITLES} for s in media['streams'] if s.get('codec_type') == 'subtitle']}

    def start(self, movie, source, height=480, audio=None, subtitle=None, account=None):
        if height not in (240, 360, 480, 720):
            raise ValueError('Qualité invalide')
        with self.lock:
            # Film et episode ont chacun leur numerotation chez le panel : un meme
            # numero peut designer les deux.
            kind = movie.get('kind') or 'movie'
            same = [j for j in self.jobs.values() if j['provider'] == movie['provider_id'] and j['movie'] == movie['stream_id'] and j.get('kind', 'movie') == kind and j['height'] == height and j.get('audio') == audio and j.get('subtitle') == subtitle and j['state'] in ('preparing', 'ready', 'paused')]
            if same and same[0]['state'] == 'paused':
                paused = same[0]
            elif same:
                same[0]['account'] = account
                return dict(same[0])
            else:
                paused = None
            for key, j in list(self.jobs.items()):
                if j['state'] != 'preparing' and time.time() - max(j['created'], j.get('last_access', 0)) > 7 * 86400:
                    shutil.rmtree(os.path.join(self.root, key), ignore_errors=True)
                    del self.jobs[key]
            if self._used_bytes() > int(self.cfg.get('vod_cache_bytes', 8_000_000_000)) * .75 or shutil.disk_usage(self.root).free < 2_000_000_000:
                raise CapacityError('Espace de préparation insuffisant. Libérez une ancienne préparation.')
            jid = secrets.token_urlsafe(18)
        if paused:
            return self.retry(paused['id'], account)
        with self.lock:
            jid = secrets.token_urlsafe(18)
            job = dict(id=jid, provider=movie['provider_id'], movie=movie['stream_id'], title=movie['title'],
                       kind=kind, account=account,
                       height=height, audio=audio, subtitle=subtitle, state='preparing', created=time.time(),
                       progress=0, format=2, playable=False)
            self.jobs[jid] = job
            os.makedirs(os.path.join(self.root, jid))
            self._save(job)
        # Hors du verrou : faire de la place attend que d'autres fils le rendent.
        self._start(job, source, new=True, preempt=True)
        return dict(job)

    # ------------------------------------------------------------ segments

    def _load_runtime(self, job):
        """Index des segments complets deja sur disque (reprise apres redemarrage)."""
        path = os.path.join(self.root, job['id'])
        rt = dict(index={}, run=0, start=0, proc=None, want=None, cursor=0)
        for name in os.listdir(path):
            m = _RUN_PLAYLIST.match(name)
            if m:
                rt['run'] = max(rt['run'], int(m[1]))
                self._read_run(rt, path, int(m[1]), int(m[2]))
        return rt

    @staticmethod
    def _read_run(rt, path, run, rung):
        """Ajoute a l'index les segments qu'une passe a termines."""
        try:
            with open(os.path.join(path, 'r%d_q%d.m3u8' % (run, rung))) as fh:
                names = re.findall(r'^(r%d_q%d_(\d+)\.ts)$' % (run, rung), fh.read(), re.M)
        except OSError:
            return
        index = rt['index'].setdefault(rung, {})
        for name, n in names:
            if int(n) not in index and os.path.exists(os.path.join(path, name)):
                index[int(n)] = name

    def _complete(self, rt, job, n):
        rungs = len(job.get('qualities') or [])
        return rungs > 0 and all(n in rt['index'].get(i, {}) for i in range(rungs))

    @staticmethod
    def _segments(duration):
        """Segments annonces. La duree du conteneur deborde souvent de quelques
        millisecondes (60,005 s avec le FFmpeg 4.4 du serveur) : un segment de
        plus, qui n'existerait jamais, bloquerait le lecteur a la fin du film."""
        return max(1, math.ceil((duration - .5) / SEGMENT))

    @staticmethod
    def _total(job):
        """Nombre de segments ; sans duree connue, la fin n'est connue qu'a l'arrivee."""
        return 10 ** 7 if job.get('open') else (job.get('segments') or 0)

    def _missing(self, rt, job, after=0):
        """Premier segment manquant a partir de `after`, sinon depuis le debut."""
        if job.get('open'):
            n = 0
            while self._complete(rt, job, n):
                n += 1
            return n
        total = job.get('segments') or 0
        for n in list(range(after, total)) + list(range(0, min(after, total))):
            if not self._complete(rt, job, n):
                return n
        return None

    def _head(self, rt, job):
        """Dernier segment d'une suite continue produite par la passe en cours."""
        n = rt['start']
        while self._complete(rt, job, n):
            n += 1
        return n - 1

    def segment(self, jid, rung, n, timeout=15):
        """Chemin d'un segment, en attendant son encodage s'il le faut.

        Renvoie None si le segment n'existe pas ou n'a pas ete produit a temps.
        """
        asked = time.time()
        deadline = asked + timeout
        requested = False
        with self.lock:
            job = self.jobs.get(jid)
            if not job or n < 0 or n >= self._total(job) or rung >= len(job.get('qualities') or []):
                return None
            rt = self.runtime.get(jid)
            if rt:
                rt['seen'] = asked
            while True:
                if self.jobs.get(jid) is not job:
                    return None  # supprimee pendant l'attente
                name = (rt or {}).get('index', {}).get(rung, {}).get(n) if rt else None
                if name:
                    return os.path.join(self.root, jid, name)
                if not rt or job['state'] != 'preparing':
                    return None
                running = rt['proc'] is not None
                near = running and rt['start'] <= n <= self._head(rt, job) + 1 + LOOKAHEAD
                if job.get('open') and not near:
                    return None  # sans duree, pas de saut au-dela de l'encode
                # Un lecteur qui saute abandonne sa requete precedente, mais le
                # serveur continue de l'attendre. Une requete ne demande donc
                # qu'un seul saut, et jamais contre une demande plus recente :
                # sinon deux requetes se volent l'encodage en boucle.
                newest = max(rt.get('asked', 0), rt.get('want_asked', 0) if rt['want'] is not None else 0)
                if not near and not requested and asked >= newest:
                    rt['want'], rt['want_asked'], requested = n, asked, True
                    self.changed.notify_all()
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.changed.wait(min(left, 1))

    # ---------------------------------------------------------------- encodage

    def _command(self, job, source, media, run, start):
        seconds = start * SEGMENT
        cmd = ['ffmpeg', '-y', '-nostdin', '-v', 'error', '-rw_timeout', '15000000',
               '-user_agent', self.cfg.get('user_agent', 'VLC/3.0.20')]
        if seconds:
            cmd += ['-ss', str(seconds)]
        cmd += ['-i', source]
        audio = [s['index'] for s in media['streams'] if s['codec_type'] == 'audio']
        threads = max(0, int(self.cfg.get('encoder_threads', 0)))
        caps = getattr(self.transcoder, '_ffmpeg_caps', None) or {}
        hls_flags = 'temp_file' if caps.get('hls_temp_file') else None
        for i, r in enumerate(job['rungs']):
            cmd += ['-map', '0:v:0']
            if audio:
                cmd += ['-map', '0:%d' % (job['audio'] if job['audio'] is not None else audio[0])]
            cmd += ['-vf', 'scale=-2:%d,setsar=1' % r['height'], '-c:v', 'libx264', '-preset', 'veryfast',
                    '-pix_fmt', 'yuv420p', '-b:v', r['bitrate'], '-maxrate', r['maxrate'], '-bufsize', r['bufsize'],
                    # Images cles a intervalle fixe depuis le point de depart :
                    # le segment n couvre toujours [n*SEGMENT, (n+1)*SEGMENT[.
                    '-force_key_frames', 'expr:gte(t,n_forced*%d)' % SEGMENT, '-sc_threshold', '0']
            if threads:
                cmd += ['-threads', str(threads)]
            if audio:
                cmd += ['-c:a', 'aac', '-b:a', '96k', '-ac', '2']
            cmd += ['-output_ts_offset', str(seconds), '-f', 'hls', '-hls_time', str(SEGMENT),
                    '-hls_list_size', '0', '-hls_playlist_type', 'event', '-start_number', str(start)]
            if hls_flags:
                cmd += ['-hls_flags', hls_flags]
            cmd += ['-hls_segment_filename', 'r%d_q%d_%%06d.ts' % (run, i), 'r%d_q%d.m3u8' % (run, i)]
        return cmd

    def _discard_unlisted(self, rt, path, run):
        """Efface ce qu'une passe interrompue a laisse d'inacheve."""
        listed = {name for rung in rt['index'].values() for name in rung.values()}
        for name in os.listdir(path):
            m = _RUN_SEGMENT.match(name)
            if (m and int(m[1]) == run and name not in listed) or name.endswith('.tmp'):
                try:
                    os.unlink(os.path.join(path, name))
                except OSError:
                    pass

    def _encode(self, job, source, media, rt, started):
        """Une passe d'encodage. Renvoie quand elle finit ou doit ceder la place."""
        path = os.path.join(self.root, job['id'])
        cache = int(self.cfg.get('vod_cache_bytes', 8_000_000_000))
        limit = max(4 * 3600, 3 * float(job['duration']))
        with self.lock:
            want = rt['want']
            if want is not None and not self._complete(rt, job, want):
                start = want
                rt['asked'] = rt.get('want_asked', 0)
            else:
                start = self._missing(rt, job, rt['cursor'])
            rt['want'] = None
            if start is None:
                return 'done'
            if rt.get('deleted'):
                raise RuntimeError('Préparation supprimée.')
            if rt.get('paused'):
                raise _Paused()
            rt['run'] += 1
            run = rt['run']
            rt['start'], rt['cursor'] = start, start
            cmd = self._command(job, source, media, run, start)
            with open(os.path.join(path, 'r%d.log' % run), 'wb') as log:
                rt['proc'] = proc = subprocess.Popen(cmd, cwd=path, stdout=subprocess.DEVNULL, stderr=log)
        produced, last_new, ticks = False, time.time(), 0
        outcome = 'yield'
        try:
            while True:
                ticks += 1
                exited = proc.poll() is not None
                with self.lock:
                    before = sum(len(v) for v in rt['index'].values())
                    for i in range(len(job['rungs'])):
                        self._read_run(rt, path, run, i)
                    if sum(len(v) for v in rt['index'].values()) != before:
                        produced, last_new = True, time.time()
                    head = self._head(rt, job)
                    total = self._total(job)
                    if exited and proc.returncode == 0 and job.get('open') and head >= start:
                        # La fin du fichier donne enfin la duree du film.
                        job.update(open=False, segments=head + 1, duration=(head + 1) * SEGMENT)
                        total = head + 1
                    elif exited and proc.returncode == 0 and head + 1 < total:
                        if head + 1 >= total - 2 and head >= start:
                            # Fin de fichier un peu avant la duree annoncee :
                            # le conteneur arrondissait sa duree.
                            job['segments'] = total = head + 1
                        else:
                            # Fin prematuree : coupure reseau vue comme une fin
                            # de fichier. On ne tronque pas le film pour autant.
                            produced = False
                    extra = max((max(v) for v in rt['index'].values() if v), default=-1) + 1
                    if not job.get('open') and extra > total and all(extra - 1 in v for v in rt['index'].values()):
                        job['segments'] = total = extra
                    if not job.get('open'):
                        done = sum(self._complete(rt, job, k) for k in range(total))
                        job['progress'] = min(99, round(done * 100 / max(1, total)))
                    self.changed.notify_all()
                    want = rt['want']
                    if want is not None and (self._complete(rt, job, want) or start <= want <= head + 1 + LOOKAHEAD):
                        rt['want'] = want = None
                    if exited:
                        rt['cursor'] = head + 1
                        outcome = 'next' if produced or head + 1 >= total else 'failed'
                        break
                    if want is not None:
                        break
                    if head + 1 < total and self._complete(rt, job, head + 1):
                        # La suite existe deja (passe precedente) : on saute.
                        rt['cursor'] = head + 1
                        break
                if self._closed.is_set():
                    raise RuntimeError('Préparation interrompue par l’arrêt du serveur.')
                if rt.get('deleted'):
                    raise RuntimeError('Préparation supprimée.')
                if rt.get('paused'):
                    raise _Paused()
                if shutil.disk_usage(path).free < 1_000_000_000 or time.time() - started > limit:
                    raise RuntimeError('Préparation arrêtée : limite de temps ou espace disque atteint.')
                if ticks % 20 == 0 and self._used_bytes() > cache:
                    raise RuntimeError('Limite du cache vidéo atteinte.')
                if time.time() - last_new > 120:
                    outcome = 'failed'
                    break
                time.sleep(.5)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            with self.lock:
                for i in range(len(job['rungs'])):
                    self._read_run(rt, path, run, i)
                rt['proc'] = None
                self._discard_unlisted(rt, path, run)
                self.changed.notify_all()
        return outcome

    def _finalize(self, job, rt):
        """Assemble le MP4 telechargeable et les sous-titres complets."""
        path = os.path.join(self.root, job['id'])
        with open(os.path.join(path, 'mux.log'), 'wb') as log:
            proc = subprocess.Popen(['ffmpeg', '-y', '-nostdin', '-v', 'error', '-f', 'mpegts', '-i', 'pipe:0',
                                     '-map', '0', '-c', 'copy', '-movflags', '+faststart', 'q0.mp4'],
                                    cwd=path, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
        try:
            # Les segments portent deja leur horodatage absolu : il suffit de
            # les mettre bout a bout, sans reencodage.
            for n in range(job['segments']):
                with open(os.path.join(path, rt['index'][0][n]), 'rb') as fh:
                    shutil.copyfileobj(fh, proc.stdin)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if proc.wait():
            raise RuntimeError('Assemblage du fichier téléchargeable impossible.')

    def _subtitles(self, job, source, rt):
        """Extrait la piste de sous-titres texte, dans un FFmpeg a part.

        Dans la passe video, FFmpeg n'ecrit rien avant le premier sous-titre
        qui suit le point de depart : apres un saut, l'image attendait jusqu'a
        la replique suivante (44 s mesurees pour 2 min 30 sans dialogue). Seule
        la piste texte est gardee, mais tout le fichier est lu.
        """
        path = os.path.join(self.root, job['id'])
        final = os.path.join(path, 'subtitles.vtt')
        if os.path.exists(final):
            job['subtitles_ready'] = True
            return
        cmd = ['ffmpeg', '-y', '-nostdin', '-v', 'error', '-rw_timeout', '15000000',
               '-user_agent', self.cfg.get('user_agent', 'VLC/3.0.20'), '-i', source,
               '-map', '0:%d' % job['subtitle'], '-c:s', 'webvtt', '-f', 'webvtt', 'subtitles.part']
        with open(os.path.join(path, 'subtitles.log'), 'wb') as log:
            with self.lock:
                rt['subs_proc'] = proc = subprocess.Popen(cmd, cwd=path, stdout=subprocess.DEVNULL, stderr=log)
        try:
            proc.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        with self.lock:
            rt['subs_proc'] = None
            if proc.returncode == 0 and os.path.exists(os.path.join(path, 'subtitles.part')):
                os.replace(os.path.join(path, 'subtitles.part'), final)
                job['subtitles_ready'] = True
            elif not self._closed.is_set() and not rt.get('paused'):
                # La video reste lisible : on le signale sans faire echouer la preparation.
                job['subtitles_error'] = 'Sous-titres indisponibles pour cette source.'
            self._save(job)
            self.changed.notify_all()

    def _subtitles_alongside(self, job, source, rt):
        try:
            self._subtitles(job, source, rt)
        except OSError:
            pass
        finally:
            self.transcoder.unreserve(job['id'] + ':st')

    def _run(self, job, source):
        path = os.path.join(self.root, job['id'])
        started = time.time()
        try:
            media = probe(source, self.cfg.get('user_agent', 'VLC/3.0.20'))
            if media.get('unverified'):
                raise RuntimeError('Impossible de vérifier le format du film.')
            audio = [s['index'] for s in media['streams'] if s['codec_type'] == 'audio']
            sub = {s['index']: s.get('codec_name') for s in media['streams'] if s['codec_type'] == 'subtitle'}
            if job['audio'] is not None and job['audio'] not in audio:
                raise RuntimeError('Piste audio invalide.')
            if job['subtitle'] is not None and sub.get(job['subtitle']) not in TEXT_SUBTITLES:
                raise RuntimeError('Ces sous-titres ne sont pas disponibles au format texte.')
            duration = float(media.get('duration') or 0)
            rungs = []
            for r in self.cfg['ladder']:
                if r['height'] > job['height']:
                    continue
                h = min(r['height'], media['height']) // 2 * 2
                if any(x['height'] == h for x in rungs):
                    continue  # source plus petite que le barreau : deja couvert
                rungs.append(dict(height=h, width=round(h * media['width'] / media['height'] / 2) * 2,
                                  bitrate=r['bitrate'], maxrate=r['maxrate'], bufsize=r['bufsize']))
            with self.lock:
                if job.get('segments') and job.get('rungs') and len(job['rungs']) != len(rungs):
                    raise RuntimeError('Configuration modifiée : relancez la préparation.')
                # Sans duree (flux TS surtout), la playlist s'allonge au fil de
                # l'encodage et le saut se limite a la partie deja encodee.
                opened = job.get('open', duration <= 0)
                job.update(duration=duration, rungs=rungs, qualities=[r['height'] for r in rungs], open=opened,
                           segments=job.get('segments') or (0 if opened else self._segments(duration)),
                           subtitles=job['subtitle'] is not None)
                self._save(job)
            rt = self.runtime[job['id']]
            if job['subtitle'] is not None and not job.get('subtitles_ready'):
                try:
                    # Une seconde connexion a l'abonnement : les sous-titres
                    # arrivent pendant que le film se regarde deja.
                    self.transcoder.reserve(job['id'] + ':st', job['provider'])
                    rt['subs_thread'] = threading.Thread(target=self._subtitles_alongside, args=(job, source, rt), daemon=True)
                    rt['subs_thread'].start()
                except CapacityError:
                    # Une seule connexion : les sous-titres d'abord, puis la video.
                    with self.lock:
                        job['stage'] = 'subtitles'
                        self._save(job)
                    self._subtitles(job, source, rt)
                    if self._closed.is_set():
                        raise RuntimeError('Préparation interrompue par l’arrêt du serveur.')
            with self.lock:
                # Le lecteur peut ouvrir le film : le premier segment demande
                # attendra simplement son encodage.
                job.pop('stage', None)
                job['playable'] = True
                self._save(job)
                self.changed.notify_all()
            failures = 0
            while True:
                if self._closed.is_set():
                    raise RuntimeError('Préparation interrompue par l’arrêt du serveur.')
                if rt.get('deleted'):
                    raise RuntimeError('Préparation supprimée.')
                if rt.get('paused'):
                    raise _Paused()
                outcome = self._encode(job, source, media, rt, started)
                if outcome == 'done':
                    break
                if outcome == 'failed':
                    failures += 1
                    if failures >= 3:
                        raise RuntimeError('Source indisponible ou format non pris en charge.')
                    time.sleep(2)
                else:
                    failures = 0
                with self.lock:
                    self._save(job)
            self._finalize(job, rt)
            if rt.get('subs_thread'):
                rt['subs_thread'].join()
            with self.lock:
                job.update(state='ready', progress=100, error=None,
                           size_bytes=os.path.getsize(os.path.join(path, 'q0.mp4')), download='q0.mp4')
        except Exception as exc:
            if self._closed.is_set() or self.jobs.get(job['id']) is not job:
                # Arret du serveur : la preparation reprendra au prochain
                # demarrage. Suppression : delete() a deja tout efface.
                return
            if isinstance(exc, _Paused):
                with self.lock:
                    subs = (self.runtime.get(job['id']) or {}).get('subs_proc')
                    if subs and subs.poll() is None:
                        subs.kill()
                    # Les segments faits restent : la reprise repart de la.
                    self.runtime[job['id']]['paused'] = False
                    job.update(state='paused', error='En pause pour laisser la connexion à une autre lecture. Reprise automatique ensuite.')
                return
            with self.lock:
                subs = (self.runtime.get(job['id']) or {}).get('subs_proc')
                if subs and subs.poll() is None:
                    subs.kill()
                job.update(state='failed', playable=False,
                           error=str(exc) if isinstance(exc, RuntimeError) else 'Échec de la préparation.')
                self.runtime.pop(job['id'], None)
                self._cleanup(path)
        finally:
            with self.lock:
                try:
                    self._save(job)
                except OSError:
                    pass
                self.changed.notify_all()
            self.transcoder.unreserve(job['id'])
            # Connexion rendue : une preparation en pause peut repartir. Pas
            # celle-ci si elle vient de ceder sa place.
            if job['state'] != 'paused' and not self._closed.is_set():
                self._resume_paused()

    # ----------------------------------------------------------- playlists

    def master(self, job):
        lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
        for i, r in enumerate(job['rungs']):
            lines += ['#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d' % (
                int((_bps(r['maxrate']) + 96000) * 1.08), r['width'], r['height']), 'q%d.m3u8' % i]
        return '\n'.join(lines) + '\n'

    def variant(self, job, rung):
        """Playlist VOD complete des le depart : le lecteur connait la duree et peut se deplacer."""
        lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:%d' % (SEGMENT + 1),
                 '#EXT-X-MEDIA-SEQUENCE:0']
        if job.get('open'):
            with self.lock:
                rt = self.runtime.get(job['id'])
                count = self._missing(rt, job) if rt else 0
            lines.append('#EXT-X-PLAYLIST-TYPE:EVENT')
            for n in range(count):
                lines += ['#EXTINF:%.3f,' % SEGMENT, 'q%d_%06d.ts' % (rung, n)]
            return '\n'.join(lines) + '\n'
        total = job['segments']
        last = job['duration'] - SEGMENT * (total - 1)
        lines += ['#EXT-X-PLAYLIST-TYPE:VOD', '#EXT-X-INDEPENDENT-SEGMENTS']
        for n in range(total):
            seconds = SEGMENT if n < total - 1 else max(.1, min(SEGMENT + 1, last))
            lines += ['#EXTINF:%.3f,' % seconds, 'q%d_%06d.ts' % (rung, n)]
        lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines) + '\n'

    def retry(self, jid, account=None):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                return None
            if job['state'] == 'preparing':
                return dict(job)
            try:
                source = self._job_source(job)
            except Exception as exc:
                job.update(state='failed', error=str(exc))
                self._save(job)
                return dict(job)
            if job['state'] == 'paused':
                job.update(state='preparing', error=None, account=account if account is not None else job.get('account'))
                self._save(job)
                resume = True
            else:
                resume = False
        if resume:
            self._start(job, source, preempt=True)
            return dict(job)
        with self.lock:
            job.update(state='preparing', progress=0, error='Reprise demandée par l’utilisateur…',
                       created=time.time(), format=2, playable=False)
            for key in ('segments', 'rungs', 'qualities', 'download', 'size_bytes', 'open',
                        'subtitles_ready', 'subtitles_error', 'stage'):
                job.pop(key, None)
            self._save(job)
        self._start(job, source, clear_files=True, preempt=True)
        return dict(job)

    def delete(self, jid):
        """Supprime une preparation, terminee ou en cours, et ses fichiers."""
        with self.lock:
            job = self.jobs.pop(jid, None)
            if not job:
                return False
            rt = self.runtime.pop(jid, None)
            if rt:
                # Le fil d'encodage voit la marque, s'arrete et libere sa
                # connexion ; les lecteurs en attente d'un segment abandonnent.
                rt['deleted'] = True
                for proc in (rt.get('proc'), rt.get('subs_proc')):
                    if proc and proc.poll() is None:
                        proc.kill()
            self.changed.notify_all()
        shutil.rmtree(os.path.join(self.root, jid), ignore_errors=True)
        return True

    def close(self):
        """Arrete les encodages en cours sans marquer les preparations en echec."""
        self._closed.set()
        with self.lock:
            procs = [p for rt in self.runtime.values() for p in (rt.get('proc'), rt.get('subs_proc')) if p]
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    def list(self):
        with self.lock:
            return [{k: v for k, v in j.items() if k != 'rungs'}
                    for j in sorted(self.jobs.values(), key=lambda j: j['created'], reverse=True)]
