"""Catalogue local en SQLite.

Le catalogue d'un panel peut depasser 90 000 entrees et 60 Mo de JSON. On le
synchronise une fois, puis on ne ressert que des donnees locales : c'est ce qui
evite le retelechargement complet a chaque ouverture.

Regrouper les chaines par (langue, nom canonique) donne deux choses :
  - l'echelle de qualite d'une meme chaine (SD / HD / FHD / UHD) ;
  - les sources alternatives, y compris chez un autre provider, pour basculer
    si un flux meurt en plein direct.
"""
import os
import re
import sqlite3
import time
import threading

from . import xtream


def _clean_label(name):
    """Retire le suffixe de qualite d'un nom de chaine, pour l'affichage."""
    if not name:
        return name
    cleaned = re.sub(xtream.QUALITY_TOKENS, "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -|:")
    return cleaned or name

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    provider_id   TEXT NOT NULL,
    stream_id     INTEGER NOT NULL,
    name          TEXT NOT NULL,
    lang          TEXT,
    canonical     TEXT,
    quality       TEXT,
    height        INTEGER DEFAULT 0,
    category_id   TEXT,
    category_name TEXT,
    icon          TEXT,
    epg_id        TEXT,
    is_backup     INTEGER DEFAULT 0,
    -- URL de lecture. Vide pour un panel Xtream, ou elle se reconstruit a
    -- partir des identifiants ; renseignee pour une playlist M3U, ou elle est
    -- la seule chose qui permette de jouer l'entree.
    url           TEXT,
    PRIMARY KEY (provider_id, stream_id)
);
CREATE INDEX IF NOT EXISTS idx_canon ON channels(lang, canonical);
CREATE INDEX IF NOT EXISTS idx_cat   ON channels(category_name);
CREATE INDEX IF NOT EXISTS idx_name  ON channels(name);

CREATE TABLE IF NOT EXISTS favorites (
    lang      TEXT NOT NULL,
    canonical TEXT NOT NULL,
    label     TEXT,
    added_at  INTEGER,
    PRIMARY KEY (lang, canonical)
);

CREATE TABLE IF NOT EXISTS vod (
    provider_id   TEXT NOT NULL,
    stream_id     INTEGER NOT NULL,
    name          TEXT NOT NULL,
    lang          TEXT,
    title         TEXT,
    category_id   TEXT,
    category_name TEXT,
    icon          TEXT,
    rating        REAL,
    added         INTEGER,
    -- Renseignes paresseusement : ces champs n'existent que dans
    -- get_vod_info, soit un appel reseau par film.
    container     TEXT,
    bitrate       INTEGER,
    duration      TEXT,
    plot          TEXT,
    PRIMARY KEY (provider_id, stream_id)
);
CREATE INDEX IF NOT EXISTS idx_vod_lang  ON vod(lang);
CREATE INDEX IF NOT EXISTS idx_vod_cat   ON vod(category_name);
CREATE INDEX IF NOT EXISTS idx_vod_title ON vod(title);

CREATE TABLE IF NOT EXISTS series (
    provider_id   TEXT NOT NULL,
    series_id     INTEGER NOT NULL,
    name          TEXT NOT NULL,
    lang          TEXT,
    title         TEXT,
    category_id   TEXT,
    category_name TEXT,
    icon          TEXT,
    rating        REAL,
    added         INTEGER,
    -- Renseignes paresseusement par get_series_info, comme le conteneur d'un
    -- film : un appel reseau par serie, fait a l'ouverture de la fiche.
    plot          TEXT,
    episodes_at   INTEGER,
    PRIMARY KEY (provider_id, series_id)
);
CREATE INDEX IF NOT EXISTS idx_series_lang  ON series(lang);
CREATE INDEX IF NOT EXISTS idx_series_cat   ON series(category_name);
CREATE INDEX IF NOT EXISTS idx_series_title ON series(title);

