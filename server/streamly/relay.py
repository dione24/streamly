"""Relais pour l'application : appareils associes et sources qu'elle envoie.

Dans l'app, la playlist vit sur l'appareil, comme dans TiviMate. Quand
l'economie de donnees est active, l'app confie au moteur l'adresse du flux a
compresser. Un relais ouvert serait une faille : il faut un appareil associe,
et la source ne peut pas viser le reseau interne du serveur.
"""
import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time
import urllib.parse

from .config import PLAYER_ALPHABET

CODE_SECONDS = 600
MAX_DEVICES = 20


def check_source(url, allow_private=False):
    """Valide l'adresse d'un flux et retourne son hote, ou leve ValueError."""
    if not isinstance(url, str) or not 0 < len(url) <= 2048:
        raise ValueError('source invalide')
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('la source doit etre une adresse http ou https')
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, UnicodeError):
        raise ValueError('hote de la source introuvable')
    for info in infos:
        # Toutes les adresses comptent : un nom qui repond a la fois en public
        # et en prive servirait de rebond vers le reseau du serveur.
        if not allow_private and not ipaddress.ip_address(info[4][0].split('%')[0]).is_global:
            raise ValueError('source refusee : adresse privee ou locale')
    return parsed.hostname.lower()


def _digest(token):
    # Jeton aleatoire de 192 bits : une empreinte simple suffit, et elle se
    # verifie a chaque requete sans le cout d'un PBKDF2.
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class Devices:
    def __init__(self, cfg, save):
        self.cfg, self.save = cfg, save
        self.lock = threading.Lock()
        self.codes = {}

    def new_code(self):
        """Code court a saisir dans l'app, valable dix minutes, usage unique."""
        with self.lock:
            now = time.time()
            self.codes = {c: t for c, t in self.codes.items() if t > now}
            code = ''.join(secrets.choice(PLAYER_ALPHABET) for _ in range(8))
            self.codes[code] = now + CODE_SECONDS
            return code, CODE_SECONDS

    def pair(self, code, name):
        """Echange un code contre un jeton d'appareil, ou retourne None."""
        given = str(code or '').strip().lower().replace('-', '').replace(' ', '')
        with self.lock:
            now = time.time()
            match = next((c for c, t in self.codes.items()
                          if t > now and hmac.compare_digest(c.encode(), given.encode())), None)
            if not match:
                return None
            del self.codes[match]
            devices = [dict(d) for d in self.cfg.get('devices', [])]
            if len(devices) >= MAX_DEVICES:
                raise ValueError('Trop d\'appareils associes : retirez-en un dans Reglages.')
            token = secrets.token_urlsafe(24)
            device = {'id': secrets.token_hex(6), 'name': str(name or 'Appareil')[:60],
                      'token_sha256': _digest(token), 'created': int(now)}
            devices.append(device)
            self.cfg['devices'] = devices
            self.save({'devices': devices})
            return {'id': device['id'], 'name': device['name'], 'token': token}

    def find(self, token):
        if not token:
            return None
        digest = _digest(str(token))
        found = None
        for d in self.cfg.get('devices', []):
            if hmac.compare_digest(str(d.get('token_sha256') or ''), digest) and found is None:
                found = d
        return found

    def list(self):
        return [{k: d.get(k) for k in ('id', 'name', 'created')} for d in self.cfg.get('devices', [])]

    def remove(self, device_id):
        with self.lock:
            devices = [d for d in self.cfg.get('devices', []) if d.get('id') != device_id]
            self.cfg['devices'] = devices
            self.save({'devices': devices})
