# Telegram bot (control interface)

`app/bot.py`, aiogram 3 long-polling (no webhook/TLS). Configure your bot
via `TELEGRAM_BOT_TOKEN` and hard-lock it to your own chat with
`TELEGRAM_USER_ID` (chat ids are bot-independent, so the same chat id works
across bots). **Use a dedicated bot token**: two `getUpdates` consumers on one
token break both — if another bot already polls a token, give this project its
own.

## Commands

| Command | Effect |
|---|---|
| `/menu` | inline-button menu (Status / Context / Folder / Preview / Build) + command legend |
| `/status` | day number, status, clips, post URLs |
| `/context` | today's accumulated context so far (typed notes + transcribed voice notes, timestamped) |
| `/folder` | today's Drive folder link on demand (same link the 18:00 job sends) |
| `/preview` | resend the latest draft preview (today's, or the day still awaiting approval) |
| `/makeit` | build today's draft now |
| `/note <text>` | add context for today's story |
| `/shoutout <@a @b>` | set today's X shoutout handles (mentioned in the X post only); `/shoutout` shows the current one, `/shoutout clear` removes it |
| `/redo` | full re-draft from scratch (also the escape hatch for drafts stored in a pre-refactor schema) |

The same list is registered with `bot.set_my_commands` (`set_bot_commands` in
`app/bot.py`, called from `main.py`) so it shows in Telegram's native menu
button and `/` autocomplete. `/menu` buttons emit `menu|<action>` callbacks
(`on_menu`); read actions reply inline, `menu|build` reuses the build path.

## Free text & voice notes (the main UX)

**Voice notes are always today's context** — downloaded, whisper-transcribed,
echoed back ("🎤 Heard: …"), then saved as **today's note**
(`notes/note_HHMMSS.md`). They never become feedback; the founder dictates the
day's story by voice, not edits to a prior draft.

Free text routes through `_route_text`, in priority order:
1. **today in `script_review`** (gate on, pre-render) → **SCRIPT feedback**:
   `revise_script()` re-runs the drafting brain and re-shows the script, with NO
   voiceover/render (`_script_review_day()`, today-only).
2. **today `awaiting_approval`/`rendered`** → **video revision feedback** (one
   Claude call, stage-targeted regeneration — see
   [pipeline.md](../architecture/pipeline.md#revisions-feedback-loop)).
3. otherwise → saved as today's note.

Both feedback paths are **same-day only** (`_feedback_day()`/`_script_review_day()`
return a day only if it equals today) — a message sent today never lands on
yesterday's still-unapproved draft.

## Uploading clips in-chat

Besides Google Drive, clips/photos can be sent straight to the bot. `on_video`,
`on_document` and `on_photo` (`app/bot.py`) download the file into the **current
day's** `raw/` folder (`_today()` in `America/Los_Angeles`), then mirror
`stage_sync`: create the day row and bump `collecting → synced`. The day is
chosen automatically; the confirmation states which day it landed on
("Day N/30 (Mon Jun 15)") and offers `⬅️ Yesterday` / `Tomorrow ➡️` buttons
(`mv|<date>|<delta>|<name>`) that refile the file via `shutil.move` — that's the
fix for the midnight-boundary case (a clip filmed at 23:50, uploaded at 00:10).

Two constraints come from Telegram, not us:
- **20MB download cap.** The Bot API can't fetch files larger than 20MB, so big
  4K/HDR clips fail (`_ingest` catches it and points the user back to Drive).
- **"Video" vs "file".** Sending as a *video* makes Telegram recompress and
  strip HDR; sending as a *file/document* preserves the original. The bot nudges
  "send as a file" on every video upload. Photos sent normally arrive as JPEG.

Filenames: documents keep their original name (`IMG_1234.MOV`); videos/photos
without one get `tg_HHMMSS_<unique_id>.<ext>` (ext from MIME). `_unique_dest`
de-collides duplicates. Non-media documents (PDF, etc.) are rejected.

## X shoutouts (`/shoutout`, `app/shoutout.py`)

A purely text-post feature: the founder can mention a few X handles in the
day's **X** post to thank people / drive interaction. Those people are NOT in
the video and never touch the EDL, narration, or `Draft` schema.

- **Input**: `/shoutout @alice_dev @bob_builds` (the `@` is optional;
  comma- or space-separated). It scopes to the current day (`_feedback_day()
  or _today()` — same day-resolution as feedback). `parse_handles` is plain
  structured extraction (regex, ≤15 word chars, de-duped, order-preserving) —
  not an LLM intent call. `/shoutout` with no args shows the current line;
  `/shoutout clear` removes it.
- **Storage**: `<day>/shoutouts.json` (`{"handles": [...]}`), a sibling of
  `notes/`. Deliberately outside the Draft so it can't trip the pre-refactor
  `Draft.model_validate` break, and so the @handles always go out verbatim
  rather than being paraphrased by the LLM. Empty list deletes the file — no
  artifact when the feature is unused.
- **Where it's applied**: `shoutout.apply_to_caption()` appends the rendered
  line to the **X** caption (EN and ES, with `and`/`y` respectively) at two
  points: the draft preview text (so the founder sees exactly what X will get)
  and inside `publishing.publish_day()` right before `zernio.create_post`. It's
  idempotent (skips if the line is already present) and a no-op without handles.
- **X vs LinkedIn**: only `platform == "twitter"` gets the line. On LinkedIn a
  bare X `@handle` doesn't resolve to anyone and we don't store the people's
  real names, so nothing is added there — the reliable, primary behavior is the
  X shoutout. (If real names are ever captured, a LinkedIn plain-text mention
  could be added in the same helper.)
- **Example line**: `Shoutout to the friends who showed me so much of SF today:
  @alice_dev and @bob_builds.`

## Nightly check-in vs auto-build (`AUTO_PIPELINE`)

`AUTO_PIPELINE=true` → the 22:30 job builds automatically. `false` (current
preference) → the job instead sends a check-in: clip count + Drive folder link
+ a yes/no keyboard (`🚀 Yes, build it` / `⏳ Not yet`). "Yes" runs the same
path as `/makeit`; the build only starts on the user's go. The button guards
against stale taps (already-built day → points to `/status`). `build_kb()` in
`app/bot.py`.

## Script-review gate (`SCRIPT_REVIEW`)

`SCRIPT_REVIEW=true` (default) inserts a review of the **script** before any
voiceover/render — the cheap words are approved first, not after a ~4-min build.
After drafting, the day parks in `script_review` and `send_script_review(bot, d)`
sends the script **beat-by-beat**: each beat labeled with its clip + **local
capture time** (so the founder sees the footage backing each line), EN narration,
and ES underneath when bilingual. It also shows the **🐦 tweet (X)** — exactly as
it will post (caption + shoutout + sign-off, via `_tweet_preview`) — so the tweet
is reviewable BEFORE the multi-minute render, not only after it. Keyboard:

`✅ Approve & build` / `✏️ Edit tweet` / `🔄 Full redo` `🗑 Discard`

- **✅ Approve & build** (`approvebuild|<date>`, `on_approve_build`) runs
  `pipeline.render_phase` → voiceover + render → `awaiting_approval` → the usual
  draft preview. Guards stale taps (day no longer in `script_review`).
- **✏️ Edit tweet** (`edittweet|<date>`, `on_edit_tweet`) arms **tweet-only**
  feedback: it sets the module-level `_pending_tweet_edit` day flag, and the NEXT
  text message is consumed FIRST in `_route_text` → `pipeline.revise_tweet()`,
  which rewrites `caption_x` via the LLM (`build_tweet_revision_prompt` +
  `TweetReply`) and echoes the new tweet. NO re-draft, NO render — the caption is
  applied at publish time. Same button + flow on the post-render preview. The
  founder always sends **feedback** ("punchier, lead with the SDK"); the LLM
  rewrites the tweet.
- **Revise**: reply with text (without tapping Edit tweet) — that's SCRIPT
  feedback (revises via `stage_draft`, re-shows the script, no render; see
  free-text routing above). Tapping Edit tweet first routes the next message to
  the tweet instead.
- **Force-build bypass**: `🔄 Full redo` re-drafts (and, gate on, re-shows the
  script); to skip the gate per-day, the founder can flip `SCRIPT_REVIEW=false`,
  or simply approve. `/preview` (and `menu|preview`) resend the **script** while
  in the gate, since no video exists yet.

`SCRIPT_REVIEW=false` → behavior is exactly as before: draft flows straight to
voiceover/render and the video preview. The nightly 22:30 job and
`scripts/run_day.py` both honor the gate (send the script, don't render).

## Hook picker

With `FIRST_FRAME_HOOK=true` (default), the script-review message is followed by
the **hook picker**: the agent proposed 2-3 cold-open candidates
(`Draft.hook_candidates`), and `send_hook_picker` encodes each as a short low-res
preview clip — the real 1-2s with the clip's natural sound — sends them as
videos, then a pick-one keyboard:

`Hook 1` `Hook 2` … / `🚫 No hook`

- The picker shows **here, before the build**, so the choice is made from a real
  moving preview without spending a render. Tapping a button
  (`hook|<date>|<idx>`, `on_hook_pick`) writes the selection to `<day>/hook.json`
  and acks; the keyboard is **left up** so the founder can change their mind right
  up until **✅ Approve & build**.
- Button labels are display positions, but the callback carries the **real
  candidate index**, so a candidate that fails to preview (missing clip / encode
  error — best-effort, skipped + logged) can't shift the mapping.
- **Default = Hook 1**: if nothing is tapped, `hook.resolve` falls back to the
  top candidate, so the cold-open works even when the gate is off. `🚫 No hook`
  writes an explicit "off" that beats the default.
- A `🔄 Full redo` / script revision regenerates candidates and **resets** the
  pick (`hook.clear`), so a stale choice can't point at a dropped clip.

The pick is just a selection; the actual prepend (and why it's a separate
cold-open, not a beat) is render-side — see
[rendering.md](../architecture/rendering.md#cold-open-hook).

## Draft preview

Sent after every (re)build: the 🇺🇸 video, then a text message showing **only the
EN tweet (X)** — exactly as it will post (caption + shoutout + sign-off, via
`_tweet_preview`) — plus confidence notes and the inline keyboard:

`✅ Publish 8AM` `🚀 Publish now` / `✏️ Edit tweet` / `🎙 New voiceover` `🔄 Full redo` / `🗑 Discard` (with confirm tap)

Since 2026-06-21 the preview is deliberately tweet-focused: the voiceover script,
the Spanish video/caption, and the LinkedIn caption are **not** shown (publishing
is X/English only — see the gate above and `_langs_for`). **✏️ Edit tweet** works
here too (rewrites `caption_x`, no re-render). Buttons publish via
`publishing.publish_day()` only after setting the day to `approved`. Telegram
media captions cap at 1024 chars — that's why the text is a separate message
(4000-char trim).

## Notifications (`app/notify.py`)

The pipeline never imports the bot; `main.py` registers a send callback.
`NOTIFY_VERBOSE=true` (current) → stage-by-stage messages (agent started,
analyzed N clips, story ready, voiceover done, Spanish version, finished).
Set `false` to keep only the always-on ones: previews, publish results,
errors, folder-created, no-clips reminder. Startup message failure is
non-fatal (a fresh bot can't message a user who never pressed Start).

## Limits

- `sendVideo` ≤ 50MB → automatic preview re-encode in the renderer.
- Bot API file *download* ≤ 20MB — fine for voice notes; caps in-chat clip
  uploads, so large footage still goes via Drive.

See also: [zernio.md](zernio.md) · [deploy.md](../operations/deploy.md)
