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
from streamly.transcoder import Transcoder, CapacityError
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
