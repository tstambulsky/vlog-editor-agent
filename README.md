# auto-vlog-editor

**An AI agent that turns your daily phone clips into a polished, narrated vertical
video — reviewed and approved over Telegram, then auto-published to X (LinkedIn optional).**

Drop the day's clips into a Google Drive folder, optionally dictate a voice note
about what happened, and the agent does the rest: it watches the footage, writes a
story, narrates it in your own cloned voice, renders a captioned vertical video, and
sends it to you for one-tap approval. Nothing is published until you say so.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12-blue.svg)
![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)

## See it in action

https://github.com/user-attachments/assets/d0f4fbef-02e8-4227-ab9a-4e86d4dbbcb1

A real result — **Day 9**, generated end-to-end by the agent and posted to X.
**▶️ [Watch it on X](https://x.com/tomasstambulsky/status/2068344665120678386)** · built by **[@tomasstambulsky](https://x.com/tomasstambulsky)**, who runs the series daily with this tool. *(video not loading? [GIF preview](docs/assets/day9-demo.gif) · [download the clip](docs/assets/day9.mp4))*

## Why

Keeping up a daily video series by hand is a grind: cut the clips, write a script,
record a voiceover, add captions, write platform captions, post on schedule. This
agent automates the whole loop while keeping a human in control of the one step that
matters — what actually goes out.

## Features

- 🎬 **Clips → finished reel.** Ingests a day's clips, picks the moments, and edits a
  vertical (1080×1920) video with music-free narration.
- 🧠 **Claude writes the story.** A Claude Code agent watches the footage (keyframes +
  transcripts), then drafts a theme, an edit list, the narration, and the social
  captions.
- 🗣️ **Your cloned voice.** Narration is generated with ElevenLabs TTS using a voice
  you choose or clone.
- 💬 **Word-accurate captions.** faster-whisper + wav2vec2 forced alignment time each
  word; the on-screen text always comes from the script, never the (mis-heard) ASR.
- 📲 **Telegram approval flow.** Review the script first (cheap), then the rendered
  video; reply with feedback to revise, or tap **Publish**.
- ✍️ **No-slop pass.** Every fresh draft is rewritten to strip AI-writing tells, then
  scored /50 by an independent reviewer (plus a mechanical lint for dashes, emoji,
  closing questions…) and rewritten until it clears the bar. Your voice lives in one
  editable file, [`prompts/writing/x-voice.md`](prompts/writing/x-voice.md).
- 🧠 **It learns from your corrections.** Every script or tweet edit you send is stored
  and fed back into the next drafts, so the same mistake doesn't come back.
- ✏️ **Edit just the tweet.** Rewrite the X caption from Telegram without re-rendering.
- 🕐 **Wrong-clock clips.** Clips whose camera clock is off get flagged at script
  review; `/retime` pins the real time so the story stays in order.
- 💾 **Nightly DB backup** to `data/backups/` and your Drive.
- 🪝 **Cold-open hooks, shoutouts, sign-off footer** — optional, toggleable.
- 🌎 **Optional bilingual.** Off by default (single-language). Flip `BILINGUAL=true`
  to also produce a second-language voiceover, video, and captions, and post one
  language per platform.
- ✅ **Approval-gated publishing.** Only a day you explicitly approve can ever post.
- 🐳 **One-container deploy.** Bot + scheduler + read-only dashboard via Docker Compose.

## How a day works

1. **Record & drop clips** into the day's Google Drive folder
   (`DRIVE_REMOTE/YYYY-MM-DD/`, created automatically each night).
2. **Add context** (optional): send the Telegram bot a text or voice note about the day.
3. **The pipeline runs** (scheduled, or on `/makeit`):

   ```
   rclone sync ──► analyze (ffprobe · keyframes · whisper)
        │
        ▼
   Claude drafts: theme · edit list · script · captions
        │
        ▼
   ┌───────────────── SCRIPT_REVIEW gate ─────────────────┐
   │  bot sends the script → you approve or reply to revise │
   └───────────────────────────────────────────────────────┘
        │ (Approve & build)
        ▼
   ElevenLabs voiceover ──► forced-align words ──► ffmpeg render (HDR tone-map · burn subs · prepend hook)
        │
        ▼
   ┌──────────────── AWAITING_APPROVAL gate ───────────────┐
   │  bot sends the video → Publish now / Publish at 08:00  │
   └───────────────────────────────────────────────────────┘
        │ (approved)
        ▼
   Publish to X (+ LinkedIn if enabled) ──► report post URLs
   ```

4. **Approve or revise.** Reply with feedback ("less touristy, more founder-oriented")
   to re-draft, or tap **Publish 8AM** / **Publish now**.

See [`docs/architecture/pipeline.md`](docs/architecture/pipeline.md) for the full
state machine and beat-sync design.

## Quickstart

### Prerequisites

- A **Linux server** with **Docker** + **Docker Compose** (≈4 cores / 8GB RAM
  recommended; ffmpeg + forced-alignment are the heavy parts).
- The **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)**, installed and
  logged in on the host (subscription `claude /login`, or an API key). This is the
  reasoning layer.
