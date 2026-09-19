"""Relais et cache des logos de chaines et des affiches.

Ils viennent de serveurs tiers, lents, et souvent en http : une page servie en
https ne les charge pas. Le serveur les recupere une fois et les garde.

Aller chercher une adresse fournie de l'exterieur est un risque : seules les
adresses presentes dans le catalogue sont servies, elles doivent etre publiques
(redirections comprises), et seul un vrai fichier image, de taille bornee, est
accepte.
"""
import hashlib
import os
import threading
import time
import urllib.request

from .relay import check_source

MAX_BYTES = 1500000
CACHE_BYTES = 500 * 1000000
RETRY_SECONDS = 86400
KINDS = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def _kind(data):
    for magic, ctype in KINDS:
        if data.startswith(magic):
            return ctype
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    # Pas de SVG : ouvert directement, il executerait du script sur notre origine.
    return None


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_source(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Logos:
    def __init__(self, root, known, user_agent='VLC/3.0.20', allow_private=False):
        """known(url) -> bool : l'adresse figure-t-elle dans le catalogue ?"""
        self.root, self.known, self.user_agent, self.allow_private = root, known, user_agent, allow_private
        os.makedirs(root, exist_ok=True)
        self._slots = threading.Semaphore(8)
        self._lock = threading.Lock()
        self._size = sum(e.stat().st_size for e in os.scandir(root) if e.is_file())
        self._opener = urllib.request.build_opener(_CheckedRedirects(allow_private))

    def get(self, url):
        """(octets, type) d'un logo, ou None."""
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')) or len(url) > 2048:
            return None
        path = os.path.join(self.root, hashlib.sha1(url.encode('utf-8')).hexdigest())
        cached = self._read(path)
        if cached is not False:
            return cached
        if not self.known(url):
            return None
        with self._slots:
            cached = self._read(path)          # un autre fil l'a peut-etre deja rapporte
            if cached is not False:
                return cached
            data = self._fetch(url)
        ctype = _kind(data) if data else None
        body = data if ctype else b''
        with self._lock:
            if self._size + len(body) <= CACHE_BYTES:
                tmp = path + '.tmp%d' % threading.get_ident()
                with open(tmp, 'wb') as fh:
                    fh.write(body)
                os.replace(tmp, path)
                self._size += len(body)
        return (body, ctype) if ctype else None

    def _read(self, path):
        """False = pas en cache ; None = echec recent memorise ; sinon (octets, type)."""
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError:
            return False
        if data:
            return data, _kind(data)
        # Fichier vide : echec memorise, pour ne pas solliciter a chaque
        # affichage un serveur qui ne repond pas.
        if time.time() - os.path.getmtime(path) < RETRY_SECONDS:
            return None
        return False

    def _fetch(self, url):
        try:
            check_source(url, self.allow_private)
            request = urllib.request.Request(url, headers={'User-Agent': self.user_agent})
            with self._opener.open(request, timeout=6) as response:
                data = response.read(MAX_BYTES + 1)
        except Exception:
            return None
        return data if len(data) <= MAX_BYTES else None
