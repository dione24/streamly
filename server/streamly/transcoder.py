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

def _int0(value):
    """Entier tolerant : ffprobe omet ou renseigne 'N/A' selon les sources."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# H264 8 bits 4:2:0 est le seul profil que tous les navigateurs decodent. Une
# source 10 bits ou 4:2:2 doit etre reencodee, meme si le codec « est du H264 ».
PASSTHROUGH_PIX_FMTS = ('yuv420p', 'yuvj420p')


def _estimated_bitrate(media):
    """Debit approximatif quand la source ne l'annonce pas.

    Ne sert qu'a renseigner BANDWIDTH dans le manifeste : une source remuxee
    n'expose qu'une variante, aucune decision ABR n'en depend. Ce chiffre
    n'est jamais utilise pour valider un plafond — voir passthrough_plan.
    """
    w = max(1, _int0(media.get('width')) or 1280)
    h = max(1, _int0(media.get('height')) or 720)
    fps = max(1.0, float(media.get('fps') or 25))
    return int(w * h * fps * 0.07)


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
        audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), {})
        fps = float(Fraction(video.get('avg_frame_rate') or video.get('r_frame_rate') or '25'))
        return {'height': int(video['height']), 'width': int(video['width']),
                'fps': max(1, min(60, fps or 25)), 'codec': video.get('codec_name'),
                'pix_fmt': video.get('pix_fmt'), 'video_bitrate': _int0(video.get('bit_rate')),
                'bitrate': _int0(data.get('format', {}).get('bit_rate')),
                'audio_codec': audio.get('codec_name'),
                'audio_bitrate_src': _int0(audio.get('bit_rate')),
                'streams': data['streams'], 'duration': data.get('format', {}).get('duration')}
    except (OSError, ValueError, KeyError, StopIteration, ZeroDivisionError, subprocess.SubprocessError):
        return {'height': 720, 'width': 1280, 'fps': 25, 'unverified': True, 'streams': []}


class CapacityError(RuntimeError):
    pass


def _redact_credentials(text, cfg=None):
    """Masque les identifiants provider dans les messages d'erreur.

    Les URLs Xtream portent username/password en chemin
    (/user/pass/id, /live/user/pass/id.m3u8) et FFmpeg les recopie
    dans ses logs. On ne peut pas empêcher FFmpeg d'écrire l'URL
    source, mais on ne doit jamais la renvoyer à l'UI ni dans /api/status.
    """
    if not text:
        return text
    redacted = text
    secrets = []
    try:
        for p in (cfg or {}).get('providers', []):
            for k in ('username', 'password'):
                v = p.get(k)
                if v and len(str(v)) >= 3:
                    secrets.append(str(v))
    except (AttributeError, TypeError):
        pass
    for s in secrets:
        if s in redacted:
            redacted = redacted.replace(s, '***')
    # Jeton de redirection provider (?token=...) et chemins restants.
    redacted = re.sub(r'(\?token=)[^&\s"\']+', r'\1***', redacted)
    redacted = re.sub(r'(https?://[^\s/]+/)\S+/\S+/(\d+)', r'\1***/***/\2', redacted)
    return redacted


class Transcoder:
    def probe_last_error(self, key, max_lines=14):
        """Retourne une erreur ffmpeg récente, lisible par l'UI (sans credentials)."""
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
                return _redact_credentials(line[:240], self.cfg)
        return _redact_credentials(lines[-1][:240], self.cfg)

    def __init__(self, cfg, hls_dir, log_dir, monitor=True, state_dir=None):
        self.cfg, self.hls_dir, self.log_dir = cfg, hls_dir, log_dir
        self.ladder = cfg['ladder']
        # Debits mesures, par source. Sans eux, une chaine dont le debit
        # depasse le plafond du mode serait relancee a chaque lecture : remux,
        # mesure, declassement. Avec eux, la decision est prise avant meme de
        # lancer ffmpeg des la deuxieme lecture, et le declassement ne coute
        # qu'un changement de generation, une fois par chaine.
        self._bitrate_path = os.path.join(state_dir, 'source_bitrates.json') if state_dir else None
        self._bitrates = {}
        if self._bitrate_path:
            try:
                with open(self._bitrate_path, encoding='utf-8') as fh:
                    self._bitrates = {k: v for k, v in json.load(fh).items() if isinstance(v, dict)}
            except (OSError, ValueError, AttributeError):
                self._bitrates = {}
        self._ffmpeg_caps = _ffmpeg_capabilities()
        self._lock = threading.RLock()
        self.workers, self.tickets, self.reservations = {}, {}, {}
        self.failures = {}
        self._closed = threading.Event()
        os.makedirs(hls_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)
        # Les logs FFmpeg recopient l'URL source avec les identifiants :
        # accès restreint au propriétaire (migration VPS dans une semaine,
        # le masquage complet du `ps` demande un proxy local, hors scope).
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        for name in os.listdir(log_dir):
            if name.startswith('ffmpeg_') and name.endswith('.log'):
                try:
                    os.chmod(os.path.join(log_dir, name), 0o600)
                except OSError:
                    pass
        # Les segments sont purement transitoires : aucun ne doit survivre a un
        # redemarrage. Le menage de fin de flux ne s'execute pas quand le
        # service est arrete ou qu'un processus est tue, si bien que les
        # segments s'accumulaient a chaque redemarrage.
        for name in os.listdir(hls_dir):
            shutil.rmtree(os.path.join(hls_dir, name), ignore_errors=True)
        if monitor:
            threading.Thread(target=self._watchdog, daemon=True).start()

    def _rung_fps(self, rung, source_fps):
        """Cadence a encoder pour un barreau.

        Une source de sport a 50 i/s double le cout d'encodage. Sur les
        barreaux basse definition, cette fluidite est invisible alors qu'elle
        se paie plein pot : on la ramene a la moitie. Les barreaux hauts, eux,
        gardent la cadence source — c'est la que le mouvement rapide compte.
        """
        seuil = int(self.cfg.get('high_fps_min_height', 480))
        if source_fps > 30 and int(rung['height']) < seuil:
            return source_fps / 2.0
        return source_fps

    @staticmethod
    def _source_id(url):
        """Cle de cache. L'URL porte les identifiants : on n'ecrit que son empreinte."""
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def _known_bitrate(self, url):
        """Debit mesure lors d'une lecture precedente, si toujours d'actualite."""
        entry = self._bitrates.get(self._source_id(url))
        if not entry:
            return 0
        ttl = float(self.cfg.get('bitrate_cache_hours', 24)) * 3600
        if ttl and time.time() - float(entry.get('at', 0)) > ttl:
            return 0
        return _int0(entry.get('bps'))

    def _remember_bitrate(self, url, bps):
        self._bitrates[self._source_id(url)] = {'bps': int(bps), 'at': time.time()}
        if not self._bitrate_path:
            return
        try:
            tmp = self._bitrate_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(self._bitrates, fh)
            os.replace(tmp, self._bitrate_path)
        except OSError:
            pass

    def passthrough_plan(self, media, ceiling=0):
        """Decide si la source peut etre remultiplexee telle quelle.

        Reencoder une source deja conforme coute les quatre barreaux x264 pour
        un resultat visuellement identique a la source. Quand le probe confirme
        du H264 8 bits 4:2:0 et que le debit tient sous le plafond du mode, on
        se contente de remultiplexer : le CPU tombe d'environ 75 % a 5 % et le
        spectateur recoit la definition source au lieu du barreau le plus haut.

        Renvoie None des qu'un critere de compatibilite manque. Le plafond du
        mode, lui, n'est verifie ici que si la source annonce son debit ; sinon
        la verification est reportee a la mesure des premiers segments.
        """
        if not self.cfg.get('passthrough', True):
            return None
        if media.get('unverified'):
            return None
        if (media.get('codec') or '').lower() != 'h264':
            return None
        if (media.get('pix_fmt') or '').lower() not in PASSTHROUGH_PIX_FMTS:
            return None
        audio_codec = (media.get('audio_codec') or '').lower()
        has_audio = bool(audio_codec)
        # MP2/AC3/MP3 ne sont pas lisibles en HLS par les navigateurs : on
        # reencode la seule piste audio (~2 % de CPU) et on garde la video.
        copy_audio = audio_codec == 'aac'

        video_bps = _int0(media.get('video_bitrate'))
        total_bps = _int0(media.get('bitrate'))
        if video_bps:
            if not has_audio:
                effective = video_bps
            elif copy_audio and _int0(media.get('audio_bitrate_src')):
                effective = video_bps + _int0(media.get('audio_bitrate_src'))
            else:
                effective = video_bps + _bps(self.cfg.get('audio_bitrate', '96k'))
        else:
            # Le debit conteneur inclut deja l'audio source : l'ajouter une
            # seconde fois compterait double.
            effective = total_bps

        if ceiling and effective and effective * 1.08 > ceiling:
            return None
        # Debit inconnu : c'est le cas courant. Un flux TS servi en HTTP
        # n'annonce ni format.bit_rate ni stream.bit_rate video — seul l'audio
        # en a un. Exiger un chiffre ici reviendrait a reserver le remux au
        # mode Sport. On part donc en remux, et _tick mesure le debit reel sur
        # les premiers segments : au-dessus du plafond, il rebascule en
        # encodage via une nouvelle generation, comme pour un failover.
        return {'audio': has_audio, 'copy_audio': copy_audio,
                'bitrate': effective or _estimated_bitrate(media),
                'measured': bool(effective)}

    def _command(self, source, media, generation, audio_only=False, passthrough=None):
        seg = int(self.cfg.get('segment_seconds', 2))
        fps = float(media['fps'])
        ua = self.cfg.get('user_agent', 'VLC/3.0.20')
        # Mode audio-only : pas de transcodage vidéo.
        if audio_only:
            if not (any(s.get('codec_type') == 'audio' for s in media.get('streams', [])) or media.get('unverified')):
                raise RuntimeError('Aucun flux audio détectable pour ce canal.')
            cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning',
                   '-rw_timeout', '12000000', '-user_agent', ua,
                   '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '3',
                   '-analyzeduration', '2000000', '-probesize', '2000000', '-i', source,
                   '-map', '0:a:0', '-vn',
                   '-c:a', 'aac', '-b:a', self.cfg.get('audio_bitrate', '96k'), '-ac', '2',
                   '-f', 'hls', '-hls_time', str(seg), '-hls_list_size', str(max(36, int(self.cfg.get('playlist_size', 36))))]
            if self._ffmpeg_caps.get('hls_flags'):
                hls_flags = ['delete_segments', 'omit_endlist']
                if self._ffmpeg_caps.get('hls_independent_segments'):
                    hls_flags.append('independent_segments')
                if self._ffmpeg_caps.get('hls_temp_file'):
                    hls_flags.append('temp_file')
                cmd += ['-hls_flags', '+'.join(hls_flags)]
            if self._ffmpeg_caps.get('hls_delete_threshold'):
                cmd += ['-hls_delete_threshold', '12']
            cmd += ['-hls_segment_type', 'mpegts',
                    '-hls_segment_filename', 'g%d_a_%%09d.ts' % generation,
                    'g%d_a.m3u8' % generation]
            return cmd

        # Remux : la source part telle quelle, sans filtre ni encodeur. Les
        # noms de sortie restent ceux de l'echelle (une variante, niveau 0),
        # pour que le routage des segments et des tickets ne change pas.
        if passthrough:
            cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning',
                   '-rw_timeout', '12000000', '-user_agent', ua,
                   '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '3',
                   '-analyzeduration', '2000000', '-probesize', '2000000', '-i', source,
                   '-map', '0:v:0', '-c:v', 'copy']
            if passthrough['audio']:
                cmd += ['-map', '0:a:0']
                cmd += ['-c:a', 'copy'] if passthrough['copy_audio'] else [
                    '-c:a', 'aac', '-b:a', self.cfg.get('audio_bitrate', '96k'), '-ac', '2']
            # Pas de -force_key_frames possible sans encodeur : le muxeur ne
            # peut couper que sur les images cles de la source. hls_time
            # devient un minimum, la duree reelle des segments suit le GOP.
            cmd += ['-sn', '-dn',
                    '-f', 'hls', '-hls_time', str(seg),
                    '-hls_list_size', str(max(36, int(self.cfg.get('playlist_size', 36))))]
            if self._ffmpeg_caps.get('hls_flags'):
                hls_flags = ['delete_segments', 'omit_endlist']
                if self._ffmpeg_caps.get('hls_independent_segments'):
                    hls_flags.append('independent_segments')
                if self._ffmpeg_caps.get('hls_temp_file'):
                    hls_flags.append('temp_file')
                cmd += ['-hls_flags', '+'.join(hls_flags)]
            cmd += ['-hls_segment_type', 'mpegts',
                    '-var_stream_map', 'v:0,a:0' if passthrough['audio'] else 'v:0']
            if self._ffmpeg_caps.get('hls_delete_threshold'):
                cmd += ['-hls_delete_threshold', '12']
            cmd += ['-hls_segment_filename', 'g%d_%%v_%%09d.ts' % generation,
                    'g%d_s_%%v.m3u8' % generation]
            return cmd

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
        # Brider chaque encodeur a un seul thread laissait la machine aux trois
        # quarts inutilisee : suffisant pour une source a 25 i/s, insuffisant a
        # 50 i/s, ou la production tombait a 65 % du temps reel et le lecteur
        # se vidait. 0 laisse ffmpeg decider ; la consommation totale reste
        # bornee par CPUQuota dans l'unite systemd.
        threads = max(0, int(self.cfg.get('encoder_threads', 0)))
        if threads and self._ffmpeg_caps.get('filter_complex_threads'):
            cmd += ['-filter_complex_threads', str(threads)]
        cmd += ['-filter_complex', fc]
        force_key_frame_expression = 'expr:gte(t,n_forced*%d)' % seg
        for i, r in enumerate(self.ladder):
            cmd += ['-map', '[o%d]' % i, '-c:v:%d' % i, 'libx264',
                    '-preset:v:%d' % i, self.cfg.get('x264_preset', 'veryfast'),
                    '-pix_fmt:v:%d' % i, 'yuv420p', '-profile:v:%d' % i, 'high',
                    '-b:v:%d' % i, r['bitrate'], '-maxrate:v:%d' % i, r['maxrate'],
                    '-bufsize:v:%d' % i, r['bufsize'],
                    '-r:v:%d' % i, str(self._rung_fps(r, fps)),
                    '-g:v:%d' % i, str(round(seg * self._rung_fps(r, fps))),
                    '-keyint_min:v:%d' % i, str(round(seg * self._rung_fps(r, fps))),
                    '-sc_threshold:v:%d' % i, '0']
            if threads:
                cmd += ['-threads:v:%d' % i, str(threads)]
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

    def open(self, owner, identity, sources, label='', ceiling=0, budget=0, audio_only=False):
        key = hashlib.sha256(identity.encode()).hexdigest()[:20]
        with self._lock:
            self._expire_locked()
            # Un meme appareil ne regarde qu'une chaine a la fois. Son ticket
            # precedent retenait la connexion de l'abonnement : changer de
            # chaine se heurtait alors a sa propre lecture, avec le message
            # « toutes les connexions sont occupees ». Un onglet laisse ouvert
            # ou une page rechargee suffisait a bloquer jusqu'a l'expiration.
            for stale in [k for k, t in self.tickets.items()
                          if t['owner'] == owner and t['key'] != key]:
                self._release_locked(stale, owner)
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
                         last=time.time(), label=label, failovers=0, media=None,
                         audio_only=bool(audio_only), error=None,
                         ceiling=max(0, int(ceiling)), passthrough=None,
                         passthrough_denied=False, measured_once=False)
                self.workers[key] = w
                threading.Thread(target=self._spawn, args=(key,), daemon=True).start()
            elif w['state'] == 'failed':
                raise CapacityError('Les sources de cette chaîne sont indisponibles. Arrêtez la lecture puis réessayez.')
            elif (w.get('passthrough') and w['passthrough']['measured'] and ceiling
                  and w['passthrough']['bitrate'] * 1.08 > int(ceiling)):
                # Un flux remuxe n'a qu'une qualite, celle de la source. Un
                # spectateur au plafond plus bas n'a rien a lire dessus : mieux
                # vaut le dire que lui servir un flux qui depassera son budget.
                raise CapacityError('Cette chaîne est diffusée en qualité source, au-dessus du plafond du mode choisi. Passez en Sport ou attendez la fin de l\'autre lecture.')
            ticket = secrets.token_urlsafe(24)
            self.tickets[ticket] = dict(owner=owner, key=key, last=time.time(), created=time.time(),
                                        bytes=0, ceiling=max(0, int(ceiling)), budget=max(0, int(budget)),
                                        audio_only=bool(audio_only))
            return ticket

    def _spawn(self, key):
        with self._lock:
            w = self.workers.get(key)
            if not w:
                return
            generation = w['generation']
            source = w['sources'][w['index']]['url']
            audio_only = w.get('audio_only')
        media = probe(source, self.cfg.get('user_agent', 'VLC/3.0.20'))
        with self._lock:
            if self.workers.get(key) is not w or w['generation'] != generation:
                return
            outdir = os.path.join(self.hls_dir, key)
            os.makedirs(outdir, exist_ok=True)
            w['media'], w['started'], w['error'] = media, time.time(), None
            # Chaque generation reprobe : une source de secours peut etre
            # remuxable la ou la precedente ne l'etait pas, et inversement.
            known = self._known_bitrate(source)
            if known:
                # Mesure de terrain : elle prime sur ce que la source annonce.
                media = dict(media, bitrate=known, video_bitrate=0)
            w['passthrough'] = None if (audio_only or w.get('passthrough_denied')) else \
                self.passthrough_plan(media, w.get('ceiling', 0))
            w['measured_once'] = False
            try:
                # FFmpeg recopie l'URL source (avec identifiants) dans ses logs :
                # fichier en 600, et jamais renvoyé tel quel à l'UI.
                log_path = os.path.join(self.log_dir, 'ffmpeg_%s.log' % key)
                with open(log_path, 'wb') as log:
                    try:
                        os.chmod(log_path, 0o600)
                    except OSError:
                        pass
                    w['proc'] = subprocess.Popen(self._command(source, media, generation, audio_only=audio_only,
                                                                  passthrough=w['passthrough']),
                                                 cwd=outdir, stdout=log, stderr=log)
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
        if t.get('audio_only'):
            return [0]
        ceiling = t.get('ceiling', 0)
        plan = (self.workers.get(t.get('key')) or {}).get('passthrough')
        if plan:
            # Tant que le debit n'est pas mesure, on laisse lire : c'est _tick
            # qui declasse le worker s'il depasse, pas un 403 a l'aveugle.
            return [0] if not (ceiling and plan['measured']) or plan['bitrate'] * 1.08 <= ceiling else []
        audio = _bps(self.cfg.get('audio_bitrate', '96k'))
        levels = [i for i, r in enumerate(self.ladder) if not ceiling or (_bps(r['maxrate']) + audio) * 1.08 <= ceiling]
        return levels

    def master_playlist(self, ticket):
        with self._lock:
            t = self.tickets[ticket]
            w = self.workers[t['key']]
            if t.get('audio_only'):
                bw = int(_bps(self.cfg.get('audio_bitrate', '96k')) * 1.08)
                return '\n'.join([
                    '#EXTM3U',
                    '#EXT-X-VERSION:3',
                    '#EXT-X-INDEPENDENT-SEGMENTS',
                    '#EXT-X-STREAM-INF:BANDWIDTH=%d,CODECS="mp4a.40.2"' % bw,
                    'g%d_a.m3u8' % w['generation'],
                ]) + '\n'

            media = w['media'] or {'width': 1280, 'height': 720, 'fps': 25}
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            if w.get('passthrough'):
                # Definition source, sans mise a l'echelle : c'est tout
                # l'interet du remux. Une seule variante, donc pas d'ABR.
                bw = int(w['passthrough']['bitrate'] * 1.08)
                lines += ['#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,FRAME-RATE=%.3f' % (
                              bw, int(media['width']), int(media['height']), float(media['fps'])),
                          'g%d_s_0.m3u8' % w['generation']]
                return '\n'.join(lines) + '\n'
            for i in self.allowed_levels(t):
                r = self.ladder[i]
                h = min(int(r['height']), media['height']) // 2 * 2
                width = round(h * media['width'] / media['height'] / 2) * 2
                # Include transport overhead; do not invent codec level strings.
                bw = int((_bps(r['maxrate']) + _bps(self.cfg.get('audio_bitrate', '96k'))) * 1.08)
                lines += ['#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,FRAME-RATE=%.3f' % (bw, width, h, self._rung_fps(r, media['fps'])),
                          'g%d_s_%d.m3u8' % (w['generation'], i)]
            return '\n'.join(lines) + '\n'

    def status(self, ticket=None):
        with self._lock:
            def public(w):
                return {k: w[k] for k in ('key', 'label', 'generation', 'state', 'failovers', 'provider')} | {
                    'uptime_s': round(time.time() - w['started']), 'media': {
                        k: v for k, v in (w['media'] or {}).items() if k != 'streams'},
                    'viewers': sum(t['key'] == w['key'] for t in self.tickets.values()),
                    'passthrough': bool(w.get('passthrough')),
                    'error': w.get('error')}
            if ticket:
                t = self.tickets.get(ticket)
                if not t or t['key'] not in self.workers:
                    return None
                return dict(public(self.workers[t['key']]),
                            bytes=t['bytes'], budget=t['budget'], ceiling=t['ceiling'],
                            audio_only=t.get('audio_only'))
            return {'running': bool(self.workers), 'workers': [public(w) for w in self.workers.values()],
                    'capacity': int(self.cfg.get('max_concurrent_streams', 1))}

    def release(self, ticket, owner=None):
        with self._lock:
            self._release_locked(ticket, owner)

    def _release_locked(self, ticket, owner=None):
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

    def _ticket_ceiling_locked(self, key):
        """Plafond le plus bas parmi les spectateurs d'un worker (0 = aucun)."""
        ceilings = [t['ceiling'] for t in self.tickets.values() if t['key'] == key]
        if not ceilings or any(c == 0 for c in ceilings):
            return 0
        return min(ceilings)

    def _measured_bitrate_locked(self, key, generation):
        """Debit reel des segments deja ecrits, en bits par seconde.

        La source n'annonce rien : ses segments, si. On additionne les
        segments complets de la generation courante et les durees que le
        muxeur a inscrites en face. Le dernier segment de la playlist est
        ignore, il peut etre en cours d'ecriture.
        """
        outdir = os.path.join(self.hls_dir, key)
        try:
            with open(os.path.join(outdir, 'g%d_s_0.m3u8' % generation), encoding='utf-8') as fh:
                playlist = fh.read()
        except OSError:
            return 0
        entries = re.findall(r'#EXTINF:([\d.]+)[^\n]*\n([^\n#]+)', playlist)
        if len(entries) < 4:
            return 0
        total_bytes = seconds = 0
        for duration, name in entries[:-1]:
            try:
                total_bytes += os.path.getsize(os.path.join(outdir, name.strip()))
            except OSError:
                continue
            seconds += float(duration)
        return int(total_bytes * 8 / seconds) if seconds > 0 else 0

    def _restart_locked(self, key, w, reason):
        """Relance la meme source dans une nouvelle generation.

        Reutilise le mecanisme du failover : les segments deja servis restent
        lisibles, et le lecteur recharge le manifeste en detectant le
        changement de generation.
        """
        self._kill(w['proc'])
        w['generation'] += 1
        w['state'] = 'starting'
        w['error'] = reason
        outdir = os.path.join(self.hls_dir, key)
        for f in os.listdir(outdir):
            m = re.match(r'g(\d+)_', f)
            if m and int(m[1]) < w['generation'] - 1:
                try: os.unlink(os.path.join(outdir, f))
                except FileNotFoundError: pass
        threading.Thread(target=self._spawn, args=(key,), daemon=True).start()

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
                if w.get('passthrough') and not w.get('measured_once'):
                    measured = self._measured_bitrate_locked(key, w['generation'])
                    if measured:
                        w['measured_once'] = True
                        # Memorise meme quand le plafond est tenu : c'est ce qui
                        # evite de refaire le tour a la prochaine lecture.
                        self._remember_bitrate(w['sources'][w['index']]['url'], measured)
                        w['passthrough'] = dict(w['passthrough'], bitrate=measured, measured=True)
                        ceiling = self._ticket_ceiling_locked(key)
                        if ceiling and measured * 1.08 > ceiling:
                            # Le remux depasse le plafond du mode : on repasse
                            # a l'echelle encodee plutot que de laisser filer
                            # le budget du spectateur.
                            w['passthrough'], w['passthrough_denied'] = None, True
                            self._restart_locked(key, w, None)
                            continue
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
