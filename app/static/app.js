"use strict";

// the version baked into this script's URL by the server; compared with what the server reports
const APP_VERSION = (() => { try { return new URL(document.currentScript.src).searchParams.get("v"); } catch (_) { return null; } })();

const $ = (s, el = document) => el.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", redirect: "manual", ...opts });
  // 401 = our own access-code gate; an opaque redirect = Cloudflare Access wants a fresh sign-in,
  // which only a full page load can show
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  if (r.type === "opaqueredirect" || r.status === 0) { location.href = "/"; throw new Error("Signing you back in…"); }
  let body = {};
  try { body = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(body.error || `Something went wrong (${r.status})`);
  return body;
}
const send = (path, method, data) => api(path, {
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}),
});

function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 30 * 86400) return `${Math.round(s / 86400)}d`;
  if (s < 365 * 86400) return `${Math.round(s / (30 * 86400))}mo`;
  return `${Math.round(s / (365 * 86400))}y`;
}

function openStory(story) {
  // via /go so iOS doesn't hand the link to a native app (see server.py)
  window.open("/go?u=" + encodeURIComponent(story.url), "_blank", "noopener");
}

/* =========================================================
   Router
   ========================================================= */
const views = { home: $("#home"), reader: $("#reader"), settings: $("#settings") };
function show(name) {
  for (const [k, v] of Object.entries(views)) v.hidden = k !== name;
}
function route() {
  const h = location.hash;
  const m = h.match(/^#\/s\/(\w+)/);
  if (m) { show("reader"); Reader.open(m[1]); }
  else if (h === "#/settings") { show("settings"); Settings.load(); }
  else { show("home"); Home.load(); }
}
window.addEventListener("hashchange", route);

/* =========================================================
   Home: section tiles
   ========================================================= */
const Home = {
  async load() {
    const box = $("#tiles");
    if (!box.children.length) box.append(el("div", "empty", "Loading…"));
    let data;
    try { data = await api("/api/home"); }
    catch (e) { box.replaceChildren(el("div", "empty", e.message)); return; }
    if (data.version && APP_VERSION && data.version !== APP_VERSION && !sessionStorage.getItem("reloaded:" + data.version)) {
      // a newer app is on the server: fetch it (once per version, so a mismatch can't loop)
      try { sessionStorage.setItem("reloaded:" + data.version, "1"); } catch (_) {}
      location.reload();
      return;
    }
    box.replaceChildren();
    for (const t of data.tiles) {
      if (!t.count && t.id !== "foryou") continue; // empty sections live in Settings only
      const tile = el("a", "tile" + (t.id === "foryou" ? " wide" : ""));
      tile.href = `#/s/${t.id}`;
      if (t.cover && t.cover.image) tile.style.backgroundImage = `url("${t.cover.image.replace(/"/g, "%22")}")`;
      else tile.classList.add("noimg");
      if (t.discover) tile.append(el("span", "t-badge", "DISCOVER"));
      const txt = el("div", "t-text");
      txt.append(el("div", "t-name", t.name));
      txt.append(el("div", "t-story", t.cover ? t.cover.title : (t.count ? "" : "No stories yet — check back in a minute")));
      tile.append(txt);
      box.append(tile);
    }
    if (data.tiles.length <= 2 && data.tiles.every(t => !t.count)) {
      box.append(el("div", "empty", "Your stories are being gathered for the first time. Pull them in from NewsBlur in Settings (⚙︎)."));
    }
    Home.suggest(box);
  },

  /* "Topics you might like": Flipboard topics tagged on the stories you already read */
  async suggest(box) {
    let data;
    try { data = await api("/api/suggestions"); } catch (_) { return; }
    if (!data.suggestions.length || box !== $("#tiles")) return;
    const wrap = el("div", "suggest");
    wrap.append(el("h2", "s-head", "Topics you might like"),
                el("p", "s-sub", "Flipboard topics that keep coming up in the stories you already follow"));
    const maybeRemove = () => { if (!wrap.querySelector(".s-row")) wrap.remove(); };
    for (const s of data.suggestions) {
      const row = el("div", "s-row");
      const main = el("div", "s-main");
      main.append(el("div", "s-name", `#${s.name}`),
                  el("div", "s-why", `${s.count} recent stories · would go in ${s.section_name}`));
      const add = el("button", "s-add", "Add");
      add.onclick = async () => {
        add.disabled = true; add.textContent = "Adding…";
        try {
          await send("/api/feeds", "POST", { kind: "url", value: s.url, section_id: s.section_id, title: `Flipboard: ${s.name}` });
          row.remove(); maybeRemove();
        } catch (e) {
          add.disabled = false; add.textContent = "Add";
          main.append(el("div", "s-why err", e.message));
        }
      };
      const no = el("button", "s-no", "×");
      no.setAttribute("aria-label", "Not interested");
      no.onclick = async () => {
        row.remove(); maybeRemove();
        try { await send("/api/suggestions/dismiss", "POST", { slug: s.slug }); } catch (_) {}
      };
      row.append(main, add, no);
      wrap.append(row);
    }
    box.append(wrap);
  },
};
$("#settingsBtn").onclick = () => { location.hash = "#/settings"; };
$("#refreshBtn").onclick = async () => {
  const note = $("#refreshNote");
  note.hidden = false; note.textContent = "Checking for new stories…";
  try { await send("/api/refresh", "POST"); } catch (_) {}
  setTimeout(async () => { await Home.load(); note.hidden = true; }, 7000);
};

/* =========================================================
   Pages: stories -> Flipboard-style layouts
   ========================================================= */
function storyCard(story, cls = "") {
  const c = el("article", "card " + cls);
  if (story.image) {
    const pic = el("div", "pic");
    const img = el("img");
    img.dataset.src = story.image;
    img.alt = "";
    img.onerror = () => { pic.remove(); c.classList.add("noimg"); };
    pic.append(img);
    c.append(pic);
  } else {
    c.classList.add("noimg");
  }
  const src = el("div", "src", story.source || "");
  src.append(el("span", "ago", ` · ${ago(story.published)}`));
  const more = el("button", "more", "···");
  more.setAttribute("aria-label", "Fewer like this");
  more.onclick = (e) => { e.stopPropagation(); Sheet.open(story); };
  c.append(more, src, el("h3", null, story.title));
  if (story.summary) c.append(el("p", null, story.summary));
  c.dataset.url = story.url;
  return c;
}

function buildPages(name, stories) {
  const pages = [];
  // cover page
  const lead = stories.find(s => s.image) || stories[0];
  const cover = el("div", "page cover");
  if (lead && lead.image) {
    const pre = new Image(); pre.src = lead.image;
    cover.style.backgroundImage = `url("${lead.image.replace(/"/g, "%22")}")`;
  } else cover.classList.add("noimg");
  cover.append(el("div", "c-name", name));
  const top = el("div");
  top.append(cover.lastChild);
  top.append(el("div", "c-sub", `${stories.length} stories`));
  cover.append(top);
  if (lead) {
    const cs = el("div", "c-story");
    cs.append(el("div", "src", lead.source || ""), el("h3", null, lead.title), el("div", "c-hint", "Swipe up to flip ↑"));
    cover.append(cs);
    cover.dataset.url = lead.url;
  }
  pages.push(cover);

  const rest = stories.filter(s => s !== lead);
  const pattern = ["hero", "duo", "trio", "duo", "hero", "trio"];
  let i = 0, p = 0;
  while (i < rest.length) {
    let kind = pattern[p % pattern.length];
    // a hero page wants a picture; fall back if this story has none and the next does
    if (kind === "hero" && !rest[i].image && rest.length - i >= 2) kind = "duo";
    const pg = el("div", "page " + kind);
    if (kind === "hero") {
      pg.append(storyCard(rest[i++]));
    } else if (kind === "duo") {
      pg.append(storyCard(rest[i++]));
      if (i < rest.length) pg.append(storyCard(rest[i++]));
    } else {
      pg.append(storyCard(rest[i++], "top"));
      if (i < rest.length) {
        const row = el("div", "row");
        row.append(storyCard(rest[i++]));
        if (i < rest.length) row.append(storyCard(rest[i++]));
        pg.append(row);
      }
    }
    pages.push(pg);
    p++;
  }
  const end = el("div", "page end");
  end.append(el("b", null, "You're all caught up"), el("div", null, "That's every story in this section for now."));
  const back = el("button", null, "Back to sections");
  back.onclick = (e) => { e.stopPropagation(); location.hash = "#/"; };
  end.append(back);
  pages.push(end);
  return pages;
}

function loadImages(page) {
  if (!page) return;
  for (const img of page.querySelectorAll("img[data-src]")) {
    img.src = img.dataset.src;
    img.removeAttribute("data-src");
  }
}

/* =========================================================
   Reader: the flip engine
   ========================================================= */
const Reader = {
  pages: [], cur: 0, sid: null, busy: false, positions: {}, loadedAt: {},

  async open(sid) {
    const stage = $("#stage");
    const fresh = this.loadedAt[sid] && Date.now() - this.loadedAt[sid] < 5 * 60 * 1000;
    if (this.sid === sid && this.pages.length && fresh) { this.layout(); return; }
    this.sid = sid;
    this.pages = []; this.cur = 0;  // nothing is flippable until this section's pages exist
    $("#readerTitle").textContent = "";
    $("#pageNum").textContent = "";
    stage.replaceChildren(el("div", "page end", "Loading…"));
    stage.firstChild.classList.add("current");
    let data;
    try { data = await api(`/api/section/${sid}`); }
    catch (e) { stage.firstChild.textContent = e.message; this.sid = null; return; }  // so a retry re-fetches
    if (this.sid !== sid) return;
    this.loadedAt[sid] = Date.now();
    $("#readerTitle").textContent = data.name;
    if (!data.stories.length) {
      stage.firstChild.replaceChildren(el("b", null, "No stories yet"),
        el("div", null, "New feeds take a minute or two to fill in. Check back shortly."));
      this.pages = [];
      return;
    }
    this.pages = buildPages(data.name, data.stories);
    stage.replaceChildren(...this.pages);
    const saved = this.positions[sid] || 0;
    this.cur = Math.min(saved, this.pages.length - 1);
    this.setCurrent(this.cur);
    $("#flipHint").hidden = !!localStorageGet("flipHintSeen");
  },

  layout() { this.setCurrent(this.cur); },

  /* re-fetch the current section (after "fewer like this"); keeps the page position */
  reload() {
    const sid = this.sid;
    if (!sid) return;
    this.loadedAt[sid] = 0; this.sid = null;
    this.open(sid);
  },

  setCurrent(i) {
    this.pages.forEach((p, k) => p.classList.toggle("current", k === i));
    this.cur = i;
    this.positions[this.sid] = i;
    for (const k of [i - 1, i, i + 1, i + 2]) loadImages(this.pages[k]);
    const total = this.pages.length;
    $("#pageNum").textContent = total ? `${i + 1}/${total}` : "";
  },

  /* Build the 3D fold for a flip from page `cur` toward `dir` (+1 forward, -1 back).
     The page is split at its middle. Going forward, the bottom half of the current page
     folds up over the top, revealing the next page's bottom half and, on its back, the
     next page's top half — the classic Flipboard flip. */
  beginFlip(dir) {
    const stage = $("#stage");
    if (!this.pages[this.cur]) return null;  // loading / empty section: nothing to fold
    const H = stage.clientHeight, W = stage.clientWidth, half = H / 2;
    const cur = this.pages[this.cur];
    const other = this.pages[this.cur + dir] || null;
    if (other) loadImages(other);

    const piece = (page, which) => {
      const h = el("div", "half");
      h.style.height = half + "px";
      h.style.top = which === "top" ? "0px" : half + "px";
      if (page) {
        const c = page.cloneNode(true);
        c.classList.remove("current");
        c.style.height = H + "px"; c.style.width = W + "px";
        c.style.top = which === "top" ? "0px" : -half + "px";
        c.style.bottom = "auto";
        h.append(c);
      }
      return h;
    };
    const layer = el("div", "flip-layer");
    const flipper = el("div", "flipper");
    flipper.style.height = half + "px";
    const front = el("div", "face front");
    const back = el("div", "face back");
    const shadeFront = el("div", "shade");
    const shadeBack = el("div", "shade");
    const shadeUnder = el("div", "shade");

    let still, under;
    if (dir > 0) {
      still = piece(cur, "top");              // stays put
      under = piece(other, "bottom");         // revealed as the fold lifts
      flipper.style.top = half + "px";
      flipper.style.transformOrigin = "50% 0%";
      front.append(piece(cur, "bottom"));
      back.append(piece(other, "top"));
    } else {
      still = piece(cur, "bottom");
      under = piece(other, "top");
      flipper.style.top = "0px";
      flipper.style.transformOrigin = "50% 100%";
      front.append(piece(cur, "top"));
      back.append(piece(other, "bottom"));
    }
    // inside a face, the piece sits at the face's own origin
    for (const f of [front, back]) { f.firstChild.style.top = "0px"; f.append(f === front ? shadeFront : shadeBack); }
    under.append(shadeUnder);
    layer.append(still, under, flipper);
    flipper.append(front, back);
    const line = el("div", "fold-line"); line.style.top = half + "px"; layer.append(line);
    stage.append(layer);
    cur.classList.remove("current"); // the layer now draws everything

    return {
      dir, other, layer,
      set(angle) { // 0 .. 180
        const a = Math.max(0, Math.min(180, angle));
        flipper.style.transform = `rotateX(${dir > 0 ? a : -a}deg)`;
        shadeFront.style.opacity = a < 90 ? (a / 90) * 0.45 : 0;
        shadeBack.style.opacity = a >= 90 ? ((180 - a) / 90) * 0.45 : 0;
        shadeUnder.style.opacity = (1 - a / 180) * 0.35;
        this.angle = a;
      },
      angle: 0,
    };
  },

  animate(flip, to, done) {
    const from = flip.angle;
    const dur = Math.max(140, Math.abs(to - from) * 2.6);
    const t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - t, 3);
      flip.set(from + (to - from) * e);
      if (t < 1) requestAnimationFrame(step); else done();
    };
    requestAnimationFrame(step);
  },

  finish(flip, complete) {
    this.busy = true;
    const target = complete && flip.other ? 180 : 0;
    this.animate(flip, target, () => {
      flip.layer.remove();
      this.setCurrent(target === 180 ? this.cur + flip.dir : this.cur);
      this.busy = false;
      if (target === 180) { $("#flipHint").hidden = true; localStorageSet("flipHintSeen", "1"); }
    });
  },

  go(dir) {
    if (this.busy || !this.pages[this.cur + dir]) return;
    this.finish(this.beginFlip(dir), true);
  },
};

