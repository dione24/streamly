"""Real FFmpeg smoke test using synthetic video; no provider or credentials."""
import functools
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from streamly.transcoder import Transcoder, probe
from streamly.vod import Movies
from streamly.catalog import Catalog

with tempfile.TemporaryDirectory() as root:
    root = pathlib.Path(root)
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=50','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
        '-t','8','-c:v','libx264','-preset','ultrafast','-c:a','aac','-f','mpegts',str(root/'source.ts')], check=True, timeout=30)
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    with patch('socket.getfqdn', return_value='localhost'):
        http = ThreadingHTTPServer(('127.0.0.1',0), functools.partial(Quiet,directory=str(root)))
    threading.Thread(target=http.serve_forever,daemon=True).start()
    cfg = json.loads((pathlib.Path(__file__).resolve().parents[1]/'server/config.example.json').read_text())
    cfg['max_concurrent_streams'] = 2
    t = Transcoder(cfg, str(root/'hls'), str(root/'logs'), monitor=False)
    source = 'http://127.0.0.1:%s/source.ts' % http.server_port
    media = probe(source, 'StreamlyTest'); assert media['fps'] == 50, media
    out = root/'live'; out.mkdir()
    r = subprocess.run(t._command(source, media, 0), cwd=out, capture_output=True, timeout=45)
    assert r.returncode == 0, r.stderr.decode()[-2000:]
    durations = []
    for i in range(4):
        text = (out/('g0_s_%d.m3u8' % i)).read_text()
        values = [float(x) for x in re.findall(r'#EXTINF:([\d.]+)',text)]
        assert values and all(abs(n-2)<.25 for n in values[:-1]), values
        durations.append(values)
    # FFmpeg 4.4 (Ubuntu 22.04) et FFmpeg 9 ne découpent pas le dernier
    # segment à la milliseconde près : on compare à tolérance, pas en strict.
    assert len({len(d) for d in durations}) == 1, [len(d) for d in durations]
    ref = durations[0]
    for other in durations[1:]:
        assert len(other) == len(ref), (ref, other)
        assert all(abs(a-b) < 0.15 for a, b in zip(ref, other)), (ref, other)
    catalog = Catalog(str(root/'catalog.db'))
    movies = Movies(cfg,str(root/'movies'),catalog,t)
    job = movies.start({'provider_id':'provider-1','stream_id':1,'title':'Synthetic fixture'},source,480)
    deadline = time.time()+60
    while movies.jobs[job['id']]['state'] == 'preparing' and time.time()<deadline: time.sleep(.2)
    j = movies.jobs[job['id']]
    assert j['state']=='ready',j
    assert j['size_bytes']>0 and len(j['qualities']) == 3,j
    download = root/'movies'/j['id']/j['download']
    video = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(download)]))
    assert next(s for s in video['streams'] if s['codec_type']=='video')['codec_name']=='h264'
    print(json.dumps({'live_50fps_segments':durations,'prepared_qualities':j['qualities'],'download_bytes':j['size_bytes'],'provider_reservation_released':not t.reservations}))
    assert not t.reservations
    t.close();http.shutdown();http.server_close()
