# Pipeline architecture

One Python service (aiogram bot + APScheduler + pipeline) in Docker.
Runs on a Linux server with Docker. SQLite holds state; every stage is idempotent (checks its output
artifact + DB status), so re-running after a crash resumes where it left off.
The pipeline is Telegram-free by design — the bot and scheduler drive it and
own all messaging via `app/notify.py` (callback registered in `main.py`).

## Daily flow

```
upload to Drive ── sync_poll (10 min) ──> raw/ + Telegram confirmation
                   (videos AND photos: jpg/png/heic are synced too)
18:00 PT → send today's Drive folder link to Telegram (reminder to upload clips)
22:30 PT check-in (AUTO_PIPELINE=false → yes/no build button; true → auto)
  stage_sync      rclone copy --min-age 2m  (skips half-uploaded files)
  stage_analyze   HEIC→JPEG · ffprobe meta + HDR flag · keyframes (1/2s, ≤12)
                  · photos: probe EXIF, the photo itself is its keyframe
                  · faster-whisper transcript (skipped if clip has no audio)
  stage_draft     ONE Claude Code call reads frames + transcripts + notes
                  + trip memory → Draft JSON (beats, captions, both languages,
                  + 1-2s cold-open hook candidates if FIRST_FRAME_HOOK)
  SCRIPT_REVIEW=true (default) → STOP at script_review, send the script to
                  Telegram beat-by-beat (clip + local capture time, EN+ES) +
                  the hook picker (pick the cold-open). Voiceover/render wait for
                  "✅ Approve & build". Free text here = SCRIPT feedback (revise
                  via stage_draft, NO render).
                  SCRIPT_REVIEW=false → fall straight through to render below.
  per language (en, then es):  [on approval, or immediately if gate off]
    stage_voiceover  ElevenLabs → mp3; WhisperX force-align script → ASS subs
    stage_render     beat-timed segments → concat → VO-only audio → final mp4
  status → awaiting_approval → bot sends preview (both videos + buttons)
approval → publishing.publish_day() → Zernio: EN and ES are SEPARATE posts
           per platform (X + LinkedIn), now or next 08:00 PT
post_poll (10 min) → published URL back to Telegram
22:00 PT → create tomorrow's Drive folder (+ link sent to Telegram)
03:00 PT → prune raw/ + intermediates of settled days older than 2 days
```

## The beat-sync contract (core design)

What the narration says must match what's on screen. This is guaranteed
structurally, not by prompting alone:

- A `Draft` is a list of **beats** (`EdlEntry`): `clip`, `start_s`,
  `narration` (EN), `narration_es`. The full script is **computed** by joining
  the narrations (`Draft.voiceover_script` is a pydantic computed field).