/* gestures: drag to fold, tap to open */
(function gestures() {
  const stage = $("#stage");
  let startY = 0, startX = 0, t0 = 0, flip = null, moved = false, lastY = 0, lastT = 0, vel = 0, pid = null;

  stage.addEventListener("pointerdown", (e) => {
    if (pid !== null) return;
    pid = e.pointerId;
    startY = lastY = e.clientY; startX = e.clientX; t0 = lastT = performance.now();
    moved = false; flip = null; vel = 0;
  });
  stage.addEventListener("pointermove", (e) => {
    if (e.pointerId !== pid) return;
    if (Reader.busy) {
      // previous page still turning: follow the finger so the new flip starts from where it is now
      if (Math.abs(e.clientY - startY) > 10) moved = true;
      startY = lastY = e.clientY; startX = e.clientX;
      return;
    }
    const dy = e.clientY - startY;
    if (!flip) {
      if (Math.abs(dy) < 10 || Math.abs(dy) < Math.abs(e.clientX - startX)) return;
      moved = true;
      try { stage.setPointerCapture(pid); } catch (_) {}
      flip = Reader.beginFlip(dy < 0 ? 1 : -1);
      if (!flip) return;
    }
    const now = performance.now();
    vel = (e.clientY - lastY) / Math.max(1, now - lastT);
    lastY = e.clientY; lastT = now;
    const span = stage.clientHeight * 0.55;
    let angle = (Math.abs(dy) / span) * 180;
    if ((flip.dir > 0 && dy > 0) || (flip.dir < 0 && dy < 0)) angle = 0; // finger reversed past start
    if (!flip.other) angle = Math.min(angle * 0.25, 28);                  // nothing there: rubber band
    flip.set(angle);
  });
  const end = (e) => {
    if (e.pointerId !== pid) return;
    pid = null;
    if (!flip) return; // a tap — handled by the click listener below
    const flick = flip.dir > 0 ? vel < -0.45 : vel > 0.45;
    Reader.finish(flip, flip.angle > 90 || flick);
    flip = null;
  };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", (e) => { if (flip) { Reader.finish(flip, false); flip = null; } pid = null; });

  // a tap opens whichever story was touched (a real click, so iOS allows the new tab)
  stage.addEventListener("click", (e) => {
    if (moved || Reader.busy || performance.now() - t0 > 700 || e.target.closest("button")) return;
    const target = e.target.closest("[data-url]");
    if (target) openStory({ url: target.dataset.url });
  });

  // desktop conveniences
  let wheelLock = 0;
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (performance.now() < wheelLock || Math.abs(e.deltaY) < 12) return;
    wheelLock = performance.now() + 450;
    Reader.go(e.deltaY > 0 ? 1 : -1);
  }, { passive: false });
  document.addEventListener("keydown", (e) => {
    if (views.reader.hidden) return;
    if (["ArrowDown", "ArrowRight", "PageDown", " "].includes(e.key)) { e.preventDefault(); Reader.go(1); }
    if (["ArrowUp", "ArrowLeft", "PageUp"].includes(e.key)) { e.preventDefault(); Reader.go(-1); }
    if (e.key === "Escape") location.hash = "#/";
  });
  window.addEventListener("resize", () => { if (!views.reader.hidden) Reader.layout(); });
})();

