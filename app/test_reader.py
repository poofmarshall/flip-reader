"""Quick checks: run with  python -m pytest app/  from the project folder."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
os.environ["START_FETCHER"] = "0"
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["APP_TOKEN"] = "secret"

import server  # noqa: E402
from fetcher import normalize_url, clean_text, entry_image, pretty_site  # noqa: E402
from opml import parse_opml  # noqa: E402


def client():
    c = server.app.test_client()
    c.set_cookie("fr_token", "secret")
    return c


def test_normalize_strips_tracking():
    a = normalize_url("https://www.example.com/story/?utm_source=flipboard&utm_content=x#top")
    b = normalize_url("http://example.com/story")
    assert a == b


def test_clean_text_strips_html_and_truncates():
    t = clean_text("<p>Hello <b>world</b> &amp; friends</p>" + " word" * 200, limit=50)
    assert t.startswith("Hello world & friends")
    assert t.endswith("…") and len(t) <= 52


def test_entry_image_prefers_largest_media():
    e = {"media_content": [{"url": "s.jpg", "width": "100"}, {"url": "l.jpg", "width": "1200"}]}
    assert entry_image(e, "https://x.com/") == "l.jpg"
    e = {"summary": '<img src="/pic.png"> text'}
    assert entry_image(e, "https://x.com/a/b") == "https://x.com/pic.png"
    assert entry_image({"summary": "no pics"}, "https://x.com/") is None


def test_pretty_site():
    assert pretty_site("https://www.wsj.com/a") == "wsj.com"
    assert pretty_site("https://m.detroitnews.com/a") == "detroitnews.com"


def test_tidy_title():
    from fetcher import tidy_title
    assert tidy_title("Push Square | Latest Updates") == "Push Square"
    assert tidy_title("www.espn.com - TENNIS") == "ESPN Tennis"
    assert tidy_title("NYT &gt; Sports &gt; Tennis") == "NYT Tennis"
    assert tidy_title("Latest from Tom's Hardware") == "Tom's Hardware"
    assert tidy_title("Bless You Boys") == "Bless You Boys"
    assert tidy_title("Flipboard: Golf") == "Flipboard: Golf"


def test_opml_folders_and_nesting():
    items = parse_opml(b"""<opml><body>
      <outline xmlUrl="https://c/feed" text="C"/>
      <outline text="Tech"><outline xmlUrl="https://a/feed" title="A"/>
        <outline text="Sub"><outline xmlUrl="https://b/feed"/></outline></outline>
      </body></opml>""")
    # loose top-level feeds sort after folder entries so the folder wins on duplicates
    assert items == [("Tech", "https://a/feed", "A"), ("Tech", "https://b/feed", None), ("Unsorted", "https://c/feed", "C")]
    try:
        parse_opml(b"not xml")
        assert False
    except ValueError:
        pass


def test_auth_gate():
    c = server.app.test_client()
    assert c.get("/api/home").status_code == 401
    assert c.get("/").status_code == 302
    assert c.post("/login", data={"token": "wrong"}).status_code == 401
    # iPhone smart quotes / accents must be a clean "wrong code", never a crash
    assert c.post("/login", data={"token": "“secret”"}).status_code == 401
    c.set_cookie("fr_token", "café")
    assert c.get("/api/home").status_code == 401
    c.set_cookie("fr_token", "")
    r = c.post("/login", data={"token": "secret"})
    assert r.status_code == 302 and "fr_token" in r.headers.get("Set-Cookie", "")


def test_non_ascii_access_code_works():
    import hmac
    assert hmac.compare_digest("café".encode("utf-8"), "café".encode("utf-8"))
    assert not hmac.compare_digest("".encode("utf-8"), "café".encode("utf-8"))


def test_dead_feed_link_on_page_is_handled(monkeypatch):
    import requests
    from fetcher import resolve_feed_url

    class Page:
        status_code = 200
        url = "https://site.example/"
        content = b"<html><head><link rel='alternate' type='application/rss+xml' href='https://dead.example/feed'></head></html>"
        text = content.decode()

    def fake_get(url, **kw):
        if "dead.example" in url:
            raise requests.ConnectionError("no route")
        return Page()
    monkeypatch.setattr(requests, "get", fake_get)
    try:
        resolve_feed_url("https://site.example/")
        assert False, "should have raised a friendly ValueError"
    except ValueError as ex:
        assert "Couldn't find" in str(ex)


def test_topic_suggestions():
    from fetcher import flipboard_tags
    assert flipboard_tags({"tags": [{"scheme": "https://flipboard.com/topic/xbox", "term": "Xbox"},
                                    {"scheme": "https://flipboard.com/topic/clairobscur%3Aexpedition33", "term": "Clair Obscur"},
                                    {"scheme": None, "term": "misc"}]}) == [("xbox", "Xbox"), ("clairobscur:expedition33", "Clair Obscur")]

    db = server.db
    sid = db.get_or_create_section("Gaming")
    disc = db.get_or_create_section("Disc2", discover=True)
    f_ps, _ = db.add_feed("https://flipboard.com/topic/playstation.rss", sid, "Flipboard: PlayStation")
    f_d, _ = db.add_feed("https://flipboard.com/topic/other.rss", disc, "Other")
    now = time.time()
    mk = lambda fid, i: dict(feed_id=fid, guid=f"g{fid}-{i}", url=f"https://g.example/{fid}/{i}", norm_url=f"https://g.example/{fid}/{i}",
                             title="T", summary="", image=None, author=None, source="S", published=now - i)
    for i in range(5):  # 5 PlayStation stories, each tagged PlayStation (followed) + Xbox (not followed)
        db.upsert_article(mk(f_ps, i), [("playstation", "PlayStation"), ("xbox", "Xbox")])
    for i in range(2):  # too few to suggest
        db.upsert_article(mk(f_ps, 10 + i), [("nintendo", "Nintendo")])
    for i in range(6):  # Discover-section stories don't count
        db.upsert_article(mk(f_d, i), [("cooking", "Cooking")])

    c = client()
    sug = c.get("/api/suggestions").get_json()["suggestions"]
    assert [s["slug"] for s in sug] == ["xbox"]
    assert sug[0]["count"] == 5 and sug[0]["section_name"] == "Gaming" and sug[0]["url"].endswith("/topic/xbox.rss")

    assert c.post("/api/suggestions/dismiss", json={"slug": "xbox"}).status_code == 200
    assert c.get("/api/suggestions").get_json()["suggestions"] == []


def test_go_page_forwards_only_to_web_addresses():
    from urllib.parse import quote
    c = server.app.test_client()  # /go is open: it carries no data of ours
    r = c.get("/go?u=" + quote('https://www.espn.com/tennis/story?id=1&x="y"', safe=""))
    assert r.status_code == 200 and "no-store" in r.headers["Cache-Control"]
    body = r.get_data(as_text=True)
    assert 'location.replace("https://www.espn.com/tennis/story?id=1\\u0026x=\\"y\\"")' in body
    assert "</script>" in body and "<script>" in body.split("location.replace")[0]
    assert c.get("/go?u=javascript:alert(1)").status_code == 400
    assert c.get("/go").status_code == 400
    evil = c.get("/go?u=" + quote("https://a.example/</script><script>evil()</script>", safe="")).get_data(as_text=True)
    assert evil.count("<script>") == 1 and "\\u003c/script" in evil


def test_fewer_like_this_mutes():
    db = server.db
    watches = db.get_or_create_section("WatchesT")
    cars = db.get_or_create_section("CarsT")
    f_rolex, _ = db.add_feed("https://flipboard.com/topic/rolexT.rss", watches, "Flipboard: Rolex")
    f_cars, _ = db.add_feed("https://cars.example/feed", cars, "Cars")
    now = time.time()
    mk = lambda fid, i, url: dict(feed_id=fid, guid=f"m{fid}-{i}", url=url, norm_url=url, title=f"S{fid}-{i}", summary="",
                                  image=None, author=None, source="S", published=now - i)
    db.upsert_article(mk(f_rolex, 1, "https://essentiallysports.com/sinner"), [("rolex", "Rolex"), ("tennis", "Tennis")])
    db.upsert_article(mk(f_rolex, 2, "https://hodinkee.example/datejust"), [("rolex", "Rolex")])
    db.upsert_article(mk(f_cars, 3, "https://essentiallysports.com/f1"), [("f1", "F1")])

    c = client()
    titles = lambda sid: [s["title"] for s in c.get(f"/api/section/{sid}").get_json()["stories"]]
    assert titles(watches) == [f"S{f_rolex}-1", f"S{f_rolex}-2"]
    story = c.get(f"/api/section/{watches}").get_json()["stories"][0]
    assert story["domain"] == "essentiallysports.com" and story["section_name"] == "WatchesT"
    assert {t["slug"] for t in story["tags"]} == {"rolex", "tennis"}

    # hide "Tennis" in Watches only: the Watches story goes, the cars story (different section) stays
    r = c.post("/api/mutes", json={"section_id": watches, "kind": "tag", "value": "tennis", "label": "#Tennis in WatchesT"})
    assert r.status_code == 200
    assert titles(watches) == [f"S{f_rolex}-2"]
    assert titles(cars) == [f"S{f_cars}-3"]
    # ...and it's gone from For You too
    assert f"S{f_rolex}-1" not in [s["title"] for s in c.get("/api/section/foryou").get_json()["stories"]]

    # hide the site everywhere: cars story goes as well
    c.post("/api/mutes", json={"section_id": None, "kind": "domain", "value": "essentiallysports.com", "label": "x"})
    assert titles(cars) == []

    mutes = c.get("/api/settings").get_json()["mutes"]
    assert len(mutes) == 2 and mutes[0]["label"] == "x"
    for m in mutes:
        assert c.delete(f"/api/mutes/{m['id']}").status_code == 200
    assert titles(watches) == [f"S{f_rolex}-1", f"S{f_rolex}-2"]
    assert c.post("/api/mutes", json={"kind": "nope", "value": "x"}).status_code == 400


def test_duplicate_section_names_are_refused():
    c = client()
    assert c.post("/api/sections", json={"name": "Alpha"}).status_code == 200
    beta = c.post("/api/sections", json={"name": "Beta"}).get_json()["id"]
    r = c.post("/api/sections", json={"name": "alpha", "discover": True})
    assert r.status_code == 409 and "already" in r.get_json()["error"]
    r = c.patch(f"/api/sections/{beta}", json={"name": "ALPHA"})
    assert r.status_code == 409
    assert c.patch(f"/api/sections/{beta}", json={"name": "Beta"}).status_code == 200  # renaming to itself is fine


def test_mixing_dedupes_and_discover_hides_followed():
    db = server.db
    mine = db.get_or_create_section("Mine")
    disc = db.get_or_create_section("Disc", discover=True)
    f1, _ = db.add_feed("https://one.example/feed", mine, "One")
    f2, _ = db.add_feed("https://two.example/feed", mine, "Two")
    fd, _ = db.add_feed("https://flipboard.com/topic/x.rss", disc, "Flip")
    now = time.time()
    mk = lambda fid, url, title, age: dict(feed_id=fid, guid=url, url=url, norm_url=normalize_url(url), title=title,
                                             summary="", image=None, author=None, source="S", published=now - age)
    db.upsert_article(mk(f1, "https://site.com/a?utm_source=x", "A", 10))
    db.upsert_article(mk(f2, "https://site.com/a", "A again", 20))          # same story, other feed
    for i in range(10):
        db.upsert_article(mk(f1, f"https://site.com/one{i}", f"One {i}", 30 + i))  # chatty feed
    db.upsert_article(mk(f2, "https://other.com/b", "B", 5000))
    db.upsert_article(mk(fd, "https://site.com/a", "A via flipboard", 1))    # already followed → hidden
    db.upsert_article(mk(fd, "https://site.com/new", "same domain", 1))      # followed domain → hidden
    db.upsert_article(mk(fd, "https://fresh.example/c", "Fresh", 2))

    c = client()
    s = c.get(f"/api/section/{mine}").get_json()["stories"]
    titles = [x["title"] for x in s]
    assert titles.count("A") == 1 and "A again" not in titles
    assert "B" in titles[:4]  # the quiet feed's old story isn't buried by the chatty one

    d = c.get(f"/api/section/{disc}").get_json()["stories"]
    assert [x["title"] for x in d] == ["Fresh"]

    fy = c.get("/api/section/foryou").get_json()["stories"]
    assert "Fresh" in [x["title"] for x in fy] and fy[3]["title"] == "Fresh"

    home = c.get("/api/home").get_json()["tiles"]
    assert home[0]["id"] == "foryou" and any(t["discover"] for t in home)


def test_import_duplicates_keep_folder_and_make_no_unsorted():
    import io
    c = client()
    opml = b"""<opml><body>
      <outline xmlUrl="https://dup.example/feed" text="Dup"/>
      <outline text="Sports"><outline xmlUrl="https://dup.example/feed" text="Dup"/>
        <outline xmlUrl="https://golf.example/feed" text="Golf.com - Top Stories"/></outline></body></opml>"""
    r = c.post("/api/import", data={"file": (io.BytesIO(opml), "x.opml")}, content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json() == {"found": 3, "added": 2, "moved": 0}
    secs = {s["name"]: s for s in c.get("/api/settings").get_json()["sections"]}
    assert "Unsorted" not in secs
    titles = {f["title"] for f in secs["Sports"]["feeds"]}
    assert titles == {"Dup", "Golf.com"}
    # importing again adds nothing
    r = c.post("/api/import", data={"file": (io.BytesIO(opml), "x.opml")}, content_type="multipart/form-data")
    assert r.get_json()["added"] == 0

    # a feed we seeded into a Discover section moves to the user's folder when their import names one
    db = server.db
    disc = db.get_or_create_section("Seeded", discover=True)
    db.add_feed("https://seed.example/feed", disc, "Seeded Feed")
    opml2 = b"""<opml><body><outline text="Sports">
      <outline xmlUrl="https://seed.example/feed" text="Seeded Feed"/></outline></body></opml>"""
    r = c.post("/api/import", data={"file": (io.BytesIO(opml2), "y.opml")}, content_type="multipart/form-data")
    assert r.get_json() == {"found": 1, "added": 0, "moved": 1}
    secs = {s["name"]: s for s in c.get("/api/settings").get_json()["sections"]}
    assert "Seeded Feed" in {f["title"] for f in secs["Sports"]["feeds"]}
    assert secs["Seeded"]["feeds"] == []


def test_prune_keeps_latest_from_slow_feeds():
    db = server.db
    sid = db.get_or_create_section("PruneTest")
    fid, _ = db.add_feed("https://slow.example/feed", sid, "Slow")
    old = time.time() - 300 * 86400
    for i in range(15):
        db.upsert_article(dict(feed_id=fid, guid=f"s{i}", url=f"https://slow.example/{i}", norm_url=f"https://slow.example/{i}",
                               title=f"S{i}", summary="", image=None, author=None, source="Slow", published=old - i * 86400))
    db.prune(max_age_days=10, max_per_feed=150, keep_min=10)
    assert db.q("SELECT COUNT(*) AS n FROM articles WHERE feed_id = ?", (fid,))[0]["n"] == 10


def test_section_and_feed_edits():
    c = client()
    sid = c.post("/api/sections", json={"name": "Temp"}).get_json()["id"]
    assert c.post("/api/sections", json={"name": "  "}).status_code == 400
    assert c.patch(f"/api/sections/{sid}", json={"name": "Renamed", "move": -1}).status_code == 200
    assert "Renamed" in [s["name"] for s in c.get("/api/settings").get_json()["sections"]]
    assert c.post("/api/feeds", json={"kind": "url", "value": "", "section_id": sid}).status_code == 400
    assert c.post("/api/feeds", json={"kind": "url", "value": "x", "section_id": 9999}).status_code == 400
    assert c.delete(f"/api/sections/{sid}").status_code == 200
    assert c.get(f"/api/section/{sid}").status_code == 404
    assert c.get("/api/section/abc").status_code == 404
