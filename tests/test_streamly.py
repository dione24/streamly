import http.client
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
from streamly.xtream import parse_name, parse_series_info
from streamly import m3u
from streamly import app, config
from streamly import player as playermod
from streamly.epg import Guide
import gzip
import xml.etree.ElementTree as ET
from streamly.vod import Movies

CFG = json.loads((pathlib.Path(__file__).resolve().parents[1] / 'server/config.example.json').read_text())
CFG.update(token='test-admin', viewer_token='test-viewer', max_concurrent_streams=2,
           providers=[dict(id='p', name='Test', host='http://example.invalid', username='u', password='p', max_connections=2)],
           players=[dict(username='tv', password='salon2024xyz', mode='eco')])

LIVE = [
    {'stream_id': 1, 'name': 'FR| TF1 HD', 'category_id': 1, 'stream_icon': 'http://img/tf1.png', 'epg_channel_id': 'tf1.fr'},
    {'stream_id': 2, 'name': 'FR| TF1 FHD', 'category_id': 1},
    {'stream_id': 3, 'name': 'FR| TF1 [BK]', 'category_id': 1},
    {'stream_id': 4, 'name': 'FR| M6 HD', 'category_id': 1},
    {'stream_id': 5, 'name': 'EN| BBC One', 'category_id': 2},
    {'stream_id': 6, 'name': 'AR| MBC 1', 'category_id': 9},
]


def seed_live(catalog, streams=LIVE):
    client = Mock()
    client.live_streams.return_value = streams
    client.live_categories.return_value = [{'category_id': 1, 'category_name': 'France'},
                                           {'category_id': 2, 'category_name': 'UK "News"'}]
    catalog.sync_provider({'id': 'p'}, client, lambda _: None)

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
    def test_quality_driven_rate_control_is_capped_at_the_rung_target(self):
        media = {'height':720,'width':1280,'fps':25,'streams':[{'codec_type':'audio'}]}
        cmd = self.t._command('http://source', media, 0)
        self.assertEqual(cmd[cmd.index('-crf:v:0')+1], '24')
        # Le plafond est le debit cible, pas le maxrate : un contenu difficile
        # ne doit pas couter plus qu'avec l'ancien debit moyen.
        self.assertEqual(cmd[cmd.index('-maxrate:v:0')+1], CFG['ladder'][0]['bitrate'])
        self.assertNotIn('-b:v:0', cmd)
        abr = Transcoder(dict(CFG, rate_control='abr'), self.tmp.name + '/h4', self.tmp.name + '/l4', monitor=False)
        try:
            old = abr._command('http://source', media, 0)
            self.assertEqual(old[old.index('-b:v:0')+1], CFG['ladder'][0]['bitrate'])
            self.assertNotIn('-crf:v:0', old)
        finally:
            abr.close()
    def test_only_playable_rungs_are_encoded_and_keep_their_ladder_index(self):
        media = {'height':720,'width':1280,'fps':25,'streams':[{'codec_type':'audio'}]}
        n = len(CFG['ladder'])
        cmd = self.t._command('http://source', media, 0, levels=[n-2, n-1])
        self.assertEqual(cmd.count('libx264'), 2)
        self.assertEqual(cmd[cmd.index('-var_stream_map')+1],
                         'v:0,a:0,name:%d v:1,a:1,name:%d' % (n-2, n-1))
        self.assertIn('[0:v]split=2', cmd[cmd.index('-filter_complex')+1])
    def test_higher_ceiling_viewer_extends_the_encoded_rungs(self):
        with patch.object(self.t, '_spawn'):
            self.t.open('a', 'one', [{'provider':'p','url':'http://a'}], ceiling=650000)
            w = next(iter(self.t.workers.values()))
            eco = list(w['levels'])
            self.assertEqual(eco, self.t.allowed_levels({'ceiling': 650000}))
            self.assertLess(len(eco), len(CFG['ladder']))
            w['state'] = 'playing'
            self.t.open('b', 'one', [{'provider':'p','url':'http://a'}], ceiling=0)
            self.assertEqual(w['levels'], list(range(len(CFG['ladder']))))
            self.assertEqual(w['generation'], 1)
            # Un spectateur de plus au meme plafond ne relance rien.
            self.t.open('c', 'one', [{'provider':'p','url':'http://a'}], ceiling=650000)
            self.assertEqual(w['generation'], 1)
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

class LiveEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Transcoder(CFG, self.tmp.name + '/hls', self.tmp.name + '/logs', monitor=False)
        patch.object(self.t, '_spawn').start()
    def tearDown(self):
        self.t.close(); patch.stopall(); self.tmp.cleanup()
    def test_each_generation_numbers_segments_after_the_previous(self):
        media = {'width': 1280, 'height': 720, 'fps': 25, 'streams': [{'codec_type': 'audio'}]}
        for cmd in (self.t._command('http://s', media, 0, start_number=7), self.t._command('http://s', media, 1, audio_only=True, start_number=7),
                    self.t._command('http://s', media, 2, passthrough={'audio': True, 'copy_audio': True}, start_number=7)):
            self.assertEqual(cmd[cmd.index('-start_number') + 1], '7')
            self.assertLess(cmd.index('-start_number'), cmd.index('-hls_segment_filename'))
        outdir = pathlib.Path(self.tmp.name, 'seq'); outdir.mkdir()
        self.assertLess(abs(Transcoder._next_sequence(str(outdir), 0) - time.time()), 5)
        (outdir / 'g0_s_2.m3u8').write_text('#EXTM3U\n#EXTINF:2,\ng0_2_100.ts\n#EXTINF:2,\ng0_2_101.ts\n')
        (outdir / 'g0_s_3.m3u8').write_text('#EXTM3U\n#EXTINF:2,\ng0_3_100.ts\n')
        # Le plus avance des barreaux fixe la suite : jamais de numero reutilise.
        self.assertEqual(Transcoder._next_sequence(str(outdir), 1), 102)
    def test_failed_channel_restarts_when_nobody_else_watches(self):
        self.t.open('a', 'one', [{'provider': 'p', 'url': 'http://source'}])
        self.t.workers[next(iter(self.t.workers))]['state'] = 'failed'
        self.t.open('a', 'one', [{'provider': 'p', 'url': 'http://source'}])
        self.assertEqual(next(iter(self.t.workers.values()))['state'], 'starting')
        self.assertEqual(len(self.t.tickets), 1)
        # Un autre spectateur la regarde encore : on ne coupe pas sa lecture.
        self.t.open('b', 'one', [{'provider': 'p', 'url': 'http://source'}])
        self.t.workers[next(iter(self.t.workers))]['state'] = 'failed'
        with self.assertRaises(CapacityError):
            self.t.open('a', 'one', [{'provider': 'p', 'url': 'http://source'}])
    def test_player_tickets_expire_sooner(self):
        ticket = self.t.open('player:tv', 'one', [{'provider': 'p', 'url': 'http://source'}], idle=45)
        self.t.tickets[ticket]['last'] -= 60
        self.assertIsNone(self.t.ticket(ticket))
    def test_preview_master_matches_the_encoded_ladder(self):
        body = self.t.preview_master(650000, lambda i: '42/%d.m3u8' % i)
        self.assertEqual([l for l in body.splitlines() if not l.startswith('#')], ['42/2.m3u8', '42/3.m3u8'])
        self.assertNotIn('42/0.m3u8', self.t.preview_master(1150000, lambda i: '42/%d.m3u8' % i))


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
        # Debit inconnu (cas courant d'un TS en HTTP) : sous plafond on
        # encode, sinon la source part a plein debit le temps de la mesure.
        blind = dict(self.H264, video_bitrate=0, bitrate=0)
        self.assertIsNone(self.t.passthrough_plan(blind, ceiling=2000000))
        # Sans plafond (Sport), le remux reste permis et sera mesure.
        self.assertFalse(self.t.passthrough_plan(blind, ceiling=0)['measured'])

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
            # Sans mesure : pas de remux sous plafond, remux permis en Sport.
            self.assertIsNone(t.passthrough_plan(media, ceiling=1150000))
            self.assertFalse(t.passthrough_plan(media, ceiling=0)['measured'])
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

class PlayerIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.cat = Catalog(self.tmp.name + '/catalog.db')
        self.facade = playermod.PlayerFacade(self.cat)
    def tearDown(self): self.cat.close(); self.tmp.cleanup()
    def test_one_entry_per_channel_without_backups(self):
        seed_live(self.cat)
        names = sorted(c['name'] for c in self.facade.index()[0])
        # TF1 HD, FHD et [BK] ne font qu'une chaine : la source est choisie a l'ouverture.
        self.assertEqual(names, ['AR| MBC 1', 'EN| BBC One', 'FR| M6', 'FR| TF1'])
    def test_ids_are_stable_and_follow_a_resync(self):
        seed_live(self.cat)
        before = {c['name']: c['id'] for c in self.facade.index()[0]}
        self.assertTrue(all(0 < i <= 0x7FFFFFFF for i in before.values()))
        seed_live(self.cat, LIVE + [{'stream_id': 7, 'name': 'FR| ARTE', 'category_id': 1}])
        after = {c['name']: c['id'] for c in self.facade.index()[0]}
        self.assertIn('FR| ARTE', after)
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertEqual(self.facade.channel(before['FR| TF1'])['canonical'], 'TF1')
    def test_hash_collisions_never_share_an_id(self):
        seed_live(self.cat)
        with patch.object(playermod, 'stream_id', return_value=42), \
             patch.object(playermod, 'category_id', return_value=7):
            self.facade.invalidate()
            channels, by_id, categories = self.facade.index()
        self.assertEqual(len(by_id), 4)
        self.assertEqual(len({c['id'] for c in categories}), len(categories))
    def test_uncategorized_channels_are_grouped(self):
        seed_live(self.cat)
        mbc = next(c for c in self.facade.index()[0] if c['canonical'] == 'MBC 1')
        self.assertEqual(mbc['category'], playermod.UNCATEGORIZED)
    def test_whole_bouquet_is_fast(self):
        seed_live(self.cat, [{'stream_id': i, 'name': 'FR| CANAL X%05d HD' % i, 'category_id': i % 40}
                             for i in range(1, 20001)])
        started = time.time()
        body = self.facade.m3u({'username': 'tv', 'password': 'pw'}, 'http://h')
        self.assertLess(time.time() - started, 3)
        self.assertEqual(body.count('#EXTINF'), 20000)


class PlayerAccountTests(unittest.TestCase):
    def test_missing_passwords_are_generated_and_kept(self):
        players, changed = config._players({'players': [{'username': 'tv', 'password': '', 'mode': 'x'}]})
        self.assertTrue(changed)
        self.assertEqual(len(players[0]['password']), 12)
        self.assertTrue(set(players[0]['password']) <= set(config.PLAYER_ALPHABET))
        self.assertEqual(players[0]['mode'], 'balanced')
        again, changed = config._players({'players': players})
        self.assertFalse(changed); self.assertEqual(again, players)
        default, _ = config._players({})
        self.assertEqual(default[0]['username'], 'streamly')
    def test_player_check_is_rate_limited(self):
        sessions = Sessions(dict(CFG))
        self.assertEqual(sessions.player('tv', 'salon2024xyz', '1.2.3.4')['mode'], 'eco')
        self.assertIsNone(sessions.player('tv', 'bad', '1.2.3.4'))
        self.assertIsNone(sessions.player('tv', '', '1.2.3.4'))
        # Le jeton admin n'ouvre pas la facade lecteur.
        self.assertIsNone(sessions.player('tv', 'test-admin', '1.2.3.4'))
        self.assertIsNone(sessions.player('tv', 'mot de passé', '1.2.3.4'))
        for _ in range(10):
            sessions.player('tv', 'bad', '5.6.7.8')
        self.assertIsNone(sessions.player('tv', 'salon2024xyz', '5.6.7.8'))
        self.assertIsNotNone(sessions.player('tv', 'salon2024xyz', '1.2.3.4'))


