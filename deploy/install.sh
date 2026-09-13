#!/usr/bin/env bash
# Installe Streamly sur une machine Debian/Ubuntu.
#   sudo ./deploy/install.sh [utilisateur]
set -euo pipefail

RUN_USER="${1:-${SUDO_USER:-$USER}}"
DEST=/opt/streamly
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "A lancer avec sudo." >&2
  exit 1
fi

echo "==> Dependances"
apt-get update -qq
apt-get install -y -qq ffmpeg python3

echo "==> Copie vers $DEST"
mkdir -p "$DEST"
# On ne recopie jamais config.json : il contient les identifiants et reste
# la propriete de la machine cible.
rsync -a --exclude 'config.json' --exclude 'data' --exclude 'hls' \
      --exclude 'logs' --exclude '.git' "$SRC/" "$DEST/"
mkdir -p "$DEST/server/data" "$DEST/server/hls" "$DEST/server/logs"
chown -R "$RUN_USER:$RUN_USER" "$DEST"

echo "==> Service systemd"
sed "s/User=%i/User=$RUN_USER/" "$SRC/deploy/streamly.service" \
  > /etc/systemd/system/streamly.service
systemctl daemon-reload
systemctl enable --now streamly

sleep 2
systemctl is-active streamly && echo "==> Service actif"

PORT=$(python3 -c "import json;print(json.load(open('$DEST/server/config.json')).get('listen_port',8088))" 2>/dev/null || echo 8088)
TOKEN=$(python3 -c "import json;print(json.load(open('$DEST/server/config.json')).get('token',''))" 2>/dev/null || true)

cat <<EOF

Installation terminee.

  Interface : http://<adresse-du-serveur>:$PORT/
  Jeton     : $TOKEN

Ajoutez vos providers depuis l'onglet Reglages, puis lancez une synchronisation.

Important : le service ecoute en HTTP simple. S'il est expose a Internet,
placez-le derriere un reverse proxy avec TLS (Caddy ou nginx).
EOF
