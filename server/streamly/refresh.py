"""Rafraichissement periodique des catalogues, independant des visiteurs."""
import math
import threading
import time


def refresh_seconds(cfg):
    try:
        hours = float(cfg.get('catalog_refresh_hours', 6))
    except (TypeError, ValueError):
        hours = 6
    if not math.isfinite(hours) or hours < 0:
        hours = 6
    return max(1, hours) * 3600 if hours else 0


def catalog_due(cfg, stats, provider_id, now):
    interval = refresh_seconds(cfg)
    if not interval:
        return False
    last = next((s.get('last_sync') for s in stats if s['provider_id'] == provider_id), None)
    return not last or now - float(last) >= interval


class CatalogRefresh:
    RETRY_SECONDS = 15 * 60

    def __init__(self, cfg, stats, sync, busy, log=print):
        self.cfg, self.stats, self.sync, self.busy, self.log = cfg, stats, sync, busy, log
        self.attempted = {}
        self.stop = threading.Event()

    def tick(self, now=None):
        now = time.time() if now is None else now
        if not refresh_seconds(self.cfg) or self.busy():
            return
        providers = [p['id'] for p in self.cfg.get('providers', []) if p.get('enabled', True)]
        self.attempted = {pid: at for pid, at in self.attempted.items() if pid in providers}
        for pid in providers:
            if self.stop.is_set() or self.busy():
                return
            # Les dates SQLite survivent au redemarrage et incluent les
            # synchronisations manuelles et celles declenchees par l'ajout.
            if not catalog_due(self.cfg, self.stats(), pid, now):
                continue
            if now - self.attempted.get(pid, -float('inf')) < self.RETRY_SECONDS:
                continue
            self.attempted[pid] = now
            try:
                self.sync(pid)
            except Exception as exc:
                # Un fournisseur en panne ne doit pas tuer la surveillance
                # ni reveler une URL authentifiee dans le journal.
                self.log('Rafraichissement catalogue interrompu (%s).' % type(exc).__name__)

    def start(self):
        threading.Thread(target=self._loop, name='catalog-refresh', daemon=True).start()

    def _loop(self):
        # Laisser le service HTTP demarrer avant un import potentiellement long.
        while not self.stop.wait(60):
            try:
                self.tick()
            except Exception as exc:
                self.log('Verification des catalogues impossible (%s).' % type(exc).__name__)

    def close(self):
        self.stop.set()
