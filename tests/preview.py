"""Local preview populated with fictional catalogue entries, no upstream access."""
import json,pathlib,sys,tempfile
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from streamly import app,config
root=pathlib.Path(tempfile.mkdtemp(prefix='streamly-preview-'))
config.CONFIG_PATH=str(root/'config.json'); config.DATA_DIR=str(root/'data');config.HLS_DIR=str(root/'hls');config.LOG_DIR=str(root/'logs')
cfg=json.loads(pathlib.Path(config.EXAMPLE_PATH).read_text());cfg.update(token='preview-only',viewer_token='viewer-only',listen_host='127.0.0.1',listen_port=8099,providers=[])
pathlib.Path(config.CONFIG_PATH).write_text(json.dumps(cfg))
app.STATE=app.State()
rows=[]
for i in range(65):
 name=['Sport • Grand Prix','Le journal','Culture & découvertes','Cinéma du soir','Documentaires','Musique live'][i%6] + (' '+str(i+1) if i>5 else '')
 rows.append(('demo',i,name,'FR',name.upper(),'HD',720,'sport','Sport & découvertes','','',0))
with app.STATE.catalog._db:
 app.STATE.catalog._db.executemany('INSERT INTO channels VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
 app.STATE.catalog._db.execute("INSERT INTO vod(provider_id,stream_id,name,title,lang,container,duration,bitrate,plot) VALUES ('demo',1,'Horizon','Horizon','FR','mp4','01:30:00',3500,'Un film de démonstration pour vérifier la présentation et le choix de qualité.')")
with patch('socket.getfqdn',return_value='localhost'):
 server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
print('Preview http://127.0.0.1:%d' % server.server_port,flush=True)
server.serve_forever()