- After TTS, `app/services/align.py` force-aligns the **known script** to the
  generated mp3 with WhisperX (wav2vec2) → tight word timestamps on the actual
  audio (`words_v{N}_{lang}.json`). `voiceover.beat_durations_from_words()` maps
  the script to those words and each beat's image switches when the next
  narration's first word starts. Fallbacks: free-form faster-whisper transcript
  (timestamps drift 100-400ms/word — the old "out of sync" path), then
  ElevenLabs alignment, then word-count proportional. (Why forced alignment, not
  transcription: we have the exact text, so aligning it beats re-guessing it.
  Why not ElevenLabs alignment directly: it drifts ~1.6s vs its own audio — see
  CLAUDE.md. faster-whisper's loose timestamps were a real desync bug,
  2026-06-12.) See [rendering.md](rendering.md#word-timings--whisperx-forced-alignment).
- The renderer cuts each clip to exactly its beat duration. EN and ES get
  independently timed cuts (Spanish runs longer), so segments are per-language.
- If a clip is shorter than its narration, its last frame freezes (tpad clone).
- The video ends 0.3s after the last spoken word — silent tails are impossible
  by construction (this was a real bug: faster voice + footage-driven length
  left a 12s mute tail).

History: the first design had a free-form `voiceover_script` + independent
EDL with `end_s`. It produced good stories but narration/footage drift.
Replaced 2026-06-10. Old drafts in that shape fail validation (see CLAUDE.md).

## Photos as beats

Photos uploaded to the Drive folder become narrated stills. `stage_analyze`
converts HEIC/HEIF → JPEG first (`pillow-heif`; iPhones shoot HEIC and ffmpeg
can't decode it), then `probe_photo` reads EXIF dimensions/timestamp and marks
`ClipMeta.is_photo`. The photo itself is its only "keyframe" for Claude. In a
beat, a photo holds the screen for its narration's length with a Ken Burns
zoom (see [rendering.md](rendering.md)); the 2s-footage validation is skipped
for photos (`schemas.is_photo`, `PHOTO_EXTS`). Sync accepts jpg/png/heic
alongside video (`drive.MEDIA_EXTS`).

## State machine

`days.status`: `collecting → synced → analyzed → drafted → [script_review →]
rendered → awaiting_approval → approved → scheduled → published | discarded |
failed | skipped`. Only `approved`/`scheduled` can publish — enforced solely in
`publishing.publish_day()`.

`script_review` is a gate between `drafted` and the voiceover/render: present
only when `SCRIPT_REVIEW=true` (default). The day parks there with a saved
`Draft` and no rendered video; `run_pipeline` returns `(draft, version, None)`.
`pipeline.render_phase(d)` (triggered by the "✅ Approve & build" button) takes
the reviewed script forward to `rendered → awaiting_approval`; it does NOT
re-draft, so the EDL/clip mapping and chronological order baked in by
`stage_draft` carry straight to the render. `pipeline.revise_script(d, feedback)`
re-runs `stage_draft` (revision) and stays in `script_review` — no render. With
`SCRIPT_REVIEW=false` the day skips `script_review` entirely (old behavior).

Tables: `days`, `clips`, `drafts` (versioned, `draft_json` + per-language
video paths), `posts` (per platform), `events` (append-only audit log).
Schema migrations are idempotent `ALTER TABLE` attempts in `db.connect()`.

## Revisions (feedback loop)

Free text/voice note while a day is `awaiting_approval` → one Claude call
gets the current draft + the feedback and returns a full new Draft,
instructed to keep untouched fields verbatim. `apply_feedback()` then diffs
old vs new and regenerates only what changed:

| Changed | Regenerated |
|---|---|
| caption_* only | nothing (previous render reused) |
| narration (EN text) | EN voiceover + EN render |
| narration_es | ES voiceover + ES render |
| EDL structure (clips/starts) | both renders, voiceovers reused |

Unchanged voiceover artifacts are copied to the new version number
(`_copy_vo_artifacts`). Every revision bumps `drafts.version`; nothing is
overwritten destructively.

While a day is in `script_review` (gate on, pre-render), free text instead runs
`revise_script()`: the SAME `stage_draft` revision call, but it stops there —
re-shows the script, no voiceover/render. So script edits are cheap and inherit
the chronological EDL sort. The full `apply_feedback` table above applies only
after the video exists (`awaiting_approval`).

## Reasoning layer

All Claude Code use goes through `app/claude_cli.py`: headless `-p
--output-format json --max-turns 25 --allowedTools Read,Glob,Grep`, cwd
pinned to the day's `work/` dir, one call at a time (asyncio lock), pydantic
schema validation with one retry (the validation error is appended to the
prompt), full logs in `work/claude_logs/`. Command swappable via
`CLAUDE_CODE_COMMAND` (a thin `claude` wrapper).

Prompt inputs (`app/prompts.py`): the series bible
([prompts/editorial.md](../../prompts/editorial.md)), per-clip metadata +
frame paths (Claude Reads the JPEGs itself) + transcripts, the user's daily
notes, **trip-wide memory** (every previous day's theme) and the last 3
published scripts (anti-repetition + X duplicate-text protection).

See also: [rendering.md](rendering.md) ·
[elevenlabs.md](../integrations/elevenlabs.md) ·
[telegram.md](../integrations/telegram.md)
