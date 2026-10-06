"""Flip Reader web server: serves the phone app and its small JSON API."""
import hmac
import logging
import os
import re
import time
from urllib.parse import quote, unquote

from flask import Flask, jsonify, request, send_from_directory, redirect, make_response

from db import DB
from fetcher import Fetcher, resolve_feed_url, google_news_url, flipboard_topic_url, domain_of, tidy_title
from opml import parse_opml

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("server")

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
APP_TOKEN = os.environ.get("APP_TOKEN", "").strip()
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "20"))
STATIC = os.path.join(os.path.dirname(__file__), "static")

# Aggregators: their domain says nothing about which *source* you follow.
AGGREGATOR_DOMAINS = {"news.google.com", "flipboard.com", "feedproxy.google.com", "feeds.feedburner.com"}
SAME_FEED_PENALTY = 3 * 3600   # each extra story from one feed counts as 3h older
FOR_YOU_SIZE = 90
SECTION_SIZE = 120

DEFAULT_DISCOVER = [
    ("flipboard", "news"),
    ("flipboard", "technology"),
    ("google", "Detroit Tigers"),
    ("google", "Michigan"),
]

db = DB(os.path.join(DATA_DIR, "reader.db"))
fetcher = Fetcher(db, refresh_minutes=REFRESH_MINUTES)
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


def seed_defaults():
    if db.sections():
        return
    sid = db.get_or_create_section("Discover", discover=True)
    for kind, value in DEFAULT_DISCOVER:
        if kind == "flipboard":
            db.add_feed(flipboard_topic_url(value), sid, f"Flipboard: {value.title()}")
        else:
            db.add_feed(google_news_url(value), sid, f"Google News: {value}")


# ---------- auth (optional shared token, on top of Cloudflare Access) ----------

def _authed():
    if not APP_TOKEN:
        return True
    tok = request.cookies.get("fr_token", "")
    # compare as bytes: compare_digest on str rejects any non-ASCII character (accents, smart quotes)
    return hmac.compare_digest(tok.encode("utf-8"), APP_TOKEN.encode("utf-8"))


