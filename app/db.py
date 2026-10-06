"""SQLite storage: sections, feeds, articles."""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0,
    discover INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    etag TEXT,
    modified TEXT,
    last_fetched REAL,
    last_ok REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    url TEXT NOT NULL,
    norm_url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    image TEXT,
    image_tried INTEGER NOT NULL DEFAULT 0,
    author TEXT,
    source TEXT,
    published REAL NOT NULL,
    UNIQUE(feed_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published DESC);
CREATE INDEX IF NOT EXISTS idx_articles_norm ON articles(norm_url);
CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(article_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_tags_slug ON article_tags(slug);
CREATE TABLE IF NOT EXISTS dismissed_topics (
    slug TEXT PRIMARY KEY,
    dismissed_at REAL NOT NULL
);
"""

_lock = threading.RLock()


class DB:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- low-level helpers (all access goes through the lock) ---
    def q(self, sql, args=()):
        with _lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with _lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # --- sections ---
    def sections(self):
        return self.q("SELECT * FROM sections ORDER BY position, id")

    def get_or_create_section(self, name, discover=False):
        name = name.strip() or "Unsorted"
        with _lock:
            row = self.conn.execute("SELECT id FROM sections WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if row:
                return row["id"]
            pos = self.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM sections").fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO sections (name, position, discover) VALUES (?, ?, ?)", (name, pos, int(discover)))
            self.conn.commit()
            return cur.lastrowid

    def update_section(self, sid, name=None, discover=None):
        if name is not None and name.strip():
            self.x("UPDATE sections SET name = ? WHERE id = ?", (name.strip(), sid))
        if discover is not None:
            self.x("UPDATE sections SET discover = ? WHERE id = ?", (int(bool(discover)), sid))

    def move_section(self, sid, direction):
        with _lock:
            secs = self.sections()
            ids = [s["id"] for s in secs]
            if sid not in ids:
                return
            i = ids.index(sid)
            j = i + (1 if direction > 0 else -1)
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
            for pos, s in enumerate(ids):
                self.conn.execute("UPDATE sections SET position = ? WHERE id = ?", (pos, s))
            self.conn.commit()

    def delete_section(self, sid):
        self.x("DELETE FROM sections WHERE id = ?", (sid,))

    # --- feeds ---
    def feeds(self, section_id=None):
        if section_id is None:
            return self.q("SELECT * FROM feeds ORDER BY section_id, title")
        return self.q("SELECT * FROM feeds WHERE section_id = ? ORDER BY title", (section_id,))

    def add_feed(self, url, section_id, title=None):
        """Returns (feed_id, created)."""
        with _lock:
            row = self.conn.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone()
            if row:
                return row["id"], False
            cur = self.conn.execute(
                "INSERT INTO feeds (url, title, section_id) VALUES (?, ?, ?)", (url, title, section_id))
            self.conn.commit()
            return cur.lastrowid, True

    def update_feed(self, fid, **fields):
        allowed = {"title", "section_id", "etag", "modified", "last_fetched", "last_ok", "last_error"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE feeds SET {sets} WHERE id = ?", (*fields.values(), fid))

    def delete_feed(self, fid):
        self.x("DELETE FROM feeds WHERE id = ?", (fid,))

    # --- articles ---
    def upsert_article(self, a, tags=()):
        """Insert a new article; returns True if it was new. Existing rows keep their data
        but pick up an image if they lacked one, and any Flipboard topic tags not yet recorded."""
        with _lock:
            try:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (feed_id, guid, url, norm_url, title, summary, image, author, source, published)
                       VALUES (:feed_id, :guid, :url, :norm_url, :title, :summary, :image, :author, :source, :published)""",
                    a)
            except sqlite3.IntegrityError:
                return False  # the feed was deleted while its stories were downloading
            created = cur.rowcount > 0
            if not created and a.get("image"):
                self.conn.execute(
                    "UPDATE articles SET image = ? WHERE feed_id = ? AND guid = ? AND image IS NULL",
                    (a["image"], a["feed_id"], a["guid"]))
            if tags:
                row = self.conn.execute("SELECT id FROM articles WHERE feed_id = ? AND guid = ?",
                                        (a["feed_id"], a["guid"])).fetchone()
                if row:
                    self.conn.executemany("INSERT OR IGNORE INTO article_tags (article_id, slug, name) VALUES (?, ?, ?)",
                                          [(row["id"], slug, name) for slug, name in tags])
            self.conn.commit()
            return created

    # --- topic suggestions ---
    def topic_counts(self, days=14):
        """How often each Flipboard topic is tagged on recent stories in your own (non-Discover)
        sections, broken down by section: rows of slug, name, section_id, n."""
        return self.q(
            """SELECT t.slug, MIN(t.name) AS name, f.section_id, COUNT(DISTINCT a.norm_url) AS n
               FROM article_tags t
               JOIN articles a ON a.id = t.article_id
               JOIN feeds f ON f.id = a.feed_id
               JOIN sections s ON s.id = f.section_id
               WHERE s.discover = 0 AND a.published > ?
                 AND t.slug NOT IN (SELECT slug FROM dismissed_topics)
               GROUP BY t.slug, f.section_id""", (time.time() - days * 86400,))

    def dismiss_topic(self, slug):
        self.x("INSERT OR REPLACE INTO dismissed_topics (slug, dismissed_at) VALUES (?, ?)", (slug, time.time()))

    def articles_needing_image(self, limit):
        return self.q(
            """SELECT a.id, a.url FROM articles a
               WHERE a.image IS NULL AND a.image_tried = 0 AND a.url NOT LIKE 'https://news.google.com/%'
               ORDER BY a.published DESC LIMIT ?""", (limit,))

    def set_image(self, aid, image):
        self.x("UPDATE articles SET image = ?, image_tried = 1 WHERE id = ?", (image, aid))

    def prune(self, max_age_days, max_per_feed, keep_min=10):
        """Drop old stories, but a slow-posting site always keeps its newest `keep_min`
        so it never shows up empty."""
        cutoff = time.time() - max_age_days * 86400
        self.x("""DELETE FROM articles WHERE id IN (
                    SELECT id FROM (
                      SELECT id, published,
                             ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY published DESC) AS rn
                      FROM articles)
                    WHERE rn > ? OR (rn > ? AND published < ?))""", (max_per_feed, keep_min, cutoff))

    def section_articles(self, section_ids, limit):
        if not section_ids:
            return []
        marks = ",".join("?" * len(section_ids))
        return self.q(
            f"""SELECT a.*, f.title AS feed_title, f.section_id FROM articles a
                JOIN feeds f ON f.id = a.feed_id
                WHERE f.section_id IN ({marks})
                ORDER BY a.published DESC LIMIT ?""", (*section_ids, limit))
