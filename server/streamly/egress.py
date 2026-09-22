"""Acces HTTP borne aux sources non fiables, sans seconde resolution DNS.

FFmpeg ne voit que des URL locales signees. Chaque redirection, playlist,
segment et cle HLS repasse par la meme validation avant connexion.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def resolve(url, allow_private=False):
    if not isinstance(url, str) or not 0 < len(url) <= 8192 or any(ord(c) < 32 for c in url):
        raise ValueError('adresse invalide')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('adresse HTTP requise')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not infos or (not allow_private and any(
            not ipaddress.ip_address(i[4][0].split('%')[0]).is_global for i in infos)):
        raise ValueError('adresse privee ou locale refusee')
    return parsed, infos


@contextmanager
def open_public(url, user_agent, allow_private=False, byte_range=None, timeout=12):
    """Connexion a l'IP validee, Host/SNI et verification TLS conserves."""
    conn = response = None
    try:
        for hop in range(6):
            parsed, infos = resolve(url, allow_private)
            sock = None
            for family, kind, proto, _, address in infos:
                candidate = socket.socket(family, kind, proto)
                candidate.settimeout(timeout)
                try:
                    candidate.connect(address)
                    sock = candidate
                    break
                except OSError:
                    candidate.close()
            if sock is None:
                raise OSError('source inaccessible')
            try:
                if parsed.scheme == 'https':
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
                conn.sock = sock
                headers = {'User-Agent': user_agent, 'Accept-Encoding': 'identity', 'Connection': 'close'}
                if byte_range:
                    headers['Range'] = byte_range
                if parsed.username is not None:
                    credentials = urllib.parse.unquote(parsed.username) + ':' + urllib.parse.unquote(parsed.password or '')
                    headers['Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
                conn.request('GET', urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, '')), headers=headers)
                response = conn.getresponse()
            except Exception:
                sock.close()
                raise
            if response.status not in (301, 302, 303, 307, 308):
                yield response, url
                return
            location = response.getheader('Location')
            response.close()
            conn.close()
            if not location:
                raise ValueError('redirection sans destination')
            url = urllib.parse.urljoin(url, location)
        raise ValueError('trop de redirections')
    finally:
        if response:
            response.close()
        if conn:
            conn.close()


class RelayGateway:
    def __init__(self, user_agent, allow_private=False):
        self.user_agent, self.allow_private = user_agent, allow_private
        self.secret = secrets.token_bytes(32)
        self.grants, self.lock = set(), threading.Lock()
        self.slots = threading.BoundedSemaphore(16)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.gateway = self
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def grant(self, key, url):
        with self.lock:
            self.grants.add(key)
        return self.url(key, url)

    def revoke(self, key):
        with self.lock:
            self.grants.discard(key)

    def url(self, key, source):
        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or len(source) > 8192:
            raise ValueError('ressource HLS non HTTP')
        payload = base64.urlsafe_b64encode(source.encode()).decode().rstrip('=')
        signature = hmac.new(self.secret, (key + '/' + payload).encode(), hashlib.sha256).hexdigest()
        # Extension conservee pour la validation des segments par FFmpeg.
        ext = parsed.path.rsplit('.', 1)[-1].lower()
        ext = ext if re.fullmatch('[a-z0-9]{1,8}', ext) else 'ts'
        return '%s/%s/%s/%s.%s' % (self.base, key, signature, payload, ext)

    def source(self, path):
        _, key, signature, filename = path.split('/')
        payload = filename.rsplit('.', 1)[0]
        expected = hmac.new(self.secret, (key + '/' + payload).encode(), hashlib.sha256).hexdigest()
        with self.lock:
            if key not in self.grants or not hmac.compare_digest(signature, expected):
                raise ValueError('acces expire')
        return key, base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode()

    def playlist(self, key, body, base):
        def local(uri):
            return self.url(key, urllib.parse.urljoin(base, uri))
        lines = []
        for line in body.decode('utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                line = local(line)
            elif line.startswith('#'):
                # FFmpeg tolere aussi les URI sans guillemets : les laisser
                # telles quelles contournerait la passerelle (cle, EXT-X-MAP,
                # audio alternatif...). Les barres obliques inverses rendent
                # l'interpretation des guillemets ambigue selon le parseur.
                if '\\' in line:
                    raise ValueError('echappement HLS non pris en charge')
                line = re.sub(r'\bURI\s*=\s*(?:"([^"]*)"|([^,]*))',
                              lambda m: 'URI="' + local(m[1] if m[1] is not None else m[2].strip()) + '"', line)
            lines.append(line)
        return ('\n'.join(lines) + '\n').encode()

    def close(self):
        with self.lock:
            self.grants.clear()
        self.server.shutdown()
        self.server.server_close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # L'adresse signee contient l'URL fournisseur encodee.

    def do_GET(self):
        gateway = self.server.gateway
        if not gateway.slots.acquire(blocking=False):
            self.send_error(503)
            return
        sent = False
        try:
            key, source = gateway.source(self.path)
            with open_public(source, gateway.user_agent, gateway.allow_private,
                             self.headers.get('Range')) as (response, final_url):
                if response.status not in (200, 206):
                    self.send_error(502, 'Source indisponible')
                    return
                prefix = response.read(7)
                is_hls = prefix.startswith(b'#EXTM3U') or prefix.startswith(b'\xef\xbb\xbf#EXT')
                if is_hls:
                    body = prefix + response.read(2 * 1024 * 1024)
                    if len(body) >= 2 * 1024 * 1024:
                        raise ValueError('playlist trop grande')
                    body = gateway.playlist(key, body, final_url)
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl' if is_hls else 'application/octet-stream')
                if is_hls:
                    self.send_header('Content-Length', str(len(body)))
                else:
                    for name in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
                        if response.getheader(name):
                            self.send_header(name, response.getheader(name))
                self.end_headers()
                sent = True
                if is_hls:
                    self.wfile.write(body)
                else:
                    self.wfile.write(prefix)
                    while True:
                        with gateway.lock:
                            if key not in gateway.grants:
                                break
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            if not sent:
                self.send_error(502, 'Source refusee ou inaccessible')
        finally:
            gateway.slots.release()
