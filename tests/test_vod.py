"""Lecture pendant la preparation, avec un vrai FFmpeg et une source HTTP.

La source est servie avec prise en charge des requetes Range, comme un panel
Xtream : c'est ce qui permet a FFmpeg de repartir du milieu du film.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from streamly.transcoder import Transcoder, CapacityError
from streamly.vod import Movies, SEGMENT

HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
LADDER = [
    {'name': '480p', 'height': 480, 'bitrate': '800k', 'maxrate': '900k', 'bufsize': '1600k'},
    {'name': '360p', 'height': 360, 'bitrate': '450k', 'maxrate': '500k', 'bufsize': '900k'},
    {'name': '240p', 'height': 240, 'bitrate': '250k', 'maxrate': '300k', 'bufsize': '500k'},
]
DURATION = 60


class RangeHandler(BaseHTTPRequestHandler):
    root = None
    requests = []
    delay = 0  # secondes par bloc de 64 Ko : simule un panel au debit limite

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = os.path.join(self.root, os.path.basename(self.path))
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range') or '')
        if m:
            start = int(m[1]); end = int(m[2]) if m[2] else size - 1
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        else:
            self.send_response(200)
        type(self).requests.append(start)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        with open(path, 'rb') as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
                    time.sleep(type(self).delay)
            except (BrokenPipeError, ConnectionResetError):
                pass


def first_pts(path):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries',
                                   'packet=pts_time', '-of', 'csv=p=0', str(path)], text=True)
    return min(float(x.strip(',')) for x in out.split() if x.strip(','))


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg requis')
class ProgressiveMovieTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        (root / 'subs.srt').write_text(
            '1\n00:00:02,000 --> 00:00:04,000\nDebut du film\n\n'
            '2\n00:00:42,000 --> 00:00:44,000\nMilieu du film\n\n'
            '3\n00:00:56,000 --> 00:00:58,000\nFin du film\n\n')
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-i', str(root / 'subs.srt'),
                        '-t', str(DURATION), '-map', '0:v', '-map', '1:a', '-map', '2:s',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '50', '-c:a', 'ac3', '-c:s', 'srt',
                        str(root / 'film.mkv')], check=True, timeout=120)
        RangeHandler.root = str(root)
        # Environ trois fois le temps reel : le saut arrive avant l'encodage.
        size = (root / 'film.mkv').stat().st_size
        RangeHandler.delay = DURATION / 3 / (size / 65536)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), RangeHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.source = 'http://127.0.0.1:%d/film.mkv' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = {'ladder': LADDER, 'user_agent': 'StreamlyTest', 'max_concurrent_streams': 2,
                    'providers': [{'id': 'p', 'max_connections': 2}]}
        self.transcoder = Transcoder(self.cfg, self.dir.name + '/hls', self.dir.name + '/logs', monitor=False)
        self.movies = Movies(self.cfg, self.dir.name + '/movies', None, self.transcoder)

    def tearDown(self):
        self.movies.close()
        time.sleep(.6)
        self.dir.cleanup()

    def wait(self, jid, predicate, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.movies.jobs[jid]
            if predicate(job):
                return job
            self.assertNotEqual(job['state'], 'failed', job.get('error'))
            time.sleep(.2)
        self.fail('delai depasse : %r' % self.movies.jobs[jid])

    def test_play_seek_then_download(self):
        job = self.movies.start({'provider_id': 'p', 'stream_id': 1, 'title': 'Film'}, self.source, 480, subtitle=2)
        jid = job['id']
        job = self.wait(jid, lambda j: j.get('playable'))
        total = DURATION // SEGMENT
        self.assertEqual(job['segments'], total)
        self.assertEqual(job['qualities'], [360, 240])
        variant = self.movies.variant(job, 0)
        self.assertIn('#EXT-X-PLAYLIST-TYPE:VOD', variant)
        self.assertTrue(variant.rstrip().endswith('#EXT-X-ENDLIST'))
        self.assertEqual(variant.count('#EXTINF'), total)

        # Le debut arrive sans attendre la fin de la preparation.
        first = self.movies.segment(jid, 0, 0, timeout=30)
        self.assertIsNotNone(first)

        # Saut aux trois quarts : le segment est produit par une passe qui
        # repart de ce point, horodate a sa place dans le film.
        target = total - 3
        asked = time.time()
        path = self.movies.segment(jid, 1, target, timeout=30)
        self.assertIsNotNone(path)
        self.assertLess(time.time() - asked, 10)
        self.assertRegex(os.path.basename(path), r'^r2_')
        self.assertTrue(any(start > 0 for start in RangeHandler.requests), 'FFmpeg aurait du sauter dans le fichier')
        self.assertAlmostEqual(first_pts(path) - first_pts(first), target * SEGMENT, delta=.1)

        job = self.wait(jid, lambda j: j['state'] == 'ready')
        folder = pathlib.Path(self.movies.root) / jid
        rt = self.movies.runtime[jid]
        for rung in range(2):
            self.assertEqual(sorted(rt['index'][rung]), list(range(total)))
        self.assertFalse(list(folder.glob('*.tmp')))
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                                   '-of', 'json', str(folder / 'q0.mp4')]))
        self.assertAlmostEqual(float(info['format']['duration']), DURATION, delta=.5)
        subtitles = (folder / 'subtitles.vtt').read_text()
        for cue in ('Debut du film', 'Milieu du film', 'Fin du film'):
            self.assertEqual(subtitles.count(cue), 1, subtitles)
        self.assertLess(subtitles.index('Debut'), subtitles.index('Milieu'))
        self.assertFalse(self.transcoder.reservations)

        # Apres un redemarrage du serveur, le film pret reste lisible.
        again = Movies(self.cfg, self.movies.root, None, self.transcoder)
        self.assertEqual(again.jobs[jid]['state'], 'ready')
        self.assertIsNotNone(again.segment(jid, 1, total - 1, timeout=1))
        self.assertIsNone(again.segment(jid, 1, total, timeout=1))

    def test_abandoned_request_does_not_steal_the_encoder(self):
        job = self.movies.start({'provider_id': 'p', 'stream_id': 1, 'title': 'Film'}, self.source, 360)
        jid = job['id']
        self.wait(jid, lambda j: j.get('playable'))
        total = DURATION // SEGMENT
        # Le lecteur attend le segment 8, puis saute plus loin sans que le
        # serveur sache que la premiere requete a ete abandonnee.
        stale = threading.Thread(target=self.movies.segment, args=(jid, 0, 8), kwargs={'timeout': 12})
        stale.start()
        time.sleep(.3)
        self.assertIsNotNone(self.movies.segment(jid, 0, total - 2, timeout=30))
        stale.join()
        # Une passe du debut, une pour le 8 au plus, une pour le saut :
        # pas de relances en boucle entre les deux requetes.
        self.assertLessEqual(self.movies.runtime[jid]['run'], 3)

    def test_single_connection_fetches_subtitles_first(self):
        self.cfg['providers'][0]['max_connections'] = 1
        job = self.movies.start({'provider_id': 'p', 'stream_id': 1, 'title': 'Film'}, self.source, 240, subtitle=2)
        jid = job['id']
        # Une seule connexion : pas de lecture tant que les sous-titres ne sont pas la.
        self.wait(jid, lambda j: j.get('stage') == 'subtitles' or j.get('playable'), timeout=30)
        job = self.wait(jid, lambda j: j.get('playable'))
        self.assertTrue(job.get('subtitles_ready'))
        self.assertNotIn('stage', job)
        folder = pathlib.Path(self.movies.root) / jid
        self.assertIn('Fin du film', (folder / 'subtitles.vtt').read_text())
        self.assertIsNotNone(self.movies.segment(jid, 0, 0, timeout=30))

    def test_next_episode_pauses_the_previous_preparation(self):
        # Abonnement a une connexion : l'episode 6 se prepare encore quand on
        # lance le 7.
        self.cfg['providers'][0]['max_connections'] = 1
        six = self.movies.start({'provider_id': 'p', 'stream_id': 6, 'title': 'Episode 6'}, self.source, 240, account='moi')
        self.wait(six['id'], lambda j: j.get('playable'))
        self.assertIsNotNone(self.movies.segment(six['id'], 0, 0, timeout=30))
        # Un autre compte ne coupe pas une preparation qu'on regarde, et sa
        # demande refusee ne laisse rien dans la liste.
        with self.assertRaises(CapacityError):
            self.movies.start({'provider_id': 'p', 'stream_id': 9, 'title': 'Autre'}, self.source, 240, account='autre')
        self.assertEqual(len(self.movies.jobs), 1)
        # Le meme compte passe a l'episode suivant : le 6 cede sa connexion.
        seven = self.movies.start({'provider_id': 'p', 'stream_id': 7, 'title': 'Episode 7'}, self.source, 240, account='moi')
        self.assertEqual(self.movies.jobs[seven['id']]['state'], 'preparing')
        self.assertEqual(self.movies.jobs[six['id']]['state'], 'paused')
        # Ce qui etait fait du 6 reste lisible.
        self.assertIsNotNone(self.movies.segment(six['id'], 0, 0, timeout=1))
        self.wait(seven['id'], lambda j: j['state'] == 'ready')
        # Connexion rendue : le 6 reprend ou il en etait, jusqu'au bout.
        self.wait(six['id'], lambda j: j['state'] == 'ready')
        self.assertFalse(self.transcoder.reservations)

    def test_delete_while_preparing(self):
        job = self.movies.start({'provider_id': 'p', 'stream_id': 1, 'title': 'Film'}, self.source, 480, subtitle=2)
        jid = job['id']
        self.wait(jid, lambda j: j.get('playable'))
        folder = pathlib.Path(self.movies.root) / jid
        waiting = []
        waiter = threading.Thread(target=lambda: waiting.append(self.movies.segment(jid, 0, DURATION // SEGMENT - 1, timeout=20)))
        waiter.start()
        time.sleep(.5)
        self.assertTrue(self.movies.delete(jid))
        waiter.join(5)
        self.assertFalse(waiter.is_alive(), 'le lecteur en attente doit abandonner')
        self.assertEqual(waiting, [None])
        deadline = time.time() + 10
        while self.transcoder.reservations and time.time() < deadline:
            time.sleep(.2)
        self.assertFalse(self.transcoder.reservations, 'les connexions a l abonnement doivent etre rendues')
        time.sleep(1)
        self.assertFalse(folder.exists())
        self.assertNotIn(jid, [j['id'] for j in self.movies.list()])
        self.assertFalse(self.movies.delete(jid))
        # Plus aucun FFmpeg ne travaille pour cette preparation.
        self.assertFalse(subprocess.run(['pgrep', '-f', jid], capture_output=True).stdout)


class EpisodeSourceTests(unittest.TestCase):
    def test_resumed_episode_is_read_under_series(self):
        class Catalog:
            def episode_get(self, pid, eid):
                return {'episode_id': eid, 'container': 'mkv'}
            def vod_get(self, pid, sid):
                return {'stream_id': sid, 'container': 'mp4'}
        with tempfile.TemporaryDirectory() as root:
            cfg = {'providers': [{'id': 'p', 'host': 'http://panel', 'username': 'u', 'password': 'pw'}]}
            movies = Movies(cfg, root, Catalog(), None)
            self.assertEqual(movies._job_source({'provider': 'p', 'movie': 904, 'kind': 'episode'}),
                             'http://panel/series/u/pw/904.mkv')
            self.assertEqual(movies._job_source({'provider': 'p', 'movie': 7}), 'http://panel/movie/u/pw/7.mp4')


if __name__ == '__main__':
    unittest.main()
