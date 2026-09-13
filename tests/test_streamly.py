import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch, Mock
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from streamly.transcoder import Transcoder, CapacityError, _redact_credentials
from streamly.auth import Sessions
from streamly.catalog import Catalog
from streamly.xtream import parse_name
from streamly import app, config
from streamly.vod import Movies

CFG = json.loads((pathlib.Path(__file__).resolve().parents[1] / 'server/config.example.json').read_text())
CFG.update(token='test-admin', viewer_token='test-viewer', max_concurrent_streams=2,
           providers=[dict(id='p', name='Test', host='http://example.invalid', username='u', password='p', max_connections=2)])

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Transcoder(CFG, self.tmp.name + '/hls', self.tmp.name + '/logs', monitor=False)
        self.spawn = patch.object(self.t, '_spawn').start()
    def tearDown(self):
        self.t.close(); patch.stopall(); self.tmp.cleanup()
    def open(self, who='a', identity='one', **kwargs):
        return self.t.open(who, identity, [{'provider':'p', 'url':'http://source'}], **kwargs)
    def test_same_channel_shared_stop_isolated(self):
        a, b = self.open(), self.open('b')
        self.assertEqual(len(self.t.workers), 1)
        self.t.release(a, 'b'); self.assertEqual(len(self.t.tickets), 2)
        self.t.release(a, 'a'); self.assertEqual(len(self.t.workers), 1)
        self.assertEqual(self.t.status(b)['viewers'], 1)
        self.t.release(b, 'b'); self.assertEqual(len(self.t.workers), 0)
    def test_capacity_never_evicts_other_viewer(self):
        self.open(); self.open('b', 'two')
        with self.assertRaises(CapacityError): self.open('c', 'three')
        self.assertEqual(len(self.t.tickets), 2)
    def test_provider_limit_includes_movie_preparation(self):
        self.t.reserve('job', 'p'); self.open()
        with self.assertRaises(CapacityError): self.open('b', 'two')
    def test_budget_atomic_and_native_master_capped(self):
        ticket = self.open(ceiling=650000, budget=100)
        self.assertTrue(self.t.charge(ticket, 70)); self.assertFalse(self.t.charge(ticket, 31))
        self.assertEqual(self.t.status(ticket)['bytes'], 70)
        playlist = self.t.master_playlist(ticket)
        self.assertNotIn('s_0.m3u8', playlist); self.assertNotIn('s_1.m3u8', playlist)
        self.assertIn('s_2.m3u8', playlist)
    def test_stalled_alive_process_triggers_generation(self):
        ticket = self.t.open('a', 'one', [{'provider':'p','url':'http://a'}, {'provider':'p','url':'http://b'}])
        w = next(iter(self.t.workers.values())); w['state'] = 'playing'; w['started'] = time.time() - 60
        w['proc'] = Mock(); w['proc'].poll.return_value = None
        os.makedirs(self.tmp.name + '/hls/' + w['key'])
        self.t._tick()
        self.assertEqual(w['generation'], 1); self.assertEqual(w['failovers'], 1)
        w['proc'].terminate.assert_called_once()
    def test_abandoned_ticket_reaped(self):
        ticket = self.open(); self.t.tickets[ticket]['last'] -= 200
        self.t._tick(); self.assertFalse(self.t.workers)
    def test_sport_cadence_and_segment_alignment(self):
        cmd = self.t._command('http://source', {'height':720,'width':1280,'fps':50,'streams':[{'codec_type':'audio'}]}, 2)
        self.assertEqual(cmd[cmd.index('-g:v:0')+1], '100')
        self.assertEqual(cmd[cmd.index('-r:v:0')+1], '50.0')
        self.assertIn('g2_%v_%09d.ts', cmd)
    def test_ffmpeg_errors_never_leak_provider_credentials(self):
        cfg = dict(CFG, providers=[dict(id='p', host='http://panel.invalid',
            username='SECRETUSER', password='SECRETPASS', max_connections=1)])
        t = Transcoder(cfg, self.tmp.name + '/h2', self.tmp.name + '/l2', monitor=False)
        try:
            line = '[http @ 0x1] HTTP error 403 Forbidden http://panel.invalid/SECRETUSER/SECRETPASS/1234: denied?token=ABCDEF123'
            red = _redact_credentials(line, cfg)
            self.assertNotIn('SECRETUSER', red); self.assertNotIn('SECRETPASS', red)
            self.assertNotIn('ABCDEF123', red); self.assertIn('403', red)
            os.makedirs(self.tmp.name + '/l2', exist_ok=True)
            with open(self.tmp.name + '/l2/ffmpeg_k.log', 'w') as fh:
                fh.write(line + '\n')
            t.log_dir = self.tmp.name + '/l2'
            self.assertNotIn('SECRETPASS', t.probe_last_error('k') or '')
        finally:
            t.close()

