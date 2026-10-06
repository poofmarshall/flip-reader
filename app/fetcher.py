"""Fetches feeds, extracts articles + images, and keeps the database fresh."""
import calendar
import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote

import feedparser
import requests

log = logging.getLogger("fetcher")

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FlipReader/1.0"
TIMEOUT = 15
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|cmpid$)", re.I)
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)


# ---------- small helpers ----------

def normalize_url(url):
    """Strip tracking params / fragments so the same story from two feeds dedupes."""
    try:
        p = urlparse(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
        host = p.netloc.lower().removeprefix("www.")
        path = p.path.rstrip("/") or "/"
        return urlunparse(("https", host, path, "", urlencode(q), ""))
    except Exception:
        return url


def domain_of(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def pretty_site(url):
    """'https://www.wsj.com/...' -> 'wsj.com' (good enough as a byline)."""
    d = domain_of(url)
    for prefix in ("m.", "amp.", "news.", "edition."):
        if d.count(".") > 1 and d.startswith(prefix):
            d = d[len(prefix):]
    return d


GENERIC_SUFFIX = re.compile(
    r"\s*[|\-–—:]\s*(latest( updates| news| stories| articles| posts)?|all( content| stories)?|top stories|"
    r"news|feed|rss|home|videos|articles|technology news.*|additive manufacturing.*)\s*$", re.I)


def tidy_title(title):
    """Make a feed's name read like a byline: 'Push Square | Latest Updates' -> 'Push Square',
    'NYT > Sports > Tennis' -> 'NYT Tennis', 'www.espn.com - TENNIS' -> 'ESPN Tennis'."""
    t = html.unescape(title or "").strip()
    t = re.sub(r"^(latest|news) from\s+", "", t, flags=re.I)
    t = GENERIC_SUFFIX.sub("", t)
    if " > " in t:
        parts = [p.strip() for p in t.split(">") if p.strip()]
        t = f"{parts[0]} {parts[-1]}" if len(parts) > 1 else parts[0]
    m = re.match(r"^(?:www\.)?([a-z0-9-]+)\.[a-z]{2,4}\s*[-|:]\s*(.+)$", t, re.I)
    if m:  # 'www.espn.com - TENNIS' -> 'ESPN Tennis'
        site, rest = m.groups()
        t = f"{site.upper() if len(site) <= 4 else site.title()} {rest.title() if rest.isupper() else rest}"
    t = re.sub(r"^www\.", "", t)
    return t.strip() or title


def clean_text(raw, limit=400):
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _first_img_in_html(raw, base):
    if not raw:
        return None
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", raw, re.I)
    if m:
        src = html.unescape(m.group(1))
        if src.startswith("data:"):
            return None
        # skip obvious tracking pixels / emoji
        if re.search(r"(pixel|feedburner|/emoji/|gravatar|1x1|spacer)", src, re.I):
            return None
        return urljoin(base, src)
    return None


def entry_image(e, link):
    """Best-effort image for a feed entry, from the richest source available."""
    candidates = []
    for m in e.get("media_content", []) or []:
        url = m.get("url")
        if url and (m.get("medium") in (None, "image") or IMG_EXT.search(url)):
            try:
                w = int(m.get("width") or 0)
            except ValueError:
                w = 0
            candidates.append((w, url))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    for m in e.get("media_thumbnail", []) or []:
        if m.get("url"):
            return m["url"]
    for l in e.get("links", []) or []:
        if l.get("rel") == "enclosure" and (l.get("type", "").startswith("image") or IMG_EXT.search(l.get("href", ""))):
            return l.get("href")
    if e.get("image") and isinstance(e["image"], dict) and e["image"].get("href"):
        return e["image"]["href"]
    for c in e.get("content", []) or []:
        img = _first_img_in_html(c.get("value"), link)
        if img:
            return img
    return _first_img_in_html(e.get("summary"), link)


TOPIC_SCHEME = re.compile(r"^https?://flipboard\.com/topic/([^/?#]+)$", re.I)


def flipboard_tags(e):
    """Flipboard tags every story with its topics:
    <category domain="https://flipboard.com/topic/xbox">Xbox</category> -> ("xbox", "Xbox")."""
    out = []
    for t in e.get("tags") or []:
        m = TOPIC_SCHEME.match(t.get("scheme") or "")
        term = (t.get("term") or "").strip()
        if m and term:
            out.append((unquote(m.group(1)).lower(), term))
    return out


def entry_time(e):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            try:
                ts = calendar.timegm(t)
                return min(ts, time.time())  # some feeds post-date items
            except Exception:
                pass
    return time.time()


def og_image(url):
    """Pull og:image / twitter:image from an article page (first ~300KB only)."""
    try:
        with requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, stream=True) as r:
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return None
            chunk = b""
            deadline = time.monotonic() + TIMEOUT  # overall cap: a slow-dripping server must not hold a worker
            for part in r.iter_content(32768):
                chunk += part
                if len(chunk) > 300_000 or b"</head>" in chunk or time.monotonic() > deadline:
                    break
            page = chunk.decode(r.encoding or "utf-8", errors="replace")
            for prop in ("og:image", "twitter:image", "og:image:url"):
                m = (re.search(rf"<meta[^>]+(?:property|name)=[\"']{prop}[\"'][^>]+content=[\"']([^\"']+)", page, re.I)
                     or re.search(rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']{prop}[\"']", page, re.I))
                if m:
                    return urljoin(r.url, html.unescape(m.group(1)))
    except Exception as ex:
        log.debug("og:image failed for %s: %s", url, ex)
    return None


# ---------- turning whatever the user pasted into a feed URL ----------

def resolve_feed_url(url):
    """Accepts a feed URL, a website URL, or a Flipboard magazine/topic page and
    returns (feed_url, title). Raises ValueError with a friendly message."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("That doesn't look like a web address.")

    # Flipboard pages: magazine or topic -> append .rss
    if domain_of(url) == "flipboard.com" and not p.path.endswith(".rss"):
        url = urlunparse((p.scheme, p.netloc, p.path.rstrip("/") + ".rss", "", "", ""))

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException:
        raise ValueError("Couldn't reach that address.")
    if r.status_code >= 400:
        raise ValueError(f"That site answered with an error ({r.status_code}).")

    parsed = feedparser.parse(r.content)
    if parsed.entries:
        return url, tidy_title(parsed.feed.get("title")) or domain_of(url)

    # It's a web page: look for <link rel="alternate" type="application/rss+xml">
    page = r.text
    for m in re.finditer(r"<link[^>]+>", page, re.I):
        tag = m.group(0)
        if re.search(r"rel=[\"']?alternate", tag, re.I) and re.search(r"type=[\"']application/(rss|atom)\+xml", tag, re.I):
            href = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if href:
                feed_url = urljoin(r.url, html.unescape(href.group(1)))
                try:
                    fp = feedparser.parse(requests.get(feed_url, headers={"User-Agent": UA}, timeout=TIMEOUT).content)
                except requests.RequestException:
                    continue  # stale feed link on the page; try the next one
                if fp.entries:
                    return feed_url, tidy_title(fp.feed.get("title")) or domain_of(url)
    raise ValueError("Couldn't find a news feed at that address.")


def google_news_url(query):
    return "https://news.google.com/rss/search?" + urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def flipboard_topic_url(topic):
    slug = re.sub(r"[^a-z0-9]+", "", topic.lower())
    return f"https://flipboard.com/topic/{slug}.rss"


# ---------- the fetch loop ----------

FEED_PASS_SECONDS = 600    # whole-refresh caps, so one hung download can't stall the loop forever
IMAGE_PASS_SECONDS = 240


def _bounded_map(fn, items, timeout, workers=8):
    """Run fn over items in threads, giving up on stragglers after `timeout` seconds.
    Returns (item, result) pairs for the ones that finished without error."""
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(fn, it): it for it in items}
    done, not_done = wait(futures, timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)
    if not_done:
        log.warning("%d of %d downloads did not finish within %ss; skipped this round", len(not_done), len(items), timeout)
    return [(futures[f], f.result()) for f in done if f.exception() is None]


class Fetcher:
    def __init__(self, db, refresh_minutes=20, og_per_run=60, max_age_days=10, max_per_feed=150):
        self.db = db
        self.refresh_minutes = refresh_minutes
        self.og_per_run = og_per_run
        self.max_age_days = max_age_days
        self.max_per_feed = max_per_feed
        self._wake = threading.Event()
        self._running = threading.Lock()
        self.last_run = None

    def fetch_feed(self, feed):
        """Never raises: one bad feed (or one deleted mid-fetch) must not abort the whole refresh."""
        try:
            return self._fetch_feed(feed)
        except Exception as ex:
            log.warning("feed %s failed: %s", feed["url"], str(ex)[:200])
            try:
                self.db.update_feed(feed["id"], last_fetched=time.time(), last_error=str(ex)[:200])
            except Exception:
                pass
            return 0

    def _fetch_feed(self, feed):
        headers = {"User-Agent": UA}
        if feed.get("etag"):
            headers["If-None-Match"] = feed["etag"]
        if feed.get("modified"):
            headers["If-Modified-Since"] = feed["modified"]
        now = time.time()
        try:
            r = requests.get(feed["url"], headers=headers, timeout=TIMEOUT)
            if r.status_code == 304:
                self.db.update_feed(feed["id"], last_fetched=now, last_ok=now, last_error=None)
                return 0
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if not parsed.entries and parsed.bozo:
                raise ValueError("not a readable feed")
        except Exception as ex:
            msg = str(ex)[:200]
            log.warning("feed %s failed: %s", feed["url"], msg)
            self.db.update_feed(feed["id"], last_fetched=now, last_error=msg)
            return 0

        is_gnews = domain_of(feed["url"]) == "news.google.com"
        is_flip = domain_of(feed["url"]) == "flipboard.com"
        # the name you gave the feed (e.g. from NewsBlur) beats the feed's own, often clunky, title
        feed_name = feed.get("title") or parsed.feed.get("title") or domain_of(feed["url"])
        new = 0
        for e in parsed.entries[:100]:
            link = e.get("link")
            title = clean_text(e.get("title"), 300)
            if not link or not title:
                continue
            source = None
            if e.get("source") and isinstance(e["source"], dict):
                source = e["source"].get("title")
            if is_gnews and source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            summary = "" if is_gnews else clean_text(e.get("summary") or (e.get("content") or [{}])[0].get("value"))
            a = {
                "feed_id": feed["id"],
                "guid": e.get("id") or link,
                "url": link,
                "norm_url": normalize_url(link),
                "title": title,
                "summary": summary,
                "image": entry_image(e, link),
                "author": clean_text(e.get("author"), 80) or None,
                "source": (source if is_gnews else pretty_site(link) if is_flip else feed_name),
                "published": entry_time(e),
            }
            if self.db.upsert_article(a, flipboard_tags(e)):
                new += 1
        fields = dict(last_fetched=now, last_ok=now, last_error=None,
                      etag=r.headers.get("ETag"), modified=r.headers.get("Last-Modified"))
        if not feed.get("title"):
            fields["title"] = tidy_title(parsed.feed.get("title")) or domain_of(feed["url"])
        self.db.update_feed(feed["id"], **fields)
        return new

    def run_once(self):
        if not self._running.acquire(blocking=False):
            return None  # a refresh is already in progress
        try:
            feeds = self.db.feeds()
            new = sum(r for _, r in _bounded_map(self.fetch_feed, feeds, FEED_PASS_SECONDS))
            # fill in missing pictures from the article pages
            todo = self.db.articles_needing_image(self.og_per_run)
            for art, img in _bounded_map(lambda a: og_image(a["url"]), todo, IMAGE_PASS_SECONDS):
                self.db.set_image(art["id"], img)
            self.db.prune(self.max_age_days, self.max_per_feed)
            self.last_run = time.time()
            log.info("refreshed %d feeds, %d new articles", len(feeds), new)
            return new
        finally:
            self._running.release()

    def poke(self):
        """Ask the background loop to refresh soon (e.g. after adding a feed)."""
        self._wake.set()

    def loop(self):
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("refresh failed")
            self._wake.wait(self.refresh_minutes * 60)
            self._wake.clear()

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="fetcher").start()
