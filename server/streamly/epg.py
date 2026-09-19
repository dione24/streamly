"""Guide des programmes (XMLTV) pour les lecteurs externes.

Le guide d'un panel pese des dizaines de Mo (77 Mo mesures, 230 000
programmes). Le relayer a chaque ouverture de TiviMate lierait le lecteur au
panel ; on le telecharge donc en tache de fond, on n'en garde que les chaines
du catalogue et les programmes pas encore termines, et on sert une copie
compressee depuis le disque.
"""
import calendar
import gzip
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

KEEP_PAST_SECONDS = 2 * 3600   # un lecteur affiche encore le programme precedent


def _epoch(stamp):
    """Instant XMLTV (« 20260919063000 +0200 ») en secondes, ou None."""
    m = re.match(r'(\d{14})\s*([+-]\d{4})?', stamp or '')
    if not m:
        return None
    seconds = calendar.timegm(time.strptime(m[1], '%Y%m%d%H%M%S'))
    if m[2]:
        offset = int(m[2][1:3]) * 3600 + int(m[2][3:5]) * 60
        seconds -= offset if m[2][0] == '+' else -offset
    return seconds


def _elements(path, tag):
    """Elements <tag> d'un gros fichier XMLTV, sans le charger en memoire."""
    root = None
    try:
        for event, elem in ET.iterparse(path, events=('start', 'end')):
            if root is None:
                root = elem
            if event == 'end' and elem.tag in ('channel', 'programme'):
                if elem.tag == tag:
                    yield elem
                # Sans cela, l'arbre garde chaque element lu : plusieurs
                # centaines de Mo pour un guide complet.
                root.clear()
    except ET.ParseError:
        # Un guide tronque ou mal forme : on garde ce qui a ete lu.
        return


class Guide:
    def __init__(self, path, sources, wanted, refresh_hours=6, log=print):
        """sources() -> [(nom, url, user_agent)] ; wanted() -> ids de chaines."""
        self.path, self.sources, self.wanted, self.log = path, sources, wanted, log
        self.refresh_seconds = max(0.5, float(refresh_hours)) * 3600
        self._lock = threading.Lock()
        self._running = False

    def age(self):
        try:
            return time.time() - os.path.getmtime(self.path)
        except OSError:
            return None

    def read(self):
        """Guide compresse en gzip, ou None s'il n'a jamais ete construit."""
        try:
            with open(self.path, 'rb') as fh:
                return fh.read()
        except OSError:
            return None

    def _index(self):
        """{chaine: [(debut, fin, titre)]} du guide sur disque, garde en memoire.

        Relu quand le fichier change. Le premier appel lance la lecture en
        tache de fond et repond vide : afficher une liste de chaines ne doit
        jamais attendre l'analyse de 90 000 programmes.
        """
        try:
            stamp = os.path.getmtime(self.path)
        except OSError:
            return {}
        with self._lock:
            if getattr(self, '_index_stamp', None) == stamp:
                return self._index_data
            if getattr(self, '_indexing', False):
                return getattr(self, '_index_data', {})
            self._indexing = True
        threading.Thread(target=self._load_index, args=(stamp,), daemon=True).start()
        return getattr(self, '_index_data', {})

    def _load_index(self, stamp):
        data = {}
        try:
            with gzip.open(self.path, 'rb') as fh:
                for elem in _elements(fh, 'programme'):
                    start, stop = _epoch(elem.get('start')), _epoch(elem.get('stop'))
                    if start is None or stop is None:
                        continue
                    data.setdefault(elem.get('channel'), []).append(
                        (start, stop, (elem.findtext('title') or '').strip()))
            for rows in data.values():
                rows.sort()
        except (OSError, EOFError):
            data = {}
        with self._lock:
            self._index_data, self._index_stamp, self._indexing = data, stamp, False

    def now_next(self, ids, now=None):
        """Programme en cours et suivant de chaque chaine demandee."""
        now = time.time() if now is None else now
        index, out = self._index(), {}
        for cid in ids:
            rows = index.get(cid)
            if not rows:
                continue
            current = following = None
            for start, stop, title in rows:
                if start <= now < stop:
                    current = {'title': title, 'start': start, 'stop': stop}
                elif start > now:
                    following = {'title': title, 'start': start, 'stop': stop}
                    break
            if current or following:
                out[cid] = {'now': current, 'next': following}
        return out

    def ensure_fresh(self, force=False):
        """Relance la construction en tache de fond si le guide a vieilli.

        force : apres une synchro, les chaines ont change ; un guide bati sur
        un catalogue encore vide resterait vide jusqu'au prochain cycle.
        """
        age = self.age()
        if not force and age is not None and age < self.refresh_seconds:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.rebuild()
        except Exception as exc:
            self.log('guide : echec (%s)' % exc)
        finally:
            with self._lock:
                self._running = False

    def rebuild(self, now=None):
        """Telecharge, filtre et remplace le guide. Garde l'ancien en cas d'echec."""
        now = time.time() if now is None else now
        wanted = {w for w in self.wanted() if w}
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        downloads = []
        try:
            for name, url, user_agent in self.sources():
                fd, tmp = tempfile.mkstemp(dir=folder, suffix='.xml')
                os.close(fd)
                downloads.append(tmp)
                try:
                    request = urllib.request.Request(url, headers={'User-Agent': user_agent,
                                                                   'Accept-Encoding': 'gzip'})
                    with urllib.request.urlopen(request, timeout=300) as response, open(tmp, 'wb') as out:
                        body = response
                        if (response.headers.get('Content-Encoding') or '').lower() == 'gzip':
                            body = gzip.GzipFile(fileobj=response)
                        shutil.copyfileobj(body, out, 1 << 20)
                except Exception as exc:
                    self.log('guide : %s indisponible (%s)' % (name, exc))
                    downloads.remove(tmp)
                    os.unlink(tmp)
            if not downloads:
                return False

            fd, packed = tempfile.mkstemp(dir=folder, suffix='.xml.gz')
            os.close(fd)
            owner, channels, programmes = {}, 0, 0
            try:
                with gzip.open(packed, 'wt', encoding='utf-8', compresslevel=6) as out:
                    out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="Streamly">\n')
                    # Les chaines d'abord, comme l'exige le format ; une chaine
                    # presente chez deux providers garde le guide du premier.
                    for n, tmp in enumerate(downloads):
                        for elem in _elements(tmp, 'channel'):
                            cid = elem.get('id')
                            if cid in wanted and cid not in owner:
                                owner[cid] = n
                                elem.tail = '\n'
                                out.write(ET.tostring(elem, encoding='unicode'))
                                channels += 1
                    for n, tmp in enumerate(downloads):
                        for elem in _elements(tmp, 'programme'):
                            if owner.get(elem.get('channel')) != n:
                                continue
                            stop = _epoch(elem.get('stop'))
                            if stop is not None and stop < now - KEEP_PAST_SECONDS:
                                continue
                            elem.tail = '\n'
                            out.write(ET.tostring(elem, encoding='unicode'))
                            programmes += 1
                    out.write('</tv>\n')
                os.replace(packed, self.path)
            except BaseException:
                os.unlink(packed)
                raise
            self.log('guide : %d chaines, %d programmes' % (channels, programmes))
            return True
        finally:
            for tmp in downloads:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