$("#backBtn").onclick = () => { location.hash = "#/"; };

/* =========================================================
   "Fewer like this" sheet (the ··· button on a story)
   ========================================================= */
const Sheet = {
  el: null,
  open(story) {
    this.close();
    const sec = story.section_name || "this section";
    const wrap = el("div", "sheet-wrap");
    wrap.onclick = (e) => { if (e.target === wrap) this.close(); };
    const sheet = el("div", "sheet");
    sheet.append(el("div", "sheet-title", "Fewer like this"),
                 el("div", "sheet-story", story.title));
    const opts = [];
    for (const t of (story.tags || []).slice(0, 5)) {
      opts.push({ label: `Hide “${t.name}” stories in ${sec}`,
                  body: { section_id: story.section_id, kind: "tag", value: t.slug, label: `#${t.name} stories in ${sec}` } });
    }
    if (story.domain) {
      opts.push({ label: `Hide ${story.domain} in ${sec}`,
                  body: { section_id: story.section_id, kind: "domain", value: story.domain, label: `${story.domain} in ${sec}` } });
      opts.push({ label: `Hide ${story.domain} everywhere`,
                  body: { section_id: null, kind: "domain", value: story.domain, label: `${story.domain} everywhere` } });
    }
    for (const o of opts) {
      const b = el("button", "sheet-btn", o.label);
      b.onclick = async () => {
        b.disabled = true;
        try { await send("/api/mutes", "POST", o.body); this.close(); Reader.reload(); }
        catch (e) { b.disabled = false; b.textContent = e.message; }
      };
      sheet.append(b);
    }
    const cancel = el("button", "sheet-btn cancel", "Cancel");
    cancel.onclick = () => this.close();
    sheet.append(cancel, el("p", "sheet-help", "Change your mind later under Settings → Hidden."));
    wrap.append(sheet);
    document.body.append(wrap);
    this.el = wrap;
  },
  close() { if (this.el) { this.el.remove(); this.el = null; } },
};

function localStorageGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }

/* =========================================================
   Settings
   ========================================================= */
const Settings = {
  data: null,
  async load() {
    try { this.data = await api("/api/settings"); }
    catch (e) { $("#sectionList").replaceChildren(el("p", "status err", e.message)); return; }
    this.render();
  },
  render() {
    const secs = this.data.sections;
    const sel = $("#addSection");
    const prev = sel.value;
    sel.replaceChildren(...secs.map(s => { const o = el("option", null, s.name); o.value = s.id; return o; }));
    if (prev && secs.some(s => String(s.id) === prev)) sel.value = prev;

    const list = $("#sectionList");
    list.replaceChildren();
    secs.forEach((s, idx) => {
      const box = el("div", "sec");
      const head = el("div", "sec-head");
      head.append(el("span", "name", s.name));
      if (s.discover) head.append(el("span", "tag", "DISCOVER"));
      box.append(head);

      const acts = el("div", "sec-actions");
      const btn = (label, fn, disabled) => { const b = el("button", null, label); b.disabled = !!disabled; b.onclick = fn; acts.append(b); };
      btn("↑", () => this.patchSection(s.id, { move: -1 }), idx === 0);
      btn("↓", () => this.patchSection(s.id, { move: 1 }), idx === secs.length - 1);
      btn("Rename", () => { const n = prompt("New name for this section:", s.name); if (n && n.trim()) this.patchSection(s.id, { name: n.trim() }); });
      btn(s.discover ? "Make normal" : "Make Discover", () => this.patchSection(s.id, { discover: !s.discover }));
      btn("Delete", () => {
        if (!confirm(`Delete "${s.name}" and its ${s.feeds.length} feed(s)?`)) return;
        this.act(() => send(`/api/sections/${s.id}`, "DELETE"));
      });
      box.append(acts);

      if (!s.feeds.length) box.append(el("div", "no-feeds", "No feeds yet — add some above."));
      for (const f of s.feeds) {
        const row = el("div", "feed");
        const dot = el("span", "dot" + (f.last_error ? " bad" : f.last_ok ? " ok" : ""));
        const main = el("div", "f-main");
        main.append(el("div", "f-title", f.title));
        if (f.last_error) main.append(el("div", "f-err", "Problem: " + f.last_error));
        const mv = el("select");
        mv.title = "Move to section";
        for (const t of secs) { const o = el("option", null, t.name); o.value = t.id; if (t.id === s.id) o.selected = true; mv.append(o); }
        mv.onchange = () => this.act(() => send(`/api/feeds/${f.id}`, "PATCH", { section_id: +mv.value }));
        const rm = el("button", "rm", "×");
        rm.setAttribute("aria-label", "Remove feed");
        rm.onclick = () => {
          if (!confirm(`Stop following "${f.title}"?`)) return;
          this.act(() => send(`/api/feeds/${f.id}`, "DELETE"));
        };
        row.append(dot, main, mv, rm);
        box.append(row);
      }
      list.append(box);
    });
    // things hidden via "fewer like this"
    const mutes = this.data.mutes || [];
    $("#hiddenBox").hidden = !mutes.length;
    const ml = $("#muteList");
    ml.replaceChildren();
    for (const m of mutes) {
      const row = el("div", "feed");
      row.append(el("div", "f-main", m.label));
      const rm = el("button", "rm", "×");
      rm.setAttribute("aria-label", "Show again");
      rm.onclick = () => this.act(() => send(`/api/mutes/${m.id}`, "DELETE"));
      row.append(rm);
      ml.append(row);
    }

    const lr = this.data.last_refresh;
    $("#lastRefresh").textContent = lr
      ? `Stories last checked ${ago(lr)} ago · checks every ${this.data.refresh_minutes} min`
      : "First check for stories is running…";
  },
  patchSection(id, body) {
    return this.act(() => send(`/api/sections/${id}`, "PATCH", body));
  },
  /* run a change, show its error (if any) above the list, and redraw so the list matches the server */
  async act(fn) {
    const st = $("#listStatus");
    try { await fn(); st.textContent = ""; }
    catch (e) { st.textContent = e.message; }
    this.load();
  },
};