CREATE TABLE IF NOT EXISTS episodes (
    provider_id TEXT NOT NULL,
    episode_id  INTEGER NOT NULL,
    series_id   INTEGER NOT NULL,
    season      INTEGER,
    episode     INTEGER,
    title       TEXT,
    container   TEXT,
    duration    TEXT,
    plot        TEXT,
    PRIMARY KEY (provider_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_ep_series ON episodes(provider_id, series_id, season, episode);

CREATE TABLE IF NOT EXISTS sync_state (
    provider_id TEXT PRIMARY KEY,
    last_sync   INTEGER,
    channels    INTEGER,
    note        TEXT
);
"""


class Catalog:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()
        if 'url' not in {r['name'] for r in self._db.execute('PRAGMA table_info(channels)')}:
            with self._db:
                self._db.execute('ALTER TABLE channels ADD COLUMN url TEXT')
        if self._db.execute('PRAGMA user_version').fetchone()[0] < 1:
            rows = self._db.execute('SELECT provider_id, stream_id, name FROM channels').fetchall()
            with self._db:
                self._db.executemany('UPDATE channels SET lang=?, canonical=? WHERE provider_id=? AND stream_id=?',
                    [(xtream.parse_name(r['name'])[0], xtream.parse_name(r['name'])[1], r['provider_id'], r['stream_id']) for r in rows])
                self._db.execute('PRAGMA user_version=1')

    @property
    def _db(self):
        if not hasattr(self._local, 'db'):
            self._local.db = sqlite3.connect(self.path, timeout=15)
            self._local.db.row_factory = sqlite3.Row
        return self._local.db

    def close(self):
        self._db.close()

    def purge_absent(self, active_ids):
        """Efface les entrees des providers qui ne sont plus configures.

        Sans cela, supprimer un abonnement laisse ses chaines et ses films dans
        le catalogue : l'interface continue de les proposer alors qu'aucune
        source ne peut plus les servir, et la lecture reste noire.
        """
        active = tuple(active_ids)
        removed = {}
        with self._db:
            for table in ("channels", "vod", "series", "episodes", "sync_state"):
                if active:
                    holes = ",".join("?" * len(active))
                    cur = self._db.execute(
                        "DELETE FROM %s WHERE provider_id NOT IN (%s)" % (table, holes),
                        active)
                else:
                    cur = self._db.execute("DELETE FROM %s" % table)
                if cur.rowcount > 0:
                    removed[table] = cur.rowcount
        return removed

    # ------------------------------------------------------------ synchro

    def sync_provider(self, provider, client, log=print):
        """Importe les chaines d'un provider et reconstruit ses categories."""
        pid = provider["id"]
        log("[%s] recuperation des chaines..." % pid)
        streams = client.live_streams()
        log("[%s] %d chaines" % (pid, len(streams)))

        # Libelles de categories : l'API d'abord, le M3U en secours.
        cat_names = {}
        try:
            for c in client.live_categories():
                cat_names[str(c.get("category_id"))] = c.get("category_name") or ""
        except Exception as exc:
            log("[%s] categories indisponibles (%s)" % (pid, exc))

        note = "categories via API"
        if not cat_names and streams:
            log("[%s] categories vides — reconstruction depuis le M3U (lent)" % pid)
            try:
                by_stream = client.category_names_from_m3u()
                votes = {}
                for s in streams:
                    group = by_stream.get(str(s.get("stream_id")))
                    if group:
                        cid = str(s.get("category_id"))
                        votes.setdefault(cid, {})
                        votes[cid][group] = votes[cid].get(group, 0) + 1
                cat_names = {
                    cid: max(g.items(), key=lambda kv: kv[1])[0]
                    for cid, g in votes.items()
                }
                note = "categories reconstruites depuis le M3U"
                log("[%s] %d categories reconstruites" % (pid, len(cat_names)))
            except Exception as exc:
                log("[%s] reconstruction impossible (%s)" % (pid, exc))
                note = "categories indisponibles"

        rows = []
        for s in streams:
            name = s.get("name") or ""
            lang, canonical, quality = xtream.parse_name(name)
            cid = str(s.get("category_id"))
            rows.append((
                pid, int(s.get("stream_id") or 0), name, lang, canonical, quality,
                xtream.quality_height(quality), cid, cat_names.get(cid),
                s.get("stream_icon") or "", s.get("epg_channel_id") or "",
                1 if xtream.is_backup(name) else 0, s.get("url") or None,
            ))

        with self._db:
            self._db.execute("DELETE FROM channels WHERE provider_id=?", (pid,))
            self._db.executemany(
                "INSERT OR REPLACE INTO channels "
                "(provider_id,stream_id,name,lang,canonical,quality,height,"
                " category_id,category_name,icon,epg_id,is_backup,url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.execute(
                "INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?)",
                (pid, int(time.time()), len(rows), note))
        return len(rows), note

    def sync_vod(self, provider, client, log=print, m3u_map=None):
        """Importe le catalogue de films.

        `get_vod_categories` renvoie souvent une liste vide : on reconstruit
        alors les libelles depuis les `group-title` du M3U, comme pour le
        direct. Le M3U deja telecharge peut etre passe via `m3u_map`.
        """
        pid = provider["id"]
        try:
            movies = client.vod_streams()
        except Exception as exc:
            log("[%s] VOD indisponible (%s)" % (pid, exc))
            return 0

        log("[%s] %d films" % (pid, len(movies)))

        cat_names = {}
        try:
            for c in client._api("get_vod_categories") or []:
                cat_names[str(c.get("category_id"))] = c.get("category_name") or ""
        except Exception:
            pass

        if not cat_names and m3u_map is None:
            log("[%s] categories VOD vides — lecture du M3U (lent, ~2 min)" % pid)
            try:
                m3u_map = client.category_names_from_m3u()
            except Exception as exc:
                log("[%s] M3U indisponible (%s)" % (pid, exc))
                m3u_map = {}

        if not cat_names and m3u_map:
            votes = {}
            for m in movies:
                group = m3u_map.get(str(m.get("stream_id")))
                if group:
                    cid = str(m.get("category_id"))
                    votes.setdefault(cid, {})
                    votes[cid][group] = votes[cid].get(group, 0) + 1
            cat_names = {cid: max(g.items(), key=lambda kv: kv[1])[0]
                         for cid, g in votes.items()}
            log("[%s] %d categories VOD reconstruites" % (pid, len(cat_names)))

        rows = []
        for m in movies:
            name = m.get("name") or ""
            lang, _canon, _q = xtream.parse_name(name)
            cid = str(m.get("category_id"))
            try:
                rating = float(m.get("rating") or 0)
            except (TypeError, ValueError):
                rating = 0.0
            rows.append((
                pid, int(m.get("stream_id") or 0), name, lang,
                _clean_label(re.sub(r"^\s*[A-Z]{2,4}\s*[-|:]\s*", "", name)),
                cid, cat_names.get(cid), m.get("stream_icon") or "",
                rating, int(m.get("added") or 0),
            ))

        with self._db:
            self._db.execute('CREATE TEMP TABLE IF NOT EXISTS incoming_vod (id INTEGER PRIMARY KEY)')
            self._db.execute('DELETE FROM incoming_vod')
            self._db.executemany('INSERT OR IGNORE INTO incoming_vod VALUES (?)', [(r[1],) for r in rows])
            self._db.execute('DELETE FROM vod WHERE provider_id=? AND stream_id NOT IN (SELECT id FROM incoming_vod)', (pid,))
            self._db.executemany(
                "INSERT INTO vod "
                "(provider_id,stream_id,name,lang,title,category_id,"
                " category_name,icon,rating,added) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider_id,stream_id) DO UPDATE SET name=excluded.name, lang=excluded.lang, "
                "title=excluded.title, category_id=excluded.category_id, category_name=excluded.category_name, "
                "icon=excluded.icon, rating=excluded.rating, added=excluded.added",
                rows)
        return len(rows)

    def sync_series(self, provider, client, log=print):
        """Importe le catalogue de series.

        Seule la liste est importee ici. Les saisons et les episodes exigent
        un appel `get_series_info` par serie : sur un catalogue de plusieurs
        milliers de titres, les prefetcher rendrait la synchronisation
        interminable. Ils sont recuperes a l'ouverture de la fiche, puis
        conserves — meme compromis que le conteneur d'un film.
        """
        pid = provider["id"]
        try:
            shows = client.series()
        except Exception as exc:
            log("[%s] series indisponibles (%s)" % (pid, exc))
            return 0
        log("[%s] %d series" % (pid, len(shows)))

        cat_names = {}
        try:
            for c in client.series_categories():
                cat_names[str(c.get("category_id"))] = c.get("category_name") or ""
        except Exception as exc:
            log("[%s] categories de series indisponibles (%s)" % (pid, exc))

        rows = []
        for show in shows:
            name = show.get("name") or ""
            lang, _canon, _q = xtream.parse_name(name)
            cid = str(show.get("category_id"))
            try:
                rating = float(show.get("rating") or 0)
            except (TypeError, ValueError):
                rating = 0.0
            rows.append((
                pid, int(show.get("series_id") or 0), name, lang,
                _clean_label(re.sub(r"^\s*[A-Z]{2,4}\s*[-|:]\s*", "", name)),
                cid, cat_names.get(cid), show.get("cover") or "",
                rating, int(show.get("last_modified") or 0),
            ))

        with self._db:
            self._db.execute('CREATE TEMP TABLE IF NOT EXISTS incoming_series (id INTEGER PRIMARY KEY)')
            self._db.execute('DELETE FROM incoming_series')
            self._db.executemany('INSERT OR IGNORE INTO incoming_series VALUES (?)', [(r[1],) for r in rows])
            # Les episodes suivent leur serie : sans cela, une serie retiree
            # du catalogue laisserait ses episodes orphelins et injouables.
            self._db.execute('DELETE FROM episodes WHERE provider_id=? AND series_id NOT IN (SELECT id FROM incoming_series)', (pid,))
            self._db.execute('DELETE FROM series WHERE provider_id=? AND series_id NOT IN (SELECT id FROM incoming_series)', (pid,))
            self._db.executemany(
                "INSERT INTO series "
                "(provider_id,series_id,name,lang,title,category_id,"
                " category_name,icon,rating,added) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider_id,series_id) DO UPDATE SET name=excluded.name, lang=excluded.lang, "
                "title=excluded.title, category_id=excluded.category_id, category_name=excluded.category_name, "
                "icon=excluded.icon, rating=excluded.rating, added=excluded.added",
                rows)
        return len(rows)

    def series_browse(self, lang=None, category=None, query=None,
                      limit=120, offset=0):
        sql = ["SELECT provider_id, series_id, title, lang, category_name,",
               " icon, rating FROM series WHERE 1=1"]
        args = []
        if lang:
            sql.append("AND lang=?")
            args.append(lang)
        if category:
            sql.append("AND category_name=?")
            args.append(category)
        if query:
            sql.append("AND name LIKE ?")
            args.append("%" + query + "%")
        sql.append("ORDER BY added DESC, title LIMIT ? OFFSET ?")
        args += [limit, offset]
        return [dict(r) for r in self._db.execute(" ".join(sql), args).fetchall()]

    def series_categories(self, lang=None):
        sql = ("SELECT category_name AS name, COUNT(*) AS n FROM series "
               "WHERE category_name IS NOT NULL AND category_name<>''")
        args = []
        if lang:
            sql += " AND lang=?"
            args.append(lang)
        sql += " GROUP BY category_name ORDER BY n DESC"
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def series_languages(self):
        cur = self._db.execute(
            "SELECT lang, COUNT(*) AS n FROM series WHERE lang IS NOT NULL "
            "GROUP BY lang ORDER BY n DESC")
        return [dict(r) for r in cur.fetchall()]

    def series_get(self, provider_id, series_id):
        row = self._db.execute(
            "SELECT * FROM series WHERE provider_id=? AND series_id=?",
            (provider_id, int(series_id))).fetchone()
        return dict(row) if row else None

    def series_episodes(self, provider_id, series_id):
        cur = self._db.execute(
            "SELECT episode_id, season, episode, title, container, duration, plot "
            "FROM episodes WHERE provider_id=? AND series_id=? "
            "ORDER BY season, episode", (provider_id, int(series_id)))
        return [dict(r) for r in cur.fetchall()]

    def series_set_episodes(self, provider_id, series_id, plot, episodes):
        """Remplace les episodes connus d'une serie.

        Un remplacement complet plutot qu'une fusion : une saison retiree par
        le fournisseur doit disparaitre, sinon elle reste proposee et sa
        lecture echoue sans explication.
        """
        sid = int(series_id)
        rows = [(provider_id, int(e["episode_id"]), sid, e.get("season"), e.get("episode"),
                 e.get("title") or "", e.get("container") or "mp4",
                 e.get("duration") or "", (e.get("plot") or "")[:1200])
                for e in episodes]
        with self._db:
            self._db.execute("DELETE FROM episodes WHERE provider_id=? AND series_id=?",
                             (provider_id, sid))
            self._db.executemany(
                "INSERT OR REPLACE INTO episodes "
                "(provider_id,episode_id,series_id,season,episode,title,container,duration,plot) "
                "VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._db.execute(
                "UPDATE series SET plot=?, episodes_at=? WHERE provider_id=? AND series_id=?",
                ((plot or "")[:1200], int(time.time()), provider_id, sid))
        return len(rows)

    def episode_get(self, provider_id, episode_id):
        row = self._db.execute(
            "SELECT * FROM episodes WHERE provider_id=? AND episode_id=?",
            (provider_id, int(episode_id))).fetchone()
        return dict(row) if row else None

    def vod_browse(self, lang=None, category=None, query=None,
                   limit=120, offset=0):
        sql = ["SELECT provider_id, stream_id, title, lang, category_name,",
               " icon, rating, container, bitrate, duration FROM vod WHERE 1=1"]
        args = []
        if lang:
            sql.append("AND lang=?")
            args.append(lang)
        if category:
            sql.append("AND category_name=?")
            args.append(category)
        if query:
            sql.append("AND name LIKE ?")
            args.append("%" + query + "%")
        sql.append("ORDER BY added DESC LIMIT ? OFFSET ?")
        args += [limit, offset]
        return [dict(r) for r in self._db.execute(" ".join(sql), args).fetchall()]

    def vod_categories(self, lang=None):
        sql = ("SELECT category_name AS name, COUNT(*) AS n FROM vod "
               "WHERE category_name IS NOT NULL AND category_name<>''")
        args = []
        if lang:
            sql += " AND lang=?"
            args.append(lang)
        sql += " GROUP BY category_name ORDER BY n DESC"
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def vod_get(self, provider_id, stream_id):
        cur = self._db.execute(
            "SELECT * FROM vod WHERE provider_id=? AND stream_id=?",
            (provider_id, int(stream_id)))
        row = cur.fetchone()
        return dict(row) if row else None

    def vod_set_details(self, provider_id, stream_id, container, bitrate,
                        duration, plot):
        with self._db:
            self._db.execute(
                "UPDATE vod SET container=?, bitrate=?, duration=?, plot=? "
                "WHERE provider_id=? AND stream_id=?",
                (container, bitrate, duration, plot, provider_id, int(stream_id)))

    # ------------------------------------------------------------ lectures

    def provider_counts(self, provider_id):
        """Ce que le catalogue local connait d'un abonnement."""
        counts = {}
        for table, column in (("channels", "stream_id"), ("vod", "stream_id"),
                              ("series", "series_id"), ("episodes", "episode_id")):
            counts[table] = self._db.execute(
                "SELECT COUNT(%s) FROM %s WHERE provider_id=?" % (column, table),
                (provider_id,)).fetchone()[0]
        return counts

    def stats(self):
        cur = self._db.execute(
            "SELECT s.provider_id, s.last_sync, s.channels, s.note FROM sync_state s")
        return [dict(r) for r in cur.fetchall()]

    def categories(self, lang=None, provider=None):
        sql = ("SELECT category_name AS name, COUNT(*) AS n FROM channels "
               "WHERE category_name IS NOT NULL AND category_name<>'' AND is_backup=0")
        args = []
        if provider:
            sql += " AND provider_id=?"
            args.append(provider)
        if lang:
            sql += " AND lang=?"
            args.append(lang)
        sql += " GROUP BY category_name ORDER BY n DESC"
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def languages(self, provider=None):
        sql = ("SELECT lang, COUNT(*) AS n FROM channels "
               "WHERE lang IS NOT NULL AND is_backup=0")
        args = []
        if provider:
            sql += " AND provider_id=?"
            args.append(provider)
        sql += " GROUP BY lang ORDER BY n DESC"
        cur = self._db.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]

    def browse(self, lang=None, category=None, query=None, limit=200, offset=0,
               provider=None):
        """Une ligne par chaine distincte : on masque les doublons de qualite.

        `provider` restreint a un abonnement : utile quand plusieurs comptes
        sont configures et que l'on veut explorer le catalogue de l'un d'eux.
        """
        sql = ["SELECT lang, canonical,",
               "  MIN(name) AS label,",
               "  MAX(icon) AS icon,",
               "  MAX(category_name) AS category,",
               "  MAX(epg_id) AS epg_id,",
               "  COUNT(*) AS sources,",
               "  MAX(height) AS best_height,",
               "  GROUP_CONCAT(DISTINCT provider_id) AS providers",
               "FROM channels WHERE canonical<>'' AND is_backup=0"]
        args = []
        if provider:
            sql.append("AND provider_id=?")
            args.append(provider)
        if lang:
            sql.append("AND lang=?")
            args.append(lang)
        if category:
            sql.append("AND category_name=?")
            args.append(category)
        if query:
            sql.append("AND (canonical LIKE ? OR name LIKE ?)")
            like = "%" + query.upper() + "%"
            args += [like, like]
        sql.append("GROUP BY lang, canonical ORDER BY label LIMIT ? OFFSET ?")
        args += [limit, offset]
        cur = self._db.execute(" ".join(sql), args)

        rows = []
        for r in cur.fetchall():
            row = dict(r)
            # Le libelle vient d'une variante arbitraire du groupe : on retire
            # son suffixe de qualite, qui ne correspond pas forcement a la
            # source qui sera reellement jouee.
            row["label"] = _clean_label(row["label"])
            rows.append(row)
        return rows

    def player_channels(self):
        """Toutes les chaines distinctes, pour les lecteurs externes.

        Meme regroupement que browse(), sans pagination : TiviMate et VLC
        chargent le bouquet d'un bloc, et c'est SQLite qui le sert, jamais le
        panel d'origine (plus de deux minutes pour son export M3U).
        """
        cur = self._db.execute(
            "SELECT lang, canonical, MIN(name) AS label, MAX(icon) AS icon, "
            "MAX(category_name) AS category, MAX(epg_id) AS epg_id "
            "FROM channels WHERE canonical<>'' AND is_backup=0 "
            "GROUP BY lang, canonical ORDER BY label, lang, canonical")
        return [dict(r, label=_clean_label(r["label"])) for r in cur.fetchall()]

    def has_icon(self, url):
        """Vrai si cette adresse est le logo ou l'affiche d'une entree du catalogue."""
        row = self._db.execute(
            "SELECT 1 FROM channels WHERE icon=?1 UNION ALL SELECT 1 FROM vod WHERE icon=?1 "
            "UNION ALL SELECT 1 FROM series WHERE icon=?1 LIMIT 1", (url,)).fetchone()
        return row is not None

    def signature(self):
        """Change des qu'une synchro ou une purge modifie les chaines."""
        row = self._db.execute(
            "SELECT (SELECT COUNT(*) FROM channels), "
            "COALESCE(SUM(last_sync), 0), COALESCE(SUM(channels), 0) FROM sync_state").fetchone()
        return tuple(row)

    def sources(self, lang, canonical):
        """Toutes les variantes d'une chaine, tous providers confondus.

        Triees pour l'usage : d'abord la hauteur la plus proche de la cible,
        les flux principaux avant les flux de secours.
        """
        cur = self._db.execute(
            "SELECT provider_id, stream_id, name, quality, height, is_backup, url "
            "FROM channels WHERE lang IS ? AND canonical=? "
            "ORDER BY is_backup, height",
            (lang, canonical))
        return [dict(r) for r in cur.fetchall()]

    def pick_source(self, lang, canonical, target_height=720):
        """Choisit la variante a ingerer.

        Ingerer du 720p plutot que du 1080p divise le cout CPU par trois pour
        un rendu identique apres transcodage. On prend donc la hauteur la plus
        proche de la cible, sans descendre sous elle si on peut l'eviter.
        """
        srcs = [s for s in self.sources(lang, canonical) if not s["is_backup"]]
        if not srcs:
            srcs = self.sources(lang, canonical)
        if not srcs:
            return None
        at_or_above = [s for s in srcs if s["height"] >= target_height]
        pool = at_or_above or srcs
        return min(pool, key=lambda s: abs((s["height"] or 0) - target_height))

    def channel(self, provider_id, stream_id):
        cur = self._db.execute(
            "SELECT * FROM channels WHERE provider_id=? AND stream_id=?",
            (provider_id, stream_id))
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- favoris

    def favorites(self):
        cur = self._db.execute(
            "SELECT lang, canonical, label FROM favorites ORDER BY label")
        return [dict(r) for r in cur.fetchall()]

    def add_favorite(self, lang, canonical, label):
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO favorites VALUES (?,?,?,?)",
                (lang or "", canonical, label, int(time.time())))

    def remove_favorite(self, lang, canonical):
        with self._db:
            self._db.execute(
                "DELETE FROM favorites WHERE lang IS ? AND canonical=?",
                (lang or "", canonical))
