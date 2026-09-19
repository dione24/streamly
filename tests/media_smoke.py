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
    # --- Remux : la source synthetique est deja du H264/AAC 4:2:0.
    # Sans debit connu on encode, meme sans plafond : « Sport » promet du 720p
    # compresse, pas la source brute. Le remux ne vaut que pour une source
    # mesuree plus legere que le barreau le plus haut.
    blind = dict(media, bitrate=0, video_bitrate=0)
    assert t.passthrough_plan(blind) is None, 'une source non mesuree ne doit pas etre remuxee'
    plan = t.passthrough_plan(dict(media, bitrate=300000, video_bitrate=0))
    assert plan and plan['copy_audio'], plan
    remux = root/'copy'; remux.mkdir()
    r = subprocess.run(t._command(source, media, 0, passthrough=plan), cwd=remux, capture_output=True, timeout=45)
    assert r.returncode == 0, r.stderr.decode()[-2000:]
    assert not (remux/'g0_s_1.m3u8').exists(), 'le remux ne doit produire qu une variante'
    copy_durations = [float(x) for x in re.findall(r'#EXTINF:([\d.]+)', (remux/'g0_s_0.m3u8').read_text())]
    assert copy_durations, 'aucun segment remuxe'
    segments = sorted(remux.glob('g0_0_*.ts'))
    assert segments, 'aucun segment remuxe sur disque'
    copied = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(segments[0])]))
    cv = next(s for s in copied['streams'] if s['codec_type']=='video')
    # Definition source conservee : aucun barreau de l echelle n a ete applique.
    assert cv['codec_name']=='h264' and (cv['width'],cv['height'])==(320,180), cv
    assert any(s['codec_type']=='audio' and s['codec_name']=='aac' for s in copied['streams']), copied

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
    print(json.dumps({'live_50fps_segments':durations,'remux_segments':copy_durations,'prepared_qualities':j['qualities'],'download_bytes':j['size_bytes'],'provider_reservation_released':not t.reservations}))
    assert not t.reservations
    t.close();http.shutdown();http.server_close()