XMLTV = '''<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="panel">
<channel id="tf1.fr"><display-name>TF1</display-name></channel>
<channel id="m6.fr"><display-name>M6</display-name></channel>
<channel id="inconnue.fr"><display-name>Hors catalogue</display-name></channel>
<programme start="20260919060000 +0200" stop="20260919070000 +0200" channel="tf1.fr"><title>Termine</title></programme>
<programme start="20260919120000 +0000" stop="20260919130000 +0000" channel="tf1.fr"><title>JT &amp; meteo</title></programme>
<programme start="20260919120000 +0000" stop="20260919130000 +0000" channel="inconnue.fr"><title>Ignore</title></programme>
<programme start="20260919120000 +0000" stop="20260919140000 +0000" channel="m6.fr"><title>Film</title></programme>
</tv>
'''
NOW = 1789819200  # 2026-09-19 11:20 UTC


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = pathlib.Path(self.tmp.name)
        self.logs = []
    def tearDown(self): self.tmp.cleanup()
    def source(self, name, text):
        path = self.root / name; path.write_text(text)
        return (name, path.as_uri(), 'VLC')
    def guide(self, sources):
        return Guide(str(self.root / 'guide.xml.gz'), lambda: sources, lambda: {'tf1.fr', 'm6.fr'}, log=self.logs.append)
    def parsed(self, guide):
        return ET.fromstring(gzip.decompress(guide.read()))
    def test_keeps_catalog_channels_and_current_programmes(self):
        guide = self.guide([self.source('a.xml', XMLTV)])
        self.assertTrue(guide.rebuild(now=NOW))
        tv = self.parsed(guide)
        self.assertEqual([c.get('id') for c in tv.iter('channel')], ['tf1.fr', 'm6.fr'])
        self.assertEqual([p.findtext('title') for p in tv.iter('programme')], ['JT & meteo', 'Film'])
    def test_first_provider_owns_a_shared_channel(self):
        other = XMLTV.replace('JT &amp; meteo', 'Doublon').replace('Film', 'Doublon')
        guide = self.guide([self.source('a.xml', XMLTV), self.source('b.xml', other)])
        guide.rebuild(now=NOW)
        titles = [p.findtext('title') for p in self.parsed(guide).iter('programme')]
        self.assertNotIn('Doublon', titles); self.assertEqual(len(titles), 2)
    def test_truncated_guide_keeps_what_was_read(self):
        guide = self.guide([self.source('a.xml', XMLTV[:XMLTV.index('<programme start="20260919120000 +0000" stop="20260919140000')])])
        self.assertTrue(guide.rebuild(now=NOW))
        self.assertEqual([p.findtext('title') for p in self.parsed(guide).iter('programme')], ['JT & meteo'])
    def test_failed_download_keeps_the_previous_guide(self):
        guide = self.guide([self.source('a.xml', XMLTV)]); guide.rebuild(now=NOW); before = guide.read()
        guide.sources = lambda: [('p', (self.root / 'absent.xml').as_uri(), 'VLC')]
        self.assertFalse(guide.rebuild(now=NOW))
        self.assertEqual(guide.read(), before)
        self.assertEqual(sorted(f.name for f in self.root.iterdir()), ['a.xml', 'guide.xml.gz'])


class PlaylistTests(unittest.TestCase):
    """Lien M3U direct : une playlist doit s'importer comme un panel."""
    SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="tf1.fr" tvg-name="TF1" tvg-logo="http://logo/tf1.png" group-title="FR | TNT",FR| TF1 HD
