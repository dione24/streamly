"""Expiring device sessions. Admin credentials never appear in media URLs."""
import secrets
import threading
import time
from http.cookies import SimpleCookie


class Sessions:
    def __init__(self, cfg):
        self.cfg, self.items, self.attempts = cfg, {}, {}
        self.lock = threading.RLock()

    def login(self, token, address):
        now = time.time()
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 300}
            count, since = self.attempts.get(address, (0, now))
            if count >= 10:
                raise ValueError('Trop de tentatives. Réessayez dans cinq minutes.')
            role = None
            if token and secrets.compare_digest(str(token), str(self.cfg.get('token', ''))):
                role = 'admin'
            elif token and secrets.compare_digest(str(token), str(self.cfg.get('viewer_token', ''))):
                role = 'viewer'
            if not role:
                self.attempts[address] = (count + 1, since)
                raise ValueError('Jeton refusé.')
            self.attempts.pop(address, None)
            self.items = {k: v for k, v in self.items.items() if v['expires'] > now}
            if len(self.items) >= 100:
                raise ValueError('Trop de sessions actives.')
            sid = secrets.token_urlsafe(32)
            self.items[sid] = {'role': role, 'expires': now + 7 * 86400}
            return sid, role

    def get(self, cookie):
        try:
            parsed = SimpleCookie(cookie or '')
            sid = parsed['streamly_session'].value
        except (KeyError, ValueError):
            return None
        with self.lock:
            session = self.items.get(sid)
            if not session or session['expires'] < time.time():
                self.items.pop(sid, None)
                return None
            return dict(session, id=sid)

    def logout(self, sid):
        with self.lock:
            self.items.pop(sid, None)