const placeholders = {
  url: "Paste a link — website, feed, or Flipboard magazine",
  flipboard: "Topic name, e.g. golf",
  google: "Search words, e.g. Detroit Lions",
};
$("#addKind").onchange = () => { $("#addValue").placeholder = placeholders[$("#addKind").value]; };
$("#addBtn").onclick = async () => {
  const st = $("#addStatus"), b = $("#addBtn");
  st.className = "status"; st.textContent = "Checking…"; b.disabled = true;
  try {
    const r = await send("/api/feeds", "POST", {
      kind: $("#addKind").value, value: $("#addValue").value, section_id: +$("#addSection").value,
    });
    st.className = "status ok"; st.textContent = `Added: ${r.title}`;
    $("#addValue").value = "";
    Settings.load();
  } catch (e) { st.className = "status err"; st.textContent = e.message; }
  b.disabled = false;
};
$("#newSectionBtn").onclick = async () => {
  const st = $("#newSectionStatus");
  try {
    await send("/api/sections", "POST", { name: $("#newSection").value, discover: $("#newSectionDiscover").checked });
    st.className = "status ok"; st.textContent = "Section created.";
    $("#newSection").value = ""; $("#newSectionDiscover").checked = false;
    Settings.load();
  } catch (e) { st.className = "status err"; st.textContent = e.message; }
};
$("#importBtn").onclick = async () => {
  const st = $("#importStatus"), file = $("#opmlFile").files[0];
  if (!file) { st.className = "status err"; st.textContent = "Choose your NewsBlur file first."; return; }
  const fd = new FormData(); fd.append("file", file);
  st.className = "status"; st.textContent = "Importing…";
  try {
    const r = await api("/api/import", { method: "POST", body: fd });
    st.className = "status ok";
    st.textContent = `Found ${r.found} sites, added ${r.added} new. Stories will appear over the next few minutes.`;
    Settings.load();
  } catch (e) { st.className = "status err"; st.textContent = e.message; }
};
$("#settingsBack").onclick = () => { location.hash = "#/"; };

/* ========================================================= */
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
// coming back to the app after a while: refresh the home grid (which also picks up new versions)
let hiddenAt = 0;
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { hiddenAt = Date.now(); return; }
  if (Date.now() - hiddenAt > 5 * 60 * 1000 && !views.home.hidden) Home.load();
});
route();
