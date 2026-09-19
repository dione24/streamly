#!/usr/bin/env bash
# Cree ou met a jour une instance Streamly isolee sur la meme machine :
# un utilisateur = une instance = son abonnement, son catalogue, son port.
# Deux abonnements dans une meme instance se serviraient de secours l'un a
# l'autre ; des instances separees ne partagent rien.
#   sudo ./deploy/instance.sh <nom> <port> [utilisateur]
# Relancer la commande met a jour le code de l'instance sans toucher a sa
# configuration ni a son catalogue.
set -euo pipefail

NAME="${1:?nom de l instance (lettres, chiffres, tirets)}"
PORT="${2:?port d ecoute}"
RUN_USER="${3:-${SUDO_USER:-$USER}}"
SRC=/opt/streamly
DEST="/opt/streamly-$NAME"

if [[ $EUID -ne 0 ]]; then
  echo "A lancer avec sudo." >&2
  exit 1
fi
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,30}$ && "$PORT" =~ ^[0-9]{2,5}$ ]]; then
  echo "Nom ou port invalide." >&2
  exit 1
fi

echo "==> Code vers $DEST"
mkdir -p "$DEST/server" "$DEST/web"
rsync -a --delete --exclude '__pycache__' "$SRC/server/streamly/" "$DEST/server/streamly/"
rsync -a "$SRC/server/run.py" "$SRC/server/config.example.json" "$DEST/server/"
rsync -a --delete "$SRC/web/" "$DEST/web/"
mkdir -p "$DEST/server/data" "$DEST/server/hls" "$DEST/server/logs"

if [[ ! -f "$DEST/server/config.json" ]]; then
  echo "==> Configuration initiale"
  python3 - "$DEST/server" "$PORT" <<'PY'
import json, os, secrets, sys
root, port = sys.argv[1], int(sys.argv[2])
cfg = json.load(open(os.path.join(root, 'config.example.json'), encoding='utf-8'))
cfg = {k: v for k, v in cfg.items() if not k.startswith('_comment')}
# Aucun abonnement au depart : son proprietaire l'ajoute depuis Reglages.
cfg.update(listen_host='0.0.0.0', listen_port=port, providers=[], users=[],
           token=secrets.token_urlsafe(24), max_concurrent_streams=1, max_mode='balanced')
path = os.path.join(root, 'config.json')
with open(path, 'w', encoding='utf-8') as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False)
os.chmod(path, 0o600)
PY
fi
chown -R "$RUN_USER:$RUN_USER" "$DEST"

echo "==> Services systemd"
# Toutes les instances partagent une tranche de CPU bornee : meme si chacune
# encode en meme temps, le reste de la machine garde de la marge.
cat > /etc/systemd/system/streamly-instances.slice <<'UNIT'
[Unit]
Description=Instances Streamly (CPU partage et borne)

[Slice]
CPUQuota=300%
UNIT
cat > /etc/systemd/system/streamly@.service <<UNIT
[Unit]
Description=Streamly - instance %i
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Slice=streamly-instances.slice
WorkingDirectory=/opt/streamly-%i/server
ExecStart=/usr/bin/python3 /opt/streamly-%i/server/run.py
Restart=always
RestartSec=3
CPUQuota=150%
MemoryMax=1G

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "streamly@$NAME" >/dev/null 2>&1
systemctl restart "streamly@$NAME"

sleep 3
systemctl is-active "streamly@$NAME" >/dev/null && echo "==> Instance $NAME active sur le port $PORT"
python3 - "$DEST/server/config.json" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding='utf-8'))
player = (cfg.get('players') or [{}])[0]
print('    jeton administrateur : %s' % cfg.get('token'))
print('    compte lecteur       : %s / %s' % (player.get('username'), player.get('password')))
PY