@app.before_request
def gate():
    open_paths = ("/login", "/manifest.webmanifest", "/static/icon", "/static/style.css", "/apple-touch-icon", "/api/health")
    if any(request.path.startswith(p) for p in open_paths) or _authed():
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="not signed in"), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        typed = request.form.get("token", "").strip().encode("utf-8")
        if APP_TOKEN and hmac.compare_digest(typed, APP_TOKEN.encode("utf-8")):
            resp = make_response(redirect("/"))
            resp.set_cookie("fr_token", APP_TOKEN, max_age=400 * 86400, httponly=True, samesite="Lax",
                            secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
            return resp
        return send_from_directory(STATIC, "login.html"), 401
    return send_from_directory(STATIC, "login.html")


# ---------- story mixing ----------

def _shape(a):
    return {
        "id": a["id"], "url": a["url"], "title": a["title"], "summary": a["summary"] or "",
        "image": a["image"], "source": a["source"] or a.get("feed_title") or domain_of(a["url"]),
        "author": a["author"], "published": a["published"], "section_id": a.get("section_id"),
    }


def mix(articles, limit, exclude_urls=(), exclude_domains=()):
    """Newest first, but no single chatty feed can bury the others, and the same
    story arriving from two feeds only shows once."""
    seen = set(exclude_urls)
    per_feed = {}
    scored = []
    for a in articles:  # already newest-first
        if a["norm_url"] in seen:
            continue
        if exclude_domains and domain_of(a["url"]) in exclude_domains:
            continue
        seen.add(a["norm_url"])
        n = per_feed.get(a["feed_id"], 0)
        per_feed[a["feed_id"]] = n + 1
        scored.append((a["published"] - n * SAME_FEED_PENALTY, a))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [a for _, a in scored[:limit]]


def _split_sections():
    secs = db.sections()
    mine = [s["id"] for s in secs if not s["discover"]]
    disc = [s["id"] for s in secs if s["discover"]]
    return secs, mine, disc


def _followed():
    """URLs and source domains from your own feeds, so Discover shows new sources."""
    _, mine, _ = _split_sections()
    arts = db.section_articles(mine, 5000)
    urls = {a["norm_url"] for a in arts}
    domains = {domain_of(a["url"]) for a in arts} | {domain_of(f["url"]) for f in db.feeds() if f["section_id"] in mine}
    return urls, domains - AGGREGATOR_DOMAINS


def section_stories(sid):
    secs, mine, disc = _split_sections()
    if sid == "foryou":
        own = mix(db.section_articles(mine, 2000), FOR_YOU_SIZE)
        urls, domains = _followed()
        fresh = mix(db.section_articles(disc, 1000), FOR_YOU_SIZE // 4, urls, domains)
        out = []
        # every 4th story is a Discover pick
        while own or fresh:
            for _ in range(3):
                if own:
                    out.append(own.pop(0))
            if fresh:
                out.append(fresh.pop(0))
        return "For You", out[:FOR_YOU_SIZE]
    sec = next((s for s in secs if s["id"] == sid), None)
    if not sec:
        return None, []
    raw = db.section_articles([sid], 1500)
    if sec["discover"]:
        urls, domains = _followed()
        return sec["name"], mix(raw, SECTION_SIZE, urls, domains)
    return sec["name"], mix(raw, SECTION_SIZE)


# ---------- reading API ----------

@app.get("/api/home")
def home():
    secs, _, _ = _split_sections()
    tiles = []
    name, stories = section_stories("foryou")
    tiles.append(_tile("foryou", name, stories, False))
    for s in secs:
        _, st = section_stories(s["id"])
        tiles.append(_tile(s["id"], s["name"], st, bool(s["discover"])))
    return jsonify(tiles=tiles, last_refresh=fetcher.last_run)


def _tile(sid, name, stories, discover):
    cover = next((a for a in stories if a["image"]), stories[0] if stories else None)
    return {"id": sid, "name": name, "discover": discover, "count": len(stories),
            "cover": _shape(cover) if cover else None}


@app.get("/api/section/<sid>")
def section(sid):
    key = "foryou" if sid == "foryou" else (int(sid) if sid.isdigit() else None)
    name, stories = section_stories(key) if key is not None else (None, [])
    if name is None:
        return jsonify(error="section not found"), 404
    return jsonify(name=name, stories=[_shape(a) for a in stories])


# ---------- settings API ----------

@app.get("/api/settings")
def settings():
    out = []
    for s in db.sections():
        out.append({**s, "discover": bool(s["discover"]), "feeds": [
            {"id": f["id"], "url": f["url"], "title": f["title"] or f["url"],
             "last_ok": f["last_ok"], "last_error": f["last_error"]} for f in db.feeds(s["id"])]})
    return jsonify(sections=out, last_refresh=fetcher.last_run, refresh_minutes=REFRESH_MINUTES)


@app.post("/api/sections")
def add_section():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="Give the section a name."), 400
    if _section_named(name):
        return jsonify(error="You already have a section with that name."), 409
    sid = db.get_or_create_section(name, discover=bool(body.get("discover")))
    return jsonify(id=sid)


def _section_named(name, except_id=None):
    name = name.strip().lower()
    return any(s["name"].lower() == name and s["id"] != except_id for s in db.sections())


@app.patch("/api/sections/<int:sid>")
def edit_section(sid):
    body = request.get_json(force=True, silent=True) or {}
    if body.get("name") and _section_named(body["name"], except_id=sid):
        return jsonify(error="You already have a section with that name."), 409
    if "move" in body:
        db.move_section(sid, int(body["move"]))
    db.update_section(sid, name=body.get("name"), discover=body.get("discover"))
    return jsonify(ok=True)


@app.delete("/api/sections/<int:sid>")
def delete_section(sid):
    db.delete_section(sid)
    return jsonify(ok=True)


@app.post("/api/feeds")
def add_feed():
    body = request.get_json(force=True, silent=True) or {}
    kind = body.get("kind", "url")
    value = (body.get("value") or "").strip()
    if not value:
        return jsonify(error="Type something to add."), 400
    sid = body.get("section_id")
    if not sid or not any(s["id"] == sid for s in db.sections()):
        return jsonify(error="Pick a section first."), 400
    try:
        if kind == "google":
            url, title = google_news_url(value), f"Google News: {value}"
        elif kind == "flipboard":
            url, title = flipboard_topic_url(value), f"Flipboard: {value.title()}"
            resolve_feed_url(url)  # check the topic exists
        else:
            url, title = resolve_feed_url(value)
            title = (body.get("title") or "").strip() or title
    except ValueError as ex:
        msg = str(ex)
        if kind == "flipboard":
            msg = "Flipboard doesn't have a topic by that name."
        return jsonify(error=msg), 400
    fid, created = db.add_feed(url, sid, title)
    if not created:
        return jsonify(error="You already follow that feed."), 409
    feed = next(f for f in db.feeds() if f["id"] == fid)
    fetcher.fetch_feed(feed)  # so its stories show right away
    return jsonify(id=fid, title=title)


@app.patch("/api/feeds/<int:fid>")
def edit_feed(fid):
    body = request.get_json(force=True, silent=True) or {}
    if body.get("section_id"):
        db.update_feed(fid, section_id=int(body["section_id"]))
    if body.get("title"):
        db.update_feed(fid, title=body["title"].strip())
    return jsonify(ok=True)


@app.delete("/api/feeds/<int:fid>")
def delete_feed(fid):
    db.delete_feed(fid)
    return jsonify(ok=True)


@app.post("/api/import")
def import_opml():
    f = request.files.get("file")
    if not f:
        return jsonify(error="Choose your OPML file first."), 400
    try:
        items = parse_opml(f.read())
    except ValueError as ex:
        return jsonify(error=str(ex)), 400
    added = moved = 0
    discover_ids = {s["id"] for s in db.sections() if s["discover"]}
    known = {f["url"]: f for f in db.feeds()}
    for folder, url, title in items:
        if url in known:
            # Already followed. If it's only there as one of our Discover starters and the
            # import puts it in a real folder, the folder wins.
            f = known[url]
            if f["section_id"] in discover_ids and folder != "Unsorted":
                db.update_feed(f["id"], section_id=db.get_or_create_section(folder))
                f["section_id"] = -1
                moved += 1
            continue
        sid = db.get_or_create_section(folder)
        fid, created = db.add_feed(url, sid, tidy_title(title) if title else None)
        known[url] = {"id": fid, "section_id": sid}
        added += created
    fetcher.poke()
    return jsonify(found=len(items), added=added, moved=moved)


# ---------- topic suggestions ----------

TOPIC_FEED = re.compile(r"^https?://flipboard\.com/topic/([^/.?#]+)\.rss$", re.I)
MIN_SUGGESTION_STORIES = 3
# Flipboard's umbrella labels land on nearly every story; suggesting them tells you nothing.
GENERIC_TOPICS = {
    "news", "technology", "business", "entertainment", "lifestyle", "health", "finance", "sports", "science",
    "food", "politics", "worldnews", "world", "usnews", "gaming", "travel", "culture", "money", "economy",
    "media", "education", "environment", "society", "opinion", "video", "videos", "photos", "music", "movies",
    "television", "celebrities", "style", "fashion", "shopping", "automotive", "autos", "cars", "home",
}


@app.get("/api/suggestions")
def suggestions():
    """Flipboard topics that keep appearing on stories you already read but don't follow yet."""
    followed = set()
    for f in db.feeds():
        m = TOPIC_FEED.match(f["url"])
        if m:
            followed.add(unquote(m.group(1)).lower())
    secs = {s["id"]: s["name"] for s in db.sections()}
    agg = {}
    for r in db.topic_counts():
        if r["slug"] in followed or r["slug"] in GENERIC_TOPICS:
            continue
        a = agg.setdefault(r["slug"], {"slug": r["slug"], "name": r["name"], "count": 0, "by_section": {}})
        a["count"] += r["n"]
        a["by_section"][r["section_id"]] = a["by_section"].get(r["section_id"], 0) + r["n"]
    out = []
    for a in sorted(agg.values(), key=lambda x: -x["count"]):
        if a["count"] < MIN_SUGGESTION_STORIES or len(out) >= 10:
            break
        best = max(a["by_section"], key=a["by_section"].get)  # the section those stories mostly came from
        out.append({"slug": a["slug"], "name": a["name"], "count": a["count"], "section_id": best,
                    "section_name": secs.get(best, ""), "url": f"https://flipboard.com/topic/{quote(a['slug'])}.rss"})
    return jsonify(suggestions=out)


@app.post("/api/suggestions/dismiss")
def dismiss_suggestion():
    body = request.get_json(force=True, silent=True) or {}
    slug = (body.get("slug") or "").strip().lower()
    if not slug:
        return jsonify(error="nothing to dismiss"), 400
    db.dismiss_topic(slug)
    return jsonify(ok=True)


@app.post("/api/refresh")
def refresh():
    fetcher.poke()
    return jsonify(ok=True)


@app.get("/api/health")
def health():
    return jsonify(ok=True, feeds=len(db.feeds()), last_refresh=fetcher.last_run, now=time.time())


# ---------- the app shell ----------

@app.get("/")
def index():
    resp = send_from_directory(STATIC, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/sw.js")
def sw():
    resp = send_from_directory(STATIC, "sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/apple-touch-icon.png")
def touch_icon():
    return send_from_directory(STATIC, "icon-180.png")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


seed_defaults()
if os.environ.get("START_FETCHER", "1") == "1":
    fetcher.start()


def create_app():
    """Entry point for the production server (waitress)."""
    return app


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8090")), threaded=True)