class PassthroughTests(unittest.TestCase):
    """Remux : une source deja conforme ne doit plus passer par x264."""
    H264 = {'height':720, 'width':1280, 'fps':25, 'codec':'h264', 'pix_fmt':'yuv420p',
            'video_bitrate':1400000, 'bitrate':1500000, 'audio_codec':'aac',
            'audio_bitrate_src':96000, 'streams':[{'codec_type':'video'},{'codec_type':'audio'}]}
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Transcoder(CFG, self.tmp.name + '/hls', self.tmp.name + '/logs', monitor=False)
        self.spawn = patch.object(self.t, '_spawn').start()
    def tearDown(self):
        self.t.close(); patch.stopall(); self.tmp.cleanup()

    def test_compatible_source_is_copied_not_encoded(self):
        plan = self.t.passthrough_plan(self.H264)
        self.assertTrue(plan['copy_audio'])
        cmd = self.t._command('http://source', self.H264, 3, passthrough=plan)
        self.assertEqual(cmd[cmd.index('-c:v')+1], 'copy')
        self.assertEqual(cmd[cmd.index('-c:a')+1], 'copy')
        self.assertNotIn('libx264', cmd); self.assertNotIn('-filter_complex', cmd)
        # Le routage des segments ne change pas : une variante, niveau 0.
        self.assertIn('g3_%v_%09d.ts', cmd)
        self.assertEqual(cmd[cmd.index('-var_stream_map')+1], 'v:0,a:0')

    def test_non_aac_audio_is_reencoded_but_video_stays_copied(self):
        plan = self.t.passthrough_plan(dict(self.H264, audio_codec='ac3', audio_bitrate_src=384000))
        self.assertFalse(plan['copy_audio'])
        cmd = self.t._command('http://source', self.H264, 0, passthrough=plan)
        self.assertEqual(cmd[cmd.index('-c:v')+1], 'copy')
        self.assertEqual(cmd[cmd.index('-c:a')+1], 'aac')

    def test_incompatible_sources_fall_back_to_encoding(self):
        for media in (dict(self.H264, codec='hevc'),
                      dict(self.H264, pix_fmt='yuv420p10le'),
                      {'height':720, 'width':1280, 'fps':25, 'unverified':True, 'streams':[]}):
            self.assertIsNone(self.t.passthrough_plan(media))
        off = Transcoder(dict(CFG, passthrough=False), self.tmp.name+'/h3', self.tmp.name+'/l3', monitor=False)
        try:
            self.assertIsNone(off.passthrough_plan(self.H264))
        finally:
            off.close()

    def test_ceiling_is_never_exceeded_by_a_remux(self):
        # 1,5 Mb/s ne tient pas sous le plafond Eco (650 kb/s) : on reencode.
        self.assertIsNone(self.t.passthrough_plan(self.H264, ceiling=650000))
        self.assertIsNotNone(self.t.passthrough_plan(self.H264, ceiling=2000000))
        # Debit inconnu (cas courant d'un TS en HTTP) : remux optimiste,
        # marque non mesure, plafond verifie plus tard sur les segments.
        blind = dict(self.H264, video_bitrate=0, bitrate=0)
        plan = self.t.passthrough_plan(blind, ceiling=2000000)
        self.assertFalse(plan['measured'])

    def test_remuxed_worker_exposes_one_level_and_source_resolution(self):
        ticket = self.t.open('a', 'one', [{'provider':'p','url':'http://s'}], ceiling=0)
        w = next(iter(self.t.workers.values()))
        w['media'] = dict(self.H264, height=1080, width=1920)
        w['passthrough'] = self.t.passthrough_plan(w['media'])
        self.assertEqual(self.t.allowed_levels(self.t.ticket(ticket)), [0])
        playlist = self.t.master_playlist(ticket)
        self.assertIn('RESOLUTION=1920x1080', playlist)
        self.assertIn('g0_s_0.m3u8', playlist)
        self.assertNotIn('g0_s_1.m3u8', playlist)
        self.assertTrue(self.t.status(ticket)['passthrough'])

    def test_joining_viewer_below_the_source_bitrate_is_told_why(self):
        self.t.open('a', 'one', [{'provider':'p','url':'http://s'}], ceiling=0)
        w = next(iter(self.t.workers.values()))
        w['media'] = self.H264
        w['passthrough'] = self.t.passthrough_plan(self.H264)
        with self.assertRaises(CapacityError):
            self.t.open('b', 'one', [{'provider':'p','url':'http://s'}], ceiling=650000)
        # Un plafond suffisant partage le meme worker, sans reencodage.
        self.t.open('c', 'one', [{'provider':'p','url':'http://s'}], ceiling=3000000)
        self.assertEqual(len(self.t.workers), 1)