http://serveur/live/u/p/101.ts
#EXTINF:-1 group-title="FR | TNT",FR| FRANCE 2 FHD
http://serveur/live/u/p/102.ts
#EXTINF:-1 group-title="FILMS",Un film quelconque
http://serveur/movie/u/p/9.mkv
#EXTINF:-1,Sans groupe
#EXTGRP:DIVERS
http://serveur/live/u/p/103.ts
"""
    def test_parses_attributes_and_separate_group_lines(self):
        entries = m3u.parse(self.SAMPLE)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]['name'], 'FR| TF1 HD')
        self.assertEqual(entries[0]['epg_id'], 'tf1.fr')
        self.assertEqual(entries[0]['group'], 'FR | TNT')
        self.assertEqual(entries[0]['url'], 'http://serveur/live/u/p/101.ts')
        self.assertEqual(entries[3]['group'], 'DIVERS')

    def test_stream_ids_follow_the_url_not_the_position(self):
        a = m3u.parse(self.SAMPLE)
        shuffled = m3u.parse('#EXTM3U\n' + '\n'.join(self.SAMPLE.splitlines()[5:] + self.SAMPLE.splitlines()[1:5]))
        by_url = {e['url']: m3u.stream_id(e['url']) for e in a}
        for e in shuffled:
            self.assertEqual(m3u.stream_id(e['url']), by_url[e['url']])

    def test_movies_are_not_imported_as_live_channels(self):
        client = m3u.M3UClient('http://x/list.m3u', 'UA')
        client._entries = [e for e in m3u.parse(self.SAMPLE) if not m3u._VOD_RE.search(e['url'])]
        names = [s['name'] for s in client.live_streams()]
        self.assertNotIn('Un film quelconque', names)
        self.assertEqual(len(names), 3)
        self.assertEqual([c['category_name'] for c in client.live_categories()],
                         ['FR | TNT', 'DIVERS'])
        # Chaque entree porte son URL : rien ne permettrait de la reconstruire.
        self.assertEqual(client.live_url(client.live_streams()[0]['stream_id']),
                         'http://serveur/live/u/p/101.ts')

    def test_a_get_php_link_is_recognised_as_an_xtream_panel(self):
        found = m3u.detect_xtream('http://panel.tld:8080/get.php?username=bob&password=s3cr3t&type=m3u_plus')
        self.assertEqual(found, ('http://panel.tld:8080', 'bob', 's3cr3t'))
        # Une playlist ordinaire ne doit pas etre prise pour un panel.
        self.assertIsNone(m3u.detect_xtream('http://cdn.tld/liste.m3u'))
        self.assertIsNone(m3u.detect_xtream('ftp://panel.tld/get.php?username=a&password=b'))
        self.assertIsNone(m3u.detect_xtream('http://panel.tld/autre.php?username=a&password=b'))

    def test_a_link_that_is_not_a_playlist_is_refused(self):
        client = m3u.M3UClient('http://x/list.m3u', 'UA')
        with patch('urllib.request.urlopen') as fake:
            fake.return_value.__enter__.return_value.read.return_value = b'<html>404</html>'
            with self.assertRaises(m3u.PlaylistError):
                client.account_info()


class CatalogPlaylistTests(unittest.TestCase):
    """L'URL d'une entree de playlist doit survivre a la synchronisation."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.cat = Catalog(self.tmp.name + '/c.db')
    def tearDown(self): self.cat.close(); self.tmp.cleanup()
    def test_playlist_url_is_stored_and_returned(self):
        client = Mock()
        client.live_streams.return_value = [{
            'stream_id': 4242, 'name': 'FR| TF1 HD', 'category_id': 'TNT',
            'stream_icon': '', 'epg_channel_id': '', 'url': 'http://serveur/live/u/p/101.ts'}]
        client.live_categories.return_value = [{'category_id': 'TNT', 'category_name': 'TNT'}]
        self.cat.sync_provider({'id': 'liste'}, client, lambda _: None)
        self.assertEqual(self.cat.channel('liste', 4242)['url'], 'http://serveur/live/u/p/101.ts')
        self.assertEqual(self.cat.sources('FR', 'TF1')[0]['url'], 'http://serveur/live/u/p/101.ts')
    def test_xtream_channels_keep_a_null_url(self):
        client = Mock()
        client.live_streams.return_value = [{'stream_id': 7, 'name': 'FR| M6', 'category_id': '1'}]
        client.live_categories.return_value = [{'category_id': '1', 'category_name': 'TNT'}]
        self.cat.sync_provider({'id': 'panel'}, client, lambda _: None)
        self.assertIsNone(self.cat.channel('panel', 7)['url'])


class SeriesTests(unittest.TestCase):
    """Series : import, episodes paresseux, et suppression en cascade."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.cat = Catalog(self.tmp.name + '/c.db')
        self.client = Mock()
        self.client.series.return_value = [
            {'series_id': 10, 'name': 'FR| Engrenages', 'category_id': '3',
             'cover': 'http://img/1.jpg', 'rating': '8.4', 'last_modified': '1700000000'},
            {'series_id': 11, 'name': 'US| Lioness', 'category_id': '3', 'cover': '', 'rating': 'n/a'}]
        self.client.series_categories.return_value = [{'category_id': '3', 'category_name': 'Drame'}]
    def tearDown(self): self.cat.close(); self.tmp.cleanup()

    def test_import_parses_language_and_survives_bad_ratings(self):
        self.assertEqual(self.cat.sync_series({'id': 'p'}, self.client, lambda _: None), 2)
        rows = self.cat.series_browse()
        self.assertEqual({r['lang'] for r in rows}, {'FR', 'US'})
        self.assertEqual(self.cat.series_get('p', 10)['title'], 'Engrenages')
        # 'n/a' ne doit pas faire echouer tout l'import.
        self.assertEqual(self.cat.series_get('p', 11)['rating'], 0.0)
        self.assertEqual([c['name'] for c in self.cat.series_categories()], ['Drame'])

    def test_episodes_are_stored_then_replaced_not_merged(self):
        self.cat.sync_series({'id': 'p'}, self.client, lambda _: None)
        plot, episodes = parse_series_info({
            'info': {'plot': 'Un resume'},
            'episodes': {'1': [
                {'id': '901', 'episode_num': '1', 'title': 'Pilote', 'container_extension': 'mkv',
                 'info': {'duration': '52:00'}},
                {'id': '902', 'episode_num': '2', 'title': 'Suite', 'container_extension': 'mkv'}]}})
        self.cat.series_set_episodes('p', 10, plot, episodes)
        self.assertEqual(len(self.cat.series_episodes('p', 10)), 2)
        self.assertEqual(self.cat.series_get('p', 10)['plot'], 'Un resume')
        self.assertTrue(self.cat.series_get('p', 10)['episodes_at'])
        self.assertEqual(self.cat.episode_get('p', 901)['container'], 'mkv')
        # Une saison retiree par le fournisseur doit disparaitre.
        _, fewer = parse_series_info({'episodes': {'1': [{'id': '901', 'episode_num': '1'}]}})
        self.cat.series_set_episodes('p', 10, '', fewer)
        self.assertEqual([e['episode_id'] for e in self.cat.series_episodes('p', 10)], [901])
        self.assertIsNone(self.cat.episode_get('p', 902))

    def test_removing_a_series_removes_its_episodes(self):
        self.cat.sync_series({'id': 'p'}, self.client, lambda _: None)
        _, eps = parse_series_info({'episodes': {'1': [{'id': '901', 'episode_num': '1'}]}})
        self.cat.series_set_episodes('p', 10, '', eps)
        self.client.series.return_value = [self.client.series.return_value[1]]
        self.cat.sync_series({'id': 'p'}, self.client, lambda _: None)
        self.assertIsNone(self.cat.series_get('p', 10))
        self.assertIsNone(self.cat.episode_get('p', 901), 'episode orphelin laisse en base')

    def test_purge_of_an_absent_provider_covers_series(self):
        self.cat.sync_series({'id': 'p'}, self.client, lambda _: None)
        _, eps = parse_series_info({'episodes': {'1': [{'id': '901', 'episode_num': '1'}]}})
        self.cat.series_set_episodes('p', 10, '', eps)
        removed = self.cat.purge_absent(['autre'])
        self.assertEqual(removed.get('series'), 2)
        self.assertEqual(removed.get('episodes'), 1)

    def test_episode_urls_do_not_use_the_movie_path(self):
        from streamly.xtream import XtreamClient
        c = XtreamClient('http://h:80', 'u', 'p', 'UA')
        self.assertEqual(c.episode_url(901, 'mkv'), 'http://h:80/series/u/p/901.mkv')
        self.assertNotIn('/movie/', c.episode_url(901, 'mkv'))


class AccountDetailsTests(unittest.TestCase):
    """Les panels renvoient des types incoherents : 0 n'est pas 'inconnu'."""
    def test_panel_integers_tolerate_strings_and_blanks(self):
        self.assertEqual(app._panel_int('1764000000'), 1764000000)
        self.assertEqual(app._panel_int(3), 3)
        # Une echeance absente doit rester distinguable d'une echeance a 0.
        self.assertIsNone(app._panel_int(''))
        self.assertIsNone(app._panel_int(None))
        self.assertIsNone(app._panel_int('null'))
        self.assertEqual(app._panel_int('0'), 0)


class ProviderCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.cat = Catalog(self.tmp.name + '/c.db')
    def tearDown(self): self.cat.close(); self.tmp.cleanup()
    def test_counts_are_scoped_to_one_provider(self):
        client = Mock()
        client.live_streams.return_value = [{'stream_id': 1, 'name': 'FR| A', 'category_id': '1'}]
        client.live_categories.return_value = [{'category_id': '1', 'category_name': 'TNT'}]
        self.cat.sync_provider({'id': 'a'}, client, lambda _: None)
        client.live_streams.return_value = [
            {'stream_id': 1, 'name': 'FR| B', 'category_id': '1'},
            {'stream_id': 2, 'name': 'FR| C', 'category_id': '1'}]
        self.cat.sync_provider({'id': 'b'}, client, lambda _: None)
        self.assertEqual(self.cat.provider_counts('a')['channels'], 1)
        self.assertEqual(self.cat.provider_counts('b')['channels'], 2)
        self.assertEqual(self.cat.provider_counts('inconnu'),
                         {'channels': 0, 'vod': 0, 'series': 0, 'episodes': 0})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = self.tmp.name
        self.responses = []
        self.old = app.STATE
        state = Mock(); state.cfg = dict(CFG); state.sessions = Sessions(state.cfg)
        state.transcoder = Transcoder(CFG, root + '/hls', root + '/logs', monitor=False)
        state.catalog = Catalog(root + '/catalog.db')
        state.movies = Movies(CFG, root + '/movies', state.catalog, state.transcoder)
        state.player = playermod.PlayerFacade(state.catalog, state.transcoder)
        state.guide = Guide(root + '/guide.xml.gz', lambda: [], lambda: set(), log=lambda _: None)
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
    def test_remember_me_controls_cookie_lifetime(self):
        kept = self.request('/api/login', {'token': 'test-admin', 'remember': True})
        self.assertIn('Max-Age=604800', kept.headers['Set-Cookie'])
        # Case decochee : cookie de session, qui meurt avec le navigateur.
        session_only = self.request('/api/login', {'token': 'test-admin', 'remember': False})
        self.assertNotIn('Max-Age', session_only.headers['Set-Cookie'])
        self.assertIn('HttpOnly', session_only.headers['Set-Cookie'])
        # Un ancien client qui n'envoie pas le champ garde le comportement d'avant.
        legacy = self.request('/api/login', {'token': 'test-admin'})
        self.assertIn('Max-Age=604800', legacy.headers['Set-Cookie'])
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
        self.request('/get.php?username=tv&password=salon2024xyz')
        self.request('/live/tv/salon2024xyz/12.m3u8')
        self.assertFalse(any('salon2024xyz' in line for line in app.ACCESS_LOG[-2:]))

    # ------------------------------------------------ lecteurs externes

    def test_m3u_points_at_streamly_only(self):
        seed_live(self.state.catalog)
        with patch.object(self.state.transcoder, 'open') as opened:
            self.assertEqual(self.request('/get.php?username=tv&password=nope').status, 401)
            r = self.request('/get.php?username=tv&password=salon2024xyz&type=m3u_plus&output=ts',
                             headers={'Host': 'tv.example:8088'})
            body = r.read().decode()
            head = self.request('/get.php?username=tv&password=salon2024xyz', headers={'Host': 'tv.example:8088'})
        self.assertEqual(r.status, 200); self.assertEqual(head.status, 200)
        self.assertTrue(body.startswith('#EXTM3U'))
        tf1 = next(c for c in self.state.player.index()[0] if c['canonical'] == 'TF1')
        self.assertIn('http://tv.example:8088/live/tv/salon2024xyz/%d.m3u8' % tf1['id'], body)
        self.assertIn('group-title="UK \'News\'"', body)
        # Ni le panel d'origine ni ses identifiants, et toujours du HLS.
        self.assertNotIn('example.invalid', body); self.assertNotIn('/u/p/', body)
        self.assertNotIn('.ts\n', body)
        opened.assert_not_called()
        with patch.object(app, 'GZIP_MIN_BYTES', 0):
            packed = self.request('/get.php?username=tv&password=salon2024xyz', headers={'Host': 'tv.example:8088', 'Accept-Encoding': 'gzip'})
        self.assertEqual(packed.headers['Content-Encoding'], 'gzip')
        import gzip
        self.assertEqual(gzip.decompress(packed.read()).decode(), body)
    def test_xtream_api_contract(self):
        seed_live(self.state.catalog)
        refused = self.request('/player_api.php?username=tv&password=nope')
        self.assertEqual(refused.status, 200)
        self.assertEqual(json.loads(refused.read())['user_info']['auth'], 0)
        account = json.loads(self.request('/player_api.php?username=tv&password=salon2024xyz',
                                          headers={'Host': 'tv.example:8088'}).read())
        self.assertEqual(account['user_info']['auth'], 1)
        self.assertEqual(account['user_info']['allowed_output_formats'], ['m3u8'])
        self.assertEqual((account['server_info']['url'], account['server_info']['port']), ('tv.example', '8088'))
        cats = json.loads(self.request('/player_api.php?username=tv&password=salon2024xyz&action=get_live_categories').read())
        self.assertEqual([c['category_name'] for c in cats], ['Autres', 'France', 'UK "News"'])
        france = next(c['category_id'] for c in cats if c['category_name'] == 'France')
        streams = json.loads(self.request('/player_api.php?username=tv&password=salon2024xyz'
                                          '&action=get_live_streams&category_id=' + france).read())
        self.assertEqual([s['name'] for s in streams], ['FR| M6', 'FR| TF1'])
        self.assertTrue(all(s['direct_source'] == '' and isinstance(s['stream_id'], int) for s in streams))
        self.assertEqual(json.loads(self.request('/player_api.php?username=tv&password=salon2024xyz'
                                                 '&action=get_vod_streams').read()), [])
        # Smarters poste un formulaire, sans Origin.
        req = urllib.request.Request(self.url + '/player_api.php', b'username=tv&password=salon2024xyz&action=get_live_streams',
                                     {'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=3) as posted:
            self.assertEqual(len(json.loads(posted.read())), 4)
        xml = self.request('/xmltv.php?username=tv&password=salon2024xyz')
        self.assertEqual(xml.status, 200); self.assertIn(b'<tv', xml.read())
    def test_guide_is_served_to_players(self):
        seed_live(self.state.catalog)
        self.state.guide.sources = lambda: [('p', pathlib.Path(self.tmp.name, 'x.xml').as_uri(), 'VLC')]
        pathlib.Path(self.tmp.name, 'x.xml').write_text(XMLTV)
        self.state.guide.wanted = lambda: {c['epg_id'] for c in self.state.player.index()[0]}
        self.state.guide.rebuild(now=NOW)
        packed = self.request('/xmltv.php?username=tv&password=salon2024xyz', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(packed.headers['Content-Encoding'], 'gzip')
        tv = ET.fromstring(gzip.decompress(packed.read()))
        # Seul TF1 a un identifiant de guide dans le catalogue de test.
        self.assertEqual([c.get('id') for c in tv.iter('channel')], ['tf1.fr'])
        plain = self.request('/xmltv.php?username=tv&password=salon2024xyz').read()
        self.assertIn(b'JT &amp; meteo', plain)
        self.assertEqual(self.request('/xmltv.php?username=tv&password=nope').status, 401)
    def test_short_epg_comes_from_the_chosen_source(self):
        seed_live(self.state.catalog)
        tf1 = next(c for c in self.state.player.index()[0] if c['canonical'] == 'TF1')
        self.state.client.return_value.live_epg.return_value = [{'title': 'SlQ=', 'start': '1'}, {'title': 'eA==', 'start': '2'}]
        body = json.loads(self.request('/player_api.php?username=tv&password=salon2024xyz'
                                       '&action=get_short_epg&limit=1&stream_id=%d' % tf1['id']).read())
        self.assertEqual(body, {'epg_listings': [{'title': 'SlQ=', 'start': '1'}]})
        # Source HD choisie (720p), identifiant de guide du panel.
        self.state.client.return_value.live_epg.assert_called_with(1, epg_channel_id='tf1.fr')
        self.state.client.return_value.live_epg.side_effect = OSError('panel muet')
        self.assertEqual(json.loads(self.request('/player_api.php?username=tv&password=salon2024xyz'
                                                 '&action=get_short_epg&stream_id=%d' % tf1['id']).read()),
                         {'epg_listings': []})
    def test_forwarded_address_only_trusted_behind_local_proxy(self):
        bad = lambda ip: self.request('/get.php?username=tv&password=nope', headers={'X-Forwarded-For': ip})
        good = lambda ip: self.request('/get.php?username=tv&password=salon2024xyz', headers={'X-Forwarded-For': ip}).status
        # Ecoute publique : l'en-tete est ignore, sinon il suffirait d'en
        # changer pour contourner la limite de tentatives.
        for n in range(10):
            bad('10.0.0.%d' % n)
        self.assertEqual(good('10.9.9.9'), 401)
        self.state.sessions.attempts.clear()
        # Derriere Apache : chaque client garde son propre compteur.
        self.state.cfg['listen_host'] = '127.0.0.1'
        for _ in range(10):
            bad('10.0.0.1')
        self.assertEqual(good('10.0.0.1'), 401)
        self.assertEqual(good('10.0.0.2'), 200)
        # Transition : proxy en place, ecoute encore publique.
        self.state.cfg.update(listen_host='0.0.0.0', trust_proxy=True)
        self.assertEqual(good('10.0.0.1'), 401)
        m3u = self.request('/get.php?username=tv&password=salon2024xyz', headers={
            'X-Forwarded-For': '10.0.0.3', 'X-Forwarded-Proto': 'https', 'Host': 'tv.example'})
        self.assertEqual(m3u.status, 200)
    # ------------------------------------------------ direct lecteur

    def raw(self, path, method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        conn.request(method, path)
        response = conn.getresponse(); body = response.read(); conn.close()
        return response.status, response.headers, body.decode()
    def fake_ffmpeg(self, passthrough=False):
        tc = self.state.transcoder
        def spawn(key):
            with tc._lock:
                w = tc.workers.get(key)
                if not w:
                    return
                if passthrough:
                    w['passthrough'] = {'bitrate': 400000, 'measured': True, 'audio': True, 'copy_audio': True}
                outdir = os.path.join(tc.hls_dir, key); os.makedirs(outdir, exist_ok=True)
                gen, first = w['generation'], 1758000000 + 2 * w['generation']
                for level in ([0] if passthrough else w['levels']):
                    pathlib.Path(outdir, 'g%d_s_%d.m3u8' % (gen, level)).write_text(
                        '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%d\n'
                        '#EXTINF:2.000000,\ng%d_%d_%d.ts\n#EXTINF:2.000000,\ng%d_%d_%d.ts\n'
                        % (first, gen, level, first, gen, level, first + 1))
                w['state'] = 'buffering'
        patch.object(tc, '_spawn', side_effect=spawn).start()
        patch.object(playermod, 'ZAP_SECONDS', 0).start()
        self.state.candidate_urls.side_effect = lambda pid, sid: [{'provider': pid, 'url': 'http://src/%s' % sid}]
        self.addCleanup(patch.stopall)
        seed_live(self.state.catalog)
        return {c['canonical']: c['id'] for c in self.state.player.index()[0]}
    def test_master_is_synthetic_and_starts_nothing(self):
        ids = self.fake_ffmpeg()
        for method in ('HEAD', 'GET'):
            status, headers, body = self.raw('/live/tv/salon2024xyz/%d.m3u8' % ids['TF1'], method)
            self.assertEqual(status, 200)
        # Mode eco : 360p et 240p seulement, adresses relatives sous l'id.
        self.assertEqual([l for l in body.splitlines() if not l.startswith('#')],
                         ['%d/2.m3u8' % ids['TF1'], '%d/3.m3u8' % ids['TF1']])
        self.assertEqual(self.raw('/live/tv/salon2024xyz/%d/2.m3u8' % ids['TF1'], 'HEAD')[0], 200)
        self.assertEqual(self.state.transcoder.workers, {})
        status, headers, _ = self.raw('/live/tv/salon2024xyz/%d.ts' % ids['TF1'])
        self.assertEqual((status, headers['Location']), (302, '/live/tv/salon2024xyz/%d.m3u8' % ids['TF1']))
        self.assertEqual(self.raw('/live/tv/nope/%d.m3u8' % ids['TF1'])[0], 401)
        # Forme courte Xtream, sans /live/ ni extension : renvoyee vers le HLS.
        status, headers, _ = self.raw('/tv/salon2024xyz/%d' % ids['TF1'])
        self.assertEqual((status, headers['Location']), (302, '/live/tv/salon2024xyz/%d.m3u8' % ids['TF1']))
        self.assertEqual(self.raw('/tv/salon2024xyz/%d.m3u8' % ids['TF1'])[0], 200)
        self.assertFalse(any('salon2024xyz' in line for line in app.ACCESS_LOG[-3:]))
        self.assertEqual(self.raw('/tv/nope/%d' % ids['TF1'])[0], 401)
        self.assertEqual(self.raw('/live/tv/salon2024xyz/12345.m3u8')[0], 404)
    def test_level_playlist_opens_once_and_points_at_tickets(self):
        ids = self.fake_ffmpeg()
        base = '/live/tv/salon2024xyz/%d' % ids['TF1']
        self.raw(base + '.m3u8')
        status, _, body = self.raw(base + '/2.m3u8')
        self.assertEqual(status, 200)
        ticket = next(iter(self.state.transcoder.tickets))
        self.assertIn('/s/%s/g0_2_1758000000.ts' % ticket, body)
        self.assertIn('#EXT-X-DISCONTINUITY-SEQUENCE:0', body)
        # Rafraichissements et changement de niveau : meme ticket, meme worker.
        self.assertEqual(self.raw(base + '/2.m3u8')[0], 200); self.assertEqual(self.raw(base + '/3.m3u8')[0], 200)
        self.assertEqual(len(self.state.transcoder.tickets), 1)
        self.assertEqual(self.state.transcoder._spawn.call_count, 1)
        ticket_row = self.state.transcoder.tickets[ticket]
        self.assertEqual((ticket_row['owner'], ticket_row['ceiling'], ticket_row['idle']), ('player:tv', 650000, 45))
        # Au-dessus du plafond du compte : refuse, sans rien ouvrir.
        self.assertEqual(self.raw(base + '/0.m3u8')[0], 404)
    def test_two_devices_never_fight_over_the_account(self):
        ids = self.fake_ffmpeg()
        tf1, m6 = '/live/tv/salon2024xyz/%d' % ids['TF1'], '/live/tv/salon2024xyz/%d' % ids['M6']
        self.raw(tf1 + '.m3u8'); self.assertEqual(self.raw(tf1 + '/2.m3u8')[0], 200)
        self.raw(m6 + '.m3u8'); self.assertEqual(self.raw(m6 + '/2.m3u8')[0], 200)
        # Le premier appareil rafraichit TF1 : il est evince, il ne reprend pas.
        for _ in range(3):
            self.assertEqual(self.raw(tf1 + '/2.m3u8')[0], 410)
            self.assertEqual(self.raw(m6 + '/2.m3u8')[0], 200)
        self.assertEqual(self.state.transcoder._spawn.call_count, 2)
        self.assertEqual(len(self.state.transcoder.workers), 1)
        # Il redemande explicitement la chaine : la c'est un vrai zap.
        self.raw(tf1 + '.m3u8'); self.assertEqual(self.raw(tf1 + '/2.m3u8')[0], 200)
        self.assertEqual(self.raw(m6 + '/2.m3u8')[0], 410)
    def test_quick_zapping_only_encodes_the_last_channel(self):
        ids = self.fake_ffmpeg()
        patch.object(playermod, 'ZAP_SECONDS', 0.3).start()
        tf1, m6 = '/live/tv/salon2024xyz/%d' % ids['TF1'], '/live/tv/salon2024xyz/%d' % ids['M6']
        self.raw(tf1 + '.m3u8')
        results = {}
        first = threading.Thread(target=lambda: results.setdefault('tf1', self.raw(tf1 + '/2.m3u8')[0])); first.start()
        time.sleep(.1); self.raw(m6 + '.m3u8'); first.join()
        self.assertEqual(results['tf1'], 410)
        self.assertEqual(self.raw(m6 + '/2.m3u8')[0], 200)
        self.assertEqual(self.state.transcoder._spawn.call_count, 1)
    def test_paused_player_resumes_after_expiry(self):
        ids = self.fake_ffmpeg()
        base = '/live/tv/salon2024xyz/%d' % ids['TF1']
        self.raw(base + '.m3u8'); self.raw(base + '/2.m3u8')
        for t in self.state.transcoder.tickets.values():
            t['last'] -= 60
        # Plus aucune lecture sur le compte : la reprise n'evince personne.
        self.assertEqual(self.raw(base + '/2.m3u8')[0], 200)
    def test_remuxed_channel_serves_its_single_variant(self):
        ids = self.fake_ffmpeg(passthrough=True)
        base = '/live/tv/salon2024xyz/%d' % ids['TF1']
        self.raw(base + '.m3u8')
        status, _, body = self.raw(base + '/3.m3u8')
        self.assertEqual(status, 200); self.assertIn('g0_0_1758000000.ts', body)
    def test_new_generation_is_flagged_as_a_discontinuity(self):
        ids = self.fake_ffmpeg()
        base = '/live/tv/salon2024xyz/%d' % ids['TF1']
        self.raw(base + '.m3u8'); self.raw(base + '/2.m3u8')
        tc = self.state.transcoder
        with tc._lock:
            w = next(iter(tc.workers.values())); w['generation'] = 1
        tc._spawn.side_effect(w['key'])
        status, _, body = self.raw(base + '/2.m3u8')
        uris = [l.rsplit('/', 1)[-1] for l in body.splitlines() if not l.startswith('#')]
        # La fin de l'ancienne generation, une discontinuite, puis la nouvelle,
        # numerotees a la suite : le lecteur franchit la bascule sans se figer.
        self.assertEqual(uris, ['g0_2_1758000000.ts', 'g0_2_1758000001.ts', 'g1_2_1758000002.ts', 'g1_2_1758000003.ts'])
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:1758000000', body)
        self.assertIn('#EXT-X-DISCONTINUITY-SEQUENCE:0', body)
        self.assertEqual(body.splitlines()[body.splitlines().index('#EXT-X-DISCONTINUITY') + 2].rsplit('/', 1)[-1], 'g1_2_1758000002.ts')
        # Une fois la nouvelle generation assez longue, l'ancienne sort de la fenetre.
        with patch.object(app, 'LIVE_OVERLAP_SEGMENTS', 2):
            body = self.raw(base + '/2.m3u8')[2]
        self.assertIn('#EXT-X-DISCONTINUITY-SEQUENCE:1', body); self.assertNotIn('g0_2_', body)
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:1758000002', body)
    def test_full_server_answers_503_to_players(self):
        ids = self.fake_ffmpeg()
        with patch.dict(self.state.transcoder.cfg, {'max_concurrent_streams': 1}):
            self.state.transcoder.open('web-session', 'EN|BBC ONE', [{'provider': 'p', 'url': 'http://x'}])
            base = '/live/tv/salon2024xyz/%d' % ids['TF1']
            self.raw(base + '.m3u8')
            status, headers, _ = self.raw(base + '/2.m3u8')
        self.assertEqual((status, headers['Retry-After']), (503, '10'))
    def test_instance_cap_bounds_web_and_players(self):
        ids = self.fake_ffmpeg()
        self.state.cfg['players'] = [dict(username='tv', password='salon2024xyz', mode='sport')]
        self.state.sessions.cfg = self.state.cfg
        self.state.cfg['max_mode'] = 'balanced'
        body = self.raw('/live/tv/salon2024xyz/%d.m3u8' % ids['TF1'])[2]
        self.assertNotIn('/0.m3u8', body); self.assertIn('/1.m3u8', body)
        web = json.loads(self.request('/api/play', {'lang': 'FR', 'canonical': 'TF1', 'mode': 'sport'}, self.login()).read())
        self.assertEqual(web['ceiling'], 1150000)
        # Un mode plus econome que la borne reste respecte.
        eco = json.loads(self.request('/api/play', {'lang': 'FR', 'canonical': 'M6', 'mode': 'eco'}, self.login()).read())
        self.assertEqual(eco['ceiling'], 650000)
    def test_player_and_web_share_the_same_encoder(self):
        ids = self.fake_ffmpeg()
        cookie = self.login()
        web = json.loads(self.request('/api/play', {'lang': 'FR', 'canonical': 'TF1', 'mode': 'eco'}, cookie).read())
        base = '/live/tv/salon2024xyz/%d' % ids['TF1']
        self.raw(base + '.m3u8'); self.assertEqual(self.raw(base + '/2.m3u8')[0], 200)
        self.assertEqual(len(self.state.transcoder.workers), 1)
        self.assertIn(web['ticket'], self.state.transcoder.tickets)
    def test_player_credentials_in_settings(self):
        viewer, admin = self.login('test-viewer'), self.login()
        seen = json.loads(self.request('/api/player-credentials', cookie=viewer, headers={'Host': 'tv.example'}).read())['players'][0]
        self.assertEqual(seen['username'], 'tv'); self.assertEqual(seen['mode'], 'eco')
        self.assertEqual(seen['m3u_url'], 'http://tv.example/get.php?username=tv&password=salon2024xyz&type=m3u_plus&output=m3u8')
        self.assertEqual(self.request('/api/player-credentials', {'regenerate': True}, viewer).status, 403)
        with patch.object(app.cfgmod, 'save') as saved:
            self.assertEqual(self.request('/api/player-credentials', {'mode': 'ultra'}, admin).status, 400)
            fresh = json.loads(self.request('/api/player-credentials', {'regenerate': True, 'mode': 'sport'}, admin).read())
        self.assertNotEqual(fresh['password'], 'salon2024xyz'); self.assertEqual(fresh['mode'], 'sport')
        saved.assert_called_once()
        self.assertEqual(self.request('/get.php?username=tv&password=salon2024xyz').status, 401)
        self.assertEqual(self.request('/get.php?username=tv&password=' + fresh['password']).status, 200)

if __name__ == '__main__': unittest.main()
