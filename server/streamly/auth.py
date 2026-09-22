"""Expiring device sessions. Admin credentials never appear in media URLs."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie

# Cout volontairement eleve : une empreinte volee doit rester couteuse a casser.
PBKDF2_ROUNDS = 200000


def hash_password(password, salt=None):
    """Retourne (sel, empreinte). Le mot de passe n'est jamais stocke en clair."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 salt.encode('utf-8'), PBKDF2_ROUNDS)
    return salt, digest.hex()


def verify_password(password, salt, expected):
    if not (password and salt and expected):
        return False
    _, computed = hash_password(password, salt)
    return hmac.compare_digest(computed, expected)


def _digest(sid):
    return hashlib.sha256(str(sid).encode('utf-8')).hexdigest()


class Sessions:
    """Sessions web, conservees sur disque quand un chemin est donne.

    En memoire seulement, chaque redemarrage du service (mise a jour,
    deploiement) deconnectait tous les appareils. Le fichier ne contient que
    l'empreinte de chaque identifiant : sa lecture ne suffit pas a se faire
    passer pour un appareil connecte.
    """
    LIMIT = 100

    def __init__(self, cfg, path=None):
        self.cfg, self.items, self.attempts = cfg, {}, {}
        self.path = path
        self.lock = threading.RLock()
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding='utf-8') as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            return
        now = time.time()
        if isinstance(stored, dict):
            self.items = {k: v for k, v in stored.items()
                          if isinstance(v, dict) and v.get('expires', 0) > now}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + '.tmp'
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(self.items, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def login(self, token, address, username=None, password=None):
        now = time.time()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 300}
            count, since = self.attempts.get(address, (0, now))
            if count >= 10:
                raise ValueError('Trop de tentatives. Réessayez dans cinq minutes.')
            role, account = None, ''
            if username:
                # Comparaison a temps constant sur le nom aussi : sinon la
                # duree de reponse revele quels comptes existent.
                for u in self.cfg.get('users', []):
                    if (secrets.compare_digest(str(u.get('username', '')), str(username))
                            and verify_password(password, u.get('salt'), u.get('password_hash'))):
                        role = u.get('role', 'admin')
                        account = str(u.get('username'))
                        break
                # Fallback : premier demarrage ou instance sans comptes 'users' definis
                if not role and not self.cfg.get('users', []):
                    candidate = password or username
                    if candidate and secrets.compare_digest(str(candidate), str(self.cfg.get('token', ''))):
                        role = 'admin'
                    elif candidate and secrets.compare_digest(str(candidate), str(self.cfg.get('viewer_token', ''))):
                        role = 'viewer'
            elif token and secrets.compare_digest(str(token), str(self.cfg.get('token', ''))):
                role = 'admin'
            elif token and secrets.compare_digest(str(token), str(self.cfg.get('viewer_token', ''))):
                role = 'viewer'
            elif password and not self.cfg.get('users', []):
                if secrets.compare_digest(str(password), str(self.cfg.get('token', ''))):
                    role = 'admin'
                elif secrets.compare_digest(str(password), str(self.cfg.get('viewer_token', ''))):
                    role = 'viewer'

            if not role:
                self.attempts[address] = (count + 1, since)
                raise ValueError('Identifiants refusés.' if username else 'Jeton refusé.')
            self.attempts.pop(address, None)
            self.items = {k: v for k, v in self.items.items() if v['expires'] > now}
            # Conservees d'un redemarrage a l'autre, les sessions s'accumulent :
            # on libere la plus ancienne plutot que de refuser la connexion.
            while len(self.items) >= self.LIMIT:
                del self.items[min(self.items, key=lambda k: self.items[k]['expires'])]
            sid = secrets.token_urlsafe(32)
            self.items[_digest(sid)] = {'role': role, 'account': account, 'expires': now + 7 * 86400}
            self._save()
            return sid, role

    def blocked(self, address):
        """Vrai apres dix echecs en cinq minutes depuis cette adresse."""
        now = time.time()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 300}
            return self.attempts.get(address, (0, now))[0] >= 10

    def failed(self, address):
        now = time.time()
        with self.lock:
            count, since = self.attempts.get(address, (0, now))
            self.attempts[address] = (count + 1, since)

    def player(self, username, password, address):
        """Compte d'un lecteur externe, ou None.

        TiviMate ou VLC n'ont pas de cookie : ils renvoient leurs
        identifiants a chaque requete, segments compris. Pas de PBKDF2 ici,
        il couterait 200 000 iterations toutes les deux secondes ; une
        comparaison a temps constant et la meme limite de tentatives que la
        connexion web.
        """
        now = time.time()
        given = (str(username or '').encode('utf-8'), str(password or '').encode('utf-8'))
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 300}
            count, since = self.attempts.get(address, (0, now))
            if count >= 10:
                return None
            found = None
            for p in self.cfg.get('players', []):
                name_ok = hmac.compare_digest(str(p.get('username') or '').encode('utf-8'), given[0])
                pass_ok = hmac.compare_digest(str(p.get('password') or '').encode('utf-8'), given[1])
                if name_ok and pass_ok and p.get('password') and found is None:
                    found = p
            if found is None:
                self.attempts[address] = (count + 1, since)
                return None
            self.attempts.pop(address, None)
            return dict(found)

    def get(self, cookie):
        try:
            parsed = SimpleCookie(cookie or '')
            sid = parsed['streamly_session'].value
        except (KeyError, ValueError):
            return None
        key = _digest(sid)
        with self.lock:
            session = self.items.get(key)
            if not session or session['expires'] < time.time():
                if self.items.pop(key, None):
                    self._save()
                return None
            account = session.get('account') or ''
            if account:
                # Compte retire ou role change dans config.json : la session
                # suit, sans attendre ses sept jours.
                user = next((u for u in self.cfg.get('users', [])
                             if str(u.get('username', '')) == account), None)
                if not user:
                    self.items.pop(key, None)
                    self._save()
                    return None
                session = dict(session, role=user.get('role', 'admin'))
            return dict(session, id=sid, account=account)

    def logout(self, sid):
        with self.lock:
            if self.items.pop(_digest(sid), None):
                self._save()