class PassthroughDowngradeTests(unittest.TestCase):
    """Un remux dont le debit reel depasse le plafond doit rendre la main."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Transcoder(CFG, self.tmp.name + '/hls', self.tmp.name + '/logs', monitor=False)
        self.spawn = patch.object(self.t, '_spawn').start()
        self.ticket = self.t.open('a', 'one', [{'provider':'p','url':'http://s'}], ceiling=650000)
        self.w = next(iter(self.t.workers.values()))
        self.w['state'] = 'playing'
        self.w['passthrough'] = {'audio':True, 'copy_audio':True, 'bitrate':1200000, 'measured':False}
        self.dir = pathlib.Path(self.tmp.name) / 'hls' / self.w['key']
        self.dir.mkdir(parents=True)
    def tearDown(self):
        self.t.close(); patch.stopall(); self.tmp.cleanup()
    def write_segments(self, size):
        names = []
        for i in range(5):
            name = 'g0_0_%09d.ts' % i
            (self.dir / name).write_bytes(b'x' * size)
            names.append('#EXTINF:2.000,\n' + name)
        (self.dir / 'g0_s_0.m3u8').write_text('#EXTM3U\n' + '\n'.join(names) + '\n')

    def test_source_above_the_ceiling_falls_back_to_the_ladder(self):
        self.write_segments(500000)  # 500 ko / 2 s = 2 Mb/s, au-dessus de 650 kb/s
        self.t._tick()
        self.assertIsNone(self.w['passthrough'])
        self.assertTrue(self.w['passthrough_denied'])
        self.assertEqual(self.w['generation'], 1)
        # Le spectateur retrouve les barreaux compatibles avec son plafond.
        self.assertEqual(self.t.allowed_levels(self.t.ticket(self.ticket)), [2, 3])

    def test_source_under_the_ceiling_keeps_the_remux_and_records_the_rate(self):
        self.write_segments(120000)  # 120 ko / 2 s = 480 kb/s
        self.t._tick()
        self.assertTrue(self.w['passthrough']['measured'])
        self.assertEqual(self.w['passthrough']['bitrate'], 480000)
        self.assertEqual(self.w['generation'], 0)
        self.assertEqual(self.t.allowed_levels(self.t.ticket(self.ticket)), [0])

    def test_measurement_waits_for_enough_segments(self):
        (self.dir / 'g0_0_000000000.ts').write_bytes(b'x' * 500000)
        (self.dir / 'g0_s_0.m3u8').write_text('#EXTM3U\n#EXTINF:2.000,\ng0_0_000000000.ts\n')
        self.t._tick()
        self.assertIsNotNone(self.w['passthrough'])
        self.assertFalse(self.w['passthrough']['measured'])


class BitrateCacheTests(unittest.TestCase):
    """Le debit mesure survit a la lecture : une seule mauvaise surprise par chaine."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name + '/data'; os.makedirs(self.state)
    def tearDown(self): self.tmp.cleanup()
    def make(self):
        t = Transcoder(CFG, self.tmp.name + '/hls', self.tmp.name + '/logs',
                       monitor=False, state_dir=self.state)
        patch.object(t, '_spawn').start()
        return t

    def test_measurement_is_persisted_and_reused_after_restart(self):
        t = self.make()
        try:
            t._remember_bitrate('http://s/secret-url', 2400000)
        finally:
            t.close(); patch.stopall()
        again = self.make()
        try:
            self.assertEqual(again._known_bitrate('http://s/secret-url'), 2400000)
            # L'URL porte les identifiants : seule son empreinte est ecrite.
            written = pathlib.Path(self.state, 'source_bitrates.json').read_text()
            self.assertNotIn('secret-url', written)
        finally:
            again.close(); patch.stopall()

    def test_known_bitrate_decides_before_ffmpeg_starts(self):
        t = self.make()
        try:
            media = {'height':1080, 'width':1920, 'fps':25, 'codec':'h264', 'pix_fmt':'yuv420p',
                     'video_bitrate':0, 'bitrate':0, 'audio_codec':'aac', 'streams':[]}
            # Sans mesure : remux optimiste, plafond verifie plus tard.
            self.assertFalse(t.passthrough_plan(media, ceiling=1150000)['measured'])
            # Avec la mesure d'une lecture precedente : decision immediate.
            t._remember_bitrate('http://s/1', 2900000)
            known = dict(media, bitrate=t._known_bitrate('http://s/1'), video_bitrate=0)
            self.assertIsNone(t.passthrough_plan(known, ceiling=1150000))
            self.assertTrue(t.passthrough_plan(known, ceiling=0)['measured'])
        finally:
            t.close(); patch.stopall()

    def test_stale_measurement_is_retried(self):
        t = self.make()
        try:
            t._remember_bitrate('http://s/1', 2900000)
            t._bitrates[t._source_id('http://s/1')]['at'] -= 25 * 3600
            self.assertEqual(t._known_bitrate('http://s/1'), 0)
        finally:
            t.close(); patch.stopall()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.cat = Catalog(self.tmp.name + '/catalog.db')
    def tearDown(self): self.cat.close(); self.tmp.cleanup()
    def test_unicode_names_never_collapse(self):
        a = parse_name('24/7 AR| آخر كلام')[1]; b = parse_name('24/7 AR| افطارنا غير')[1]
        self.assertNotEqual(a, b); self.assertIn('آخر', a)
    def test_vod_metadata_survives_refresh(self):
        client = Mock(); client.vod_streams.return_value = [{'stream_id':1,'name':'FR| Film','category_id':1}]
        client._api.return_value = [{'category_id':1,'category_name':'Films'}]
        self.cat.sync_vod({'id':'p'}, client, lambda _:None)
        self.cat.vod_set_details('p', 1, 'mkv', 800, '01:20:00', 'plot')
        self.cat.sync_vod({'id':'p'}, client, lambda _:None)
        self.assertEqual(self.cat.vod_get('p', 1)['container'], 'mkv')
        self.assertEqual(self.cat.vod_get('p', 1)['bitrate'], 800)
    def test_connections_are_not_shared_between_threads(self):
        connections = []; 
        def capture():
            connections.append(self.cat._db); self.cat.close()
        t = threading.Thread(target=capture); t.start(); t.join()
        self.assertIsNot(self.cat._db, connections[0])

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = self.tmp.name
        self.responses = []
        self.old = app.STATE
        state = Mock(); state.cfg = dict(CFG); state.sessions = Sessions(state.cfg)
        state.transcoder = Transcoder(CFG, root + '/hls', root + '/logs', monitor=False)
        state.catalog = Catalog(root + '/catalog.db')
        state.movies = Movies(CFG, root + '/movies', state.catalog, state.transcoder)
        state.sync_log = []; self.state = app.STATE = state
        app.STATE.provider.side_effect = lambda pid: CFG['providers'][0] if pid == 'p' else None
        with patch('socket.getfqdn', return_value='localhost'):
            self.http = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.url = 'http://127.0.0.1:%s' % self.http.server_port
    def tearDown(self):
        for response in self.responses: response.close()
        self.http.shutdown(); self.http.server_close(); self.state.transcoder.close(); self.state.catalog.close(); app.STATE = self.old; self.tmp.cleanup()
    def request(self, path, data=None, cookie=None, headers=None):
        h = headers or {}
        if cookie: h['Cookie'] = cookie
        if data is not None: h['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.url + path, json.dumps(data).encode() if data is not None else None, h)
        try: response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as e: response = e
        self.responses.append(response)
        return response
    def login(self, token='test-admin'):
        r = self.request('/api/login', {'token':token}); self.assertEqual(r.status, 200)
        self.assertIn('HttpOnly', r.headers['Set-Cookie']); return r.headers['Set-Cookie'].split(';')[0]
    def test_viewer_cannot_administer(self):
        cookie = self.login('test-viewer')
        self.assertEqual(self.request('/api/providers/delete', {'id':'p'}, cookie).status, 403)
        self.assertEqual(self.request('/api/viewer-token', {}, cookie).status, 403)
        self.assertEqual(self.request('/api/me', cookie=cookie).status, 200)
        self.assertEqual(self.request('/api/me').status, 401)
    def test_cross_origin_and_logout(self):
        cookie = self.login()
        self.assertEqual(self.request('/api/stop', {}, cookie, {'Origin':'https://evil.invalid'}).status, 403)
        self.request('/api/logout', {}, cookie)
        self.assertEqual(self.request('/api/me', cookie=cookie).status, 401)
    def test_range_download_and_cookie_protection(self):
        jid = 'testjob'; folder = pathlib.Path(self.state.movies.root) / jid; folder.mkdir()
        (folder/'q0.mp4').write_bytes(b'abcdefghij'); self.state.movies.jobs[jid] = {'state':'ready','height':480}
        cookie = self.login()
        r = self.request('/media/testjob/q0.mp4', cookie=cookie, headers={'Range':'bytes=3-6'})
        self.assertEqual(r.status, 206); self.assertEqual(r.read(), b'defg'); self.assertEqual(r.headers['Content-Range'],'bytes 3-6/10')
        self.assertEqual(self.request('/media/testjob/q0.mp4').status, 401)
        self.assertEqual(self.request('/media/testjob/q0.mp4',cookie=cookie, headers={'Range':'bytes=50-'}).status,416)
    def test_credentials_redacted_from_log(self):
        self.request('/s/never-log-this-secret/master.m3u8')
        self.assertNotIn('never-log-this-secret', app.ACCESS_LOG[-1])

if __name__ == '__main__': unittest.main()
