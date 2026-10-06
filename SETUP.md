# Flip Reader — deployment notes (for Josh)

A small self-hosted news reader for Chris's phone: pulls his RSS feeds plus public
Flipboard magazine/topic feeds and Google News searches, and presents them as
Flipboard-style flip pages. Single Python/Flask container, SQLite storage,
no browser automation, no outbound ports needed other than normal HTTPS.

Same pattern as the Tigers Den monitor: Docker on Proxmox, exposed through your
Cloudflare Tunnel.

## 1. Run it

The container is built automatically from Chris's GitHub repo and published as
`ghcr.io/<chris's-github-name>/flip-reader:latest` (the exact name is in
`docker-compose.yml`). You only need the `docker-compose.yml` and `.env` from this
folder — no source, no build step.

```bash
cp .env.example .env         # put Chris's APP_TOKEN in here (he'll send it)
docker compose pull
docker compose up -d
docker compose logs -f       # first line should be "refreshed N feeds, M new articles"
curl -s http://127.0.0.1:8090/api/health
```

If `pull` is refused, the package on GitHub is still private — Chris flips it to
public under the repo's Packages settings (or sends you a read-only token).

The container listens on `127.0.0.1:8090`. `./data/` holds `reader.db` — his feed list
and story cache. **Keep it between rebuilds** (it's a bind mount, so `docker compose up
--build` preserves it). Losing it only means he re-imports his feed list; no big deal.

## 2. Cloudflare Tunnel

Add an ingress rule pointing a hostname (suggest `news.elusive.net`, or whatever you
prefer) at `http://127.0.0.1:8090`. Nothing else special — the app is self-contained
and serves everything (including its icons) from that one origin.

Auth is two layers, both optional but recommended:

- **Cloudflare Access** in front, like the Tigers dashboard. Note: iOS "Add to Home
  Screen" apps keep Access cookies fine, but Access sessions do expire and he'd have to
  re-auth in the app window. If that gets annoying, a long Access session duration
  (e.g. 1 month) helps.
- **APP_TOKEN** — the app's own access code. Entering it once sets a 400-day cookie on
  his phone. If you skip Cloudflare Access, keep this set; if `APP_TOKEN` is empty the
  app is open to anyone who finds the URL.

No paths need to bypass Access — the phone app talks only to `/api/*` on the same
hostname from inside the authenticated page.

## 3. Resource use

Fetches every `REFRESH_MINUTES` (default 20). ~15 feeds → a few hundred KB per refresh,
plus up to 60 article-page peeks per run to find pictures for stories that don't ship
one. Memory ~80 MB. Stories older than 10 days or beyond 150 per feed are pruned
automatically, so the database stays small (tens of MB).

## 4. Updating

Automatic, if Watchtower runs on the host: the container carries the
`com.centurylinklabs.watchtower.enable=true` label, so Watchtower (with
`--label-enable`) swaps in each new build within its check interval. `data/` is a
bind mount and survives every update. If you don't run Watchtower, there's a
commented-out service for it in `docker-compose.yml`, or update by hand:

```bash
docker compose pull && docker compose up -d
```

## 5. Troubleshooting

- Blank sections / "No stories yet": wait one refresh cycle; check logs for
  `fetcher WARNING feed ... failed` lines — those feeds show a red dot in his Settings.
- `/api/health` returns `last_refresh: null` for more than ~10 min → fetcher thread died;
  `docker compose restart`.
- Everything is logged to stdout.
