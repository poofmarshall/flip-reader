# Flip Reader

Your own Flipboard-style reader: NewsBlur feeds + Flipboard magazines/topics + Google
News searches, in one app on your iPhone home screen.

## What it does

- **Home screen** — a grid of sections (like Flipboard's tiles). *For You* at the top
  mixes everything you follow, with every 4th story a Discover pick.
- **Flip pages** — tap a section, swipe up to flip through pages of 1–3 stories each.
  Tap a story to read it in Safari. Swipe down to go back a page.
- **Sections** — your NewsBlur folders become sections automatically. Rename, reorder,
  move feeds between them in Settings.
- **Discover** — a special kind of section that shows *only* stories from sources you
  don't already follow (from Flipboard topics and Google News searches). That's how you
  keep finding new things, like Flipboard does.
- **Sources you can add**
  - any website or RSS feed link
  - any public Flipboard magazine link (paste the magazine's web address)
  - any Flipboard topic by name ("golf", "detroit", "home automation"…)
  - any Google News search ("Detroit Lions", "Michigan waterfront"…)

## Setting it up on your phone (once Josh has it running)

1. Open the address Josh gives you in Safari, enter your access code.
2. Tap the Share button → **Add to Home Screen**. It now opens full-screen like an app.
3. Tap ⚙︎ → **Import from NewsBlur** → choose the **`my-feeds.opml`** file Claude sent you. It
   holds your NewsBlur sites, your Flipboard follows and the Local News sources, already sorted
   into sections. (Get the file onto your phone first: AirDrop, iCloud Drive, or email it to
   yourself.) It's deliberately kept out of the GitHub repo since it's your personal list.
4. Give it a couple of minutes to gather stories, then flip.

If you later add sites in NewsBlur, you can re-import a fresh NewsBlur export the same way
(`newsblur.com/import/opml_export`); it only adds what's new.

## What you need to send Josh

- The link to the GitHub repo (he only needs `docker-compose.yml` and `.env.example` from it).
- An access code of your choosing (a long password-like string). That becomes `APP_TOKEN`.

## How updates work

The code lives on GitHub. Every change Claude pushes is tested and built into a container
automatically; Josh's server picks up new builds on its own (Watchtower). Nothing to send.

## Known limits

- Flipboard doesn't let outside apps read your personal Flipboard "For You" feed. We
  can only pull in *public* magazines and topics. Your own curation lives here instead.
- Google News stories don't come with pictures, so those show as text-only cards.
- A few sites (paywalled papers, etc.) refuse automated readers and can't be added
  directly. A Google News search for that paper's name usually works as a substitute.

## Running it on this PC (for testing)

```bash
python -m pip install -r requirements.txt
python app/server.py
```

Then open http://localhost:8090 . Data lands in `./data/`.
