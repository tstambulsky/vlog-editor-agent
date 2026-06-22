# Web dashboard (read-only)

A single-module aiohttp app (`app/web.py`) that gives a browser view of
everything the pipeline produces: trip calendar, per-day clips, drafts with
revision feedback, voiceovers, renders, posts, notes, and the event log.
**Strictly read-only** — approval and publishing stay in the Telegram bot, so
the single publish gate (`publishing.publish_day()`) is untouched.

Served on `DASHBOARD_PORT` (Basic auth — any username, password is
`DASHBOARD_PASSWORD` in the server `.env`).

## How it's served

- aiohttp ships with aiogram, so there are **no new dependencies**. The app
  runs inside the bot's event loop: `app/main.py` starts an `AppRunner` after
  the scheduler, wrapped in try/except so a dashboard failure can never take
  the bot down.
- **Disabled by default**: an empty `DASHBOARD_PASSWORD` means the server
  never starts (fail closed). `DASHBOARD_PORT` (default 8080) is the
  *container* listen port; map it to a host port in `docker-compose.yml`.
- **Exposure is your responsibility.** Basic auth is the only built-in
  protection, so don't put the raw port on the public internet. Expose and
  secure it behind your own reverse proxy, VPN, or firewall rules.

## Pages

- `GET /` — calendar: one card per day from `TRIP_START_DATE` through today
  (plus any DB days outside that range), with day number, clip count, theme,
  and a status badge colored by the 12 `DAY_STATUSES`.
- `GET /day/{date}` — everything about one day:
  - clips with keyframe thumbnails (`work/frames/{stem}/*.jpg`), duration,
    HDR flag, and a raw-file download link;
  - every draft version (newest first) with trigger, the user's revision
    feedback highlighted, EDL table (EN + ES narration), captions,
    confidence notes;
  - `<audio>` players for `vo_v{N}*.mp3`, `<video>` players for renders
    (preview preferred over the full-quality file);
  - posts table (platform, status, published URL), notes, event timeline.
- `GET /media/{path}` — serves files from `DATA_DIR` via `web.FileResponse`
  (native HTTP Range support, so video seeking works).

## Safety properties

- Basic auth middleware on every route, compared with `hmac.compare_digest`.
- Media route: path resolved and required to stay under `DATA_DIR`
  (`is_relative_to`), plus an extension allowlist (`.mp4 .mp3 .jpg .jpeg
  .png .json .md .mov`) — the SQLite file is not servable.
- All DB/filesystem strings pass through `html.escape`.

## Quirks & tolerances

- Raw iPhone `.MOV` (HEVC) won't play in browsers → shown as keyframes +
  size + download link, never a `<video>` tag. Photo clips show a "photo"
  label instead of a duration.
- Raw/intermediate files for settled days are pruned after 2 days — every
  section tolerates missing files ("raw file pruned") and missing DB rows.
- `draft_json` is parsed with plain `json.loads` + `.get()`, **not**
  `Draft.model_validate` — pre-beat-sync drafts (2026-06-10 v1-v3) fail the
  current schema.
- `app/web.py` must never call `pipeline.day_dirs()` — it mkdirs as a side
  effect and would resurrect pruned directories.

## Local testing

```bash
# seed synthetic data into a host dir, then run the dashboard standalone
docker run --rm -v "$PWD":/srv/app -v /tmp/vj-dash:/tmp/vj -e DATA_DIR=/tmp/vj \
  -e PYTHONPATH=/srv/app auto-vlog-dev python scripts/smoke_render.py
docker run -d --rm --name vj-web -p 8080:8080 -v "$PWD":/srv/app -v /tmp/vj-dash:/tmp/vj \
  -e DATA_DIR=/tmp/vj -e PYTHONPATH=/srv/app -e DASHBOARD_PASSWORD=test \
  auto-vlog-dev python -m app.web
curl -u x:test localhost:8080/        # 200; without -u → 401
```

Note `smoke_render.py` writes files but no DB rows — the day page will show
empty sections unless you seed `days`/`clips`/`drafts` rows too.

See also: [deploy.md](deploy.md) ·
[pipeline.md](../architecture/pipeline.md) ·
[telegram.md](../integrations/telegram.md)
