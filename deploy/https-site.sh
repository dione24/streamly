#!/usr/bin/env bash
# Place une instance Streamly derriere Apache en HTTPS (certificat Let's Encrypt).
#   sudo ./deploy/https-site.sh <nom-de-domaine> <port-de-l-instance>
# N'ajoute qu'un site : les sites existants ne sont pas modifies, et Apache
# n'est recharge qu'apres une verification de syntaxe reussie.
set -euo pipefail

HOST="${1:?nom de domaine}"
PORT="${2:?port de l instance}"
CONF="/etc/apache2/sites-available/streamly-$PORT.conf"

if [[ $EUID -ne 0 ]]; then
  echo "A lancer avec sudo." >&2
  exit 1
fi

a2enmod -q proxy proxy_http headers >/dev/null

cat > "$CONF" <<EOF
<VirtualHost *:80>
    ServerName $HOST
    ProxyPreserveHost On
    ProxyTimeout 60
    RequestHeader set X-Forwarded-Proto expr=%{REQUEST_SCHEME}
    ProxyPass / http://127.0.0.1:$PORT/ connectiontimeout=5 timeout=60
    ProxyPassReverse / http://127.0.0.1:$PORT/
    # Ni chemin ni query dans le journal : ils portent tickets et mots de passe.
    CustomLog \${APACHE_LOG_DIR}/streamly-$PORT-access.log "%h %t %m %>s %b"
    ErrorLog \${APACHE_LOG_DIR}/streamly-$PORT-error.log
</VirtualHost>
EOF
a2ensite -q "streamly-$PORT" >/dev/null

if ! apachectl configtest 2>/dev/null; then
  echo "Syntaxe Apache refusee : site retire, rien n'a ete recharge." >&2
  a2dissite -q "streamly-$PORT" >/dev/null
  exit 1
fi
systemctl reload apache2

# certbot cree le site :443 a partir de celui-ci et redirige le port 80.
certbot --apache -d "$HOST" --non-interactive --agree-tos --redirect --keep-until-expiring
echo "==> https://$HOST"
