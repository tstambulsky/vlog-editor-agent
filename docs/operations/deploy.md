# Deployment guide

This project ships as a Docker Compose stack. Docker is the primary (and
recommended) way to run it; a systemd fallback is documented at the end.

## Requirements

- A Linux server with **Docker** and **Docker Compose** installed.
- Enough CPU/RAM for the render pipeline. A render runs `ffmpeg` plus the
  faster-whisper/WhisperX forced-alignment stack, which is CPU-heavy. Plan for
  **at least 4 cores and ~8GB RAM**; 2-core/low-memory boxes can swap-thrash
  during a render. torch is **CPU-only** in the prod image (no GPU needed).
- **Disk for footage.** Raw video is the bulk of the storage (≈1-3GB per day
  for 4K/HDR clips). On first run the stack downloads **~2GB of models**
  (faster-whisper + wav2vec2 alignment models) into a cache volume. Keep an eye
  on free space and enable the prune job below.

## Deploy with Docker Compose

The public repo ships a single `docker-compose.yml`. From the repo root
(`<INSTALL_DIR>`):

```bash
# first deploy
docker compose up -d --build

# update to a new version
git pull && docker compose up -d --build --force-recreate
```

`--force-recreate` matters after `.env` changes — Compose won't reload the
environment otherwise.

## Configuration & state

### Secrets (`.env`)

Copy `.env.example` to `.env` and fill it in. Keep `.env` out of version control
(it's git-ignored). Edit it on the server; `--force-recreate` picks up changes.

### Claude Code CLI (reasoning layer)

The Claude Code CLI is the swappable reasoning layer for the whole pipeline. It
must be **installed and logged in** on the host (subscription login or an API
key) and reachable as the binary named by `CLAUDE_CODE_COMMAND` (default
`claude`). The CLI binary and its login state are **bind-mounted into the
container** (see `docker-compose.yml`). Verify it works headless before the
first run:

```bash
claude -p 'Reply with {"ok":true}' --output-format json
```

### Google Drive (rclone)

Footage is pulled from Google Drive via `rclone`, which runs inside the
container. Configure an rclone remote named to match your `.env` (the examples
use `gdrive:VideoJournal`) and bind-mount your `rclone.conf` into the container.
Verify:

```bash
docker compose run --rm --no-deps auto-vlog rclone lsd gdrive:VideoJournal
```

### State (`./data`)

The `./data` volume holds all persistent state:

- the SQLite database, and
- per-day footage / work / output (`data/days/<date>/{raw,work,output,...}`).

A sibling cache volume holds the downloaded whisper + wav2vec2 models so they
don't re-download on every container rebuild.

## Pre-warming alignment models (optional)

After a fresh deploy the alignment models download lazily on the first
voiceover. To avoid the first scheduled run paying that cost, pre-warm them into
the cache volume:

```bash
docker compose run --rm --no-deps auto-vlog python -c \
  "import whisperx; whisperx.load_align_model(language_code='en',device='cpu'); \
   whisperx.load_align_model(language_code='es',device='cpu')"
```

## Docker image layout

`python:3.12-slim` base (`ffmpeg` 7.x with libzimg — needed for HDR
tone-mapping), plus `rclone` and DejaVu fonts. Multi-stage:

- `base` → `prod` (what Compose runs, `target: prod` pinned) adds the CPU torch +
  WhisperX forced-alignment stack used for subtitle/beat timing.
- `dev` is `FROM base` (no torch) and adds pytest + tests, so the test image runs
  the whisper fallback.

Volumes: `./data:/data`, the model cache (`huggingface` + `TORCH_HOME`), the
rclone config, and the Claude CLI binary plus its login state.

## Runner scripts (on the server)

All run as one-off containers sharing `/data` with the live bot — safe because
stages are idempotent and SQLite handles multi-process access:

```bash
docker compose run --rm --no-deps -e PYTHONPATH=/srv/app auto-vlog python scripts/<name>.py [args]
```

| Script | Purpose |
|---|---|
| `run_day.py [date]` | full pipeline + Telegram preview (same path as /makeit) |
| `regen_vo.py [date]` | fresh TTS takes (current voice settings) + re-render |
| `rerender.py [date]` | re-render reusing existing voiceovers (after renderer fixes) |
| `voice_test.py [set] [date]` | send labeled voice A/B samples to Telegram |
| `smoke_zernio.py` | publish-path round-trip via a draft post (never publishes) |
| `smoke_draft.py` | synthetic clips → real Claude drafting call |
| `smoke_render.py` | synthetic clips → full render (local dev mostly) |

`PYTHONPATH=/srv/app` is required (`python scripts/x.py` puts `scripts/` on
sys.path, not the app root).

## Testing

```bash
docker build --target dev -t auto-vlog-dev . && docker run --rm -e DATA_DIR=/tmp/vj auto-vlog-dev          # pytest
docker run --rm -v "$PWD":/srv/app -e DATA_DIR=/tmp/vj -e PYTHONPATH=/srv/app auto-vlog-dev python scripts/smoke_render.py
```

Tests run in Docker only — no virtualenv needed.

pytest collection is scoped to `tests/` via `pytest.ini` (`testpaths`) —
without it, bare `pytest` tries to import `scripts/voice_test.py` (matches the
`*_test.py` pattern) and dies on `ModuleNotFoundError: app`.

## Disk management (optional pruning)

A 03:00 job prunes `raw/` + `work/frames` + `work/segments_*` for days in
`published`/`discarded`/`skipped` older than 2 days. Originals remain in Drive;
outputs/drafts/voiceovers are kept. Without this, weeks of footage
(~1-3GB/day) will exhaust disk on a small server.

## Scheduler (all in `America/Los_Angeles`, regardless of server TZ)

| When | Job |
|---|---|
| every 10 min | sync Drive day folder; poll pending Zernio posts |
| 22:30 | nightly pipeline, or check-in w/ build button if `AUTO_PIPELINE=false` |
| 22:00 | create tomorrow's Drive folder + notify |
| 03:00 | prune settled days |

## systemd fallback

If you'd rather not run under Docker (or the CLI ever fights the container), a
unit file is provided at `deploy/auto-vlog.service` to run the app from a
virtualenv on the host. Docker remains the primary, supported path.

See also: [pipeline.md](../architecture/pipeline.md) ·
[rendering.md](../architecture/rendering.md)