- **[ElevenLabs](https://elevenlabs.io)** account + a voice id (premade or your own clone).
- **[Zernio](https://zernio.com)** account with X and/or LinkedIn connected (the
  publishing API), plus the per-platform account ids.
- A **Telegram bot** token (from [@BotFather](https://t.me/BotFather)) and your numeric user id.
- A **Google Drive** remote configured via [rclone](https://rclone.org).

### Setup

```bash
git clone https://github.com/<you>/auto-vlog-editor.git
cd auto-vlog-editor
cp .env.example .env        # then fill it in (see Configuration below)

# Configure the Google Drive remote (named to match DRIVE_REMOTE)
docker compose run --rm auto-vlog rclone config

# Verify the reasoning layer works headless
claude -p 'Reply with {"ok":true}' --output-format json

# Launch the bot + scheduler + dashboard
docker compose up -d --build
```

Message your bot once so it can reach you, then drop clips into
`DRIVE_REMOTE/YYYY-MM-DD/` and send `/makeit`. Full deployment notes:
[`docs/operations/deploy.md`](docs/operations/deploy.md).

### ⚡ One-paste setup (with Claude Code)

Don't want to wire it up by hand? Paste this prompt into
[Claude Code](https://docs.claude.com/en/docs/claude-code) (or your agent of choice)
in an empty folder and it will walk you through the whole setup:

```text
Set up "auto-vlog-editor" (https://github.com/tstambulsky/auto-vlog-editor) for me —
an AI agent that turns my daily phone clips into a narrated vertical video and posts it to X.

Work with me one step at a time, asking for each credential only when it's needed:
1. Clone the repo and run `cp .env.example .env`.
2. Walk me through filling .env:
   - a Telegram bot token (from @BotFather) and my numeric Telegram user id,
   - an ElevenLabs API key and a voice id (premade, or clone my own),
   - a Zernio API key and my X account id,
   - a Google Drive remote via `rclone config` (match DRIVE_REMOTE).
3. Make sure the Claude Code CLI is installed and logged in — it's the reasoning layer.
4. Bring it up with `docker compose up -d --build` and confirm the bot messages me.
5. Show me how to drop clips into the day's Drive folder and run /makeit.
Keep all my keys out of git — they live only in .env. Explain each step briefly as we go.
```

## Configuration

All configuration is environment variables (see [`.env.example`](.env.example)).

| Variable | What it does |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID` | Bot token + the single user allowed to control it |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL_ID` | TTS credentials + voice |
| `VOICE_STABILITY` / `VOICE_STYLE` / `VOICE_SIMILARITY` / `VOICE_SPEED` | Voice delivery knobs |
| `ZERNIO_API_KEY`, `ZERNIO_X_ACCOUNT_ID`, `ZERNIO_LINKEDIN_ACCOUNT_ID` | Publishing credentials |
| `PUBLISH_LANGS` | Language per platform, e.g. `twitter:en,linkedin:es` |
| `CLAUDE_CODE_COMMAND`, `CLAUDE_BIN`, `CLAUDE_TIMEOUT_S` | Reasoning-layer command, host binary path, per-call timeout |
| `DRIVE_REMOTE` | rclone `remote:path` root for day folders |
| `TRIP_START_DATE`, `TIMEZONE` | Day numbering + scheduling timezone (scheduled jobs stay quiet outside days 1..`TRIP_DAYS`) |
| `TRIP_PLACE`, `TRIP_DAYS` | Fill the series bible and the `day N/M in <place>` tweet line |
| `TRIP_CONTEXT` | Optional private free text for the agent: what the trip is, who to tag |
| `REVIEW_ROUNDS`, `REVIEW_MIN_SCORE`, `TWEET_LOWERCASE` | No-slop reviewer loop (rounds, pass mark /50) + optional all-lowercase tweet lint |
| `PIPELINE_HOUR/MINUTE`, `PUBLISH_HOUR/MINUTE`, `SYNC_INTERVAL_MIN` | Schedule |
| `AUTO_PIPELINE` | `false` = nightly check-in instead of auto-build |
| `SCRIPT_REVIEW` | Review the script before spending a render |
| `BILINGUAL` | **Off by default.** `true` also produces a second-language voiceover + video + captions |
| `FIRST_FRAME_HOOK`, `BURN_SUBTITLES`, `HUMANIZE`, `INCLUDE_PHOTOS` | Feature toggles |
| `MAX_VIDEO_SECONDS`, `WHISPER_MODEL` | Render cap + ASR model size |
| `DASHBOARD_PASSWORD`, `DASHBOARD_PORT` | Read-only web dashboard (Basic auth) |

## Bot commands

| Command | Effect |
|---|---|
| `/makeit` | Build today's draft now |
| `/status` | Day number, state, clip count, post URLs |
| `/note <text>` | Add context for today's story |
| `/shoutout @a @b` | Set today's X shoutout handles |
| `/redo` | Re-draft from scratch |
| `/retime` | Fix the time of a clip whose camera clock was wrong |
| `/menu`, `/context`, `/folder`, `/preview` | Inline menu, accumulated context, Drive link, resend latest draft |

Any plain text or voice note becomes a revision request while a draft is awaiting
approval; otherwise it's saved as today's note. See
[`docs/integrations/telegram.md`](docs/integrations/telegram.md).

## Make it your own series

The agent is series-agnostic. To run your own:

- Set `TRIP_PLACE`, `TRIP_DAYS`, `TRIP_START_DATE` (Day 1) and `TIMEZONE`; put anything
  private about the trip in `TRIP_CONTEXT` (`.env` only).
- Edit [`prompts/editorial.md`](prompts/editorial.md), the "series bible": who you are,
  who it's for, what the series is about.
- Edit [`prompts/writing/x-voice.md`](prompts/writing/x-voice.md), your house writing
  rules and tweet format. It overrides the generic anti-slop rules.
- Pick a voice (`ELEVENLABS_VOICE_ID`) and tune the `VOICE_*` settings; `scripts/voice_test.py` helps you A/B.
- Choose your languages with `PUBLISH_LANGS` and `BILINGUAL`.

## Project layout

```
app/
  config.py        env settings
  db.py            SQLite state (days, clips, drafts, posts, events)
  schemas.py       Draft/EDL contracts between reasoning and rendering
  claude_cli.py    the ONLY module that invokes the Claude Code CLI
  prompts.py       prompt builders (+ prompts/editorial.md series bible)
  pipeline.py      idempotent stages + revision diffing
  publishing.py    approval-gated publishing
  bot.py           Telegram approval flow
  scheduler.py     nightly pipeline, folder creation, sync & post polling
  services/        drive (rclone) · analyze (ffprobe/whisper) · voiceover
                   (ElevenLabs) · align · subtitles (ASS) · render (ffmpeg) · zernio
docs/              architecture, integrations, operations, design decisions
prompts/           editorial series bible + humanizer skill
scripts/           one-off runners (run_day, regen_vo, rerender, voice_test, smoke tests)
```

## Running tests

```bash
docker build --target dev -t auto-vlog-dev . && docker run --rm auto-vlog-dev
```

The `dev` image has no torch, so it exercises the whisper alignment fallback.

## Tech stack & acknowledgements

- **[Claude](https://www.anthropic.com/claude) / [Claude Code](https://docs.claude.com/en/docs/claude-code)** — the reasoning layer (drafting, revisions, humanizing)
- **[ElevenLabs](https://elevenlabs.io)** — text-to-speech voiceover
- **[Zernio](https://zernio.com)** — social publishing API
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** + **[WhisperX](https://github.com/m-bain/whisperX)** — transcription & word-level forced alignment
- **[ffmpeg](https://ffmpeg.org)** — rendering, HDR tone-mapping, subtitle burn-in
- **[rclone](https://rclone.org)** — Google Drive sync
- Narration de-AI-ification uses [`blader/humanizer`](https://github.com/blader/humanizer) and [`hardikpandya/stop-slop`](https://github.com/hardikpandya/stop-slop) (both MIT), vendored in [`prompts/writing/`](prompts/writing/).

## License

[MIT](LICENSE) © Tomás Stambulsky
