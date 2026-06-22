"""Telegram control interface (aiogram, long polling).

Voice notes are always saved as the day's context (never feedback). Free-text
replies become revision feedback only while TODAY's draft is awaiting approval;
otherwise they too are saved as the day's notes. Feedback never reaches across
days. Buttons cover publish/discard/regenerate.
"""
import asyncio
import html
import json
import mimetypes
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

from app import db, hook, pipeline, publishing, shoutout, signature
from app.config import settings
from app.schemas import Draft
from app.services import analyze, drive, render as render_svc

router = Router()
router.message.filter(F.from_user.id == settings.telegram_user_id)
router.callback_query.filter(F.from_user.id == settings.telegram_user_id)

_busy = False  # one heavy job at a time; pipeline has its own lock too


def _today() -> date:
    return datetime.now(settings.tz).date()


def _feedback_day() -> date | None:
    """Feedback only ever targets TODAY's pending draft — never a lingering
    prior day. A message sent today is about today's video, full stop; routing
    it onto yesterday's still-unapproved draft was the cross-day mix-up bug."""
    row = db.latest_day_with_status(("awaiting_approval", "rendered"))
    if not row:
        return None
    d = date.fromisoformat(row["date"])
    return d if d == _today() else None


def _script_review_day() -> date | None:
    """Today's day if it's parked in `script_review` (gate ON, awaiting a
    script go/no-go). Same today-only rule as `_feedback_day`."""
    row = db.latest_day_with_status(("script_review",))
    if not row:
        return None
    d = date.fromisoformat(row["date"])
    return d if d == _today() else None


def _kb(d: date) -> InlineKeyboardMarkup:
    ds = d.isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Publish 8AM", callback_data=f"pub8|{ds}"),
         InlineKeyboardButton(text="🚀 Publish now", callback_data=f"pubnow|{ds}")],
        [InlineKeyboardButton(text="🎙 New voiceover", callback_data=f"regenvo|{ds}"),
         InlineKeyboardButton(text="🔄 Full redo", callback_data=f"redo|{ds}")],
        [InlineKeyboardButton(text="🗑 Discard", callback_data=f"discard|{ds}")],
    ])


def build_kb(d: date) -> InlineKeyboardMarkup:
    """Yes/no keyboard for the nightly check-in (AUTO_PIPELINE=false)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Yes, build it", callback_data=f"build|{d.isoformat()}"),
        InlineKeyboardButton(text="⏳ Not yet", callback_data="nobuild"),
    ]])


async def send_draft_preview(bot: Bot, d: date) -> None:
    row = db.get_draft(d)
    day = db.get_day(d)
    draft = json.loads(row["draft_json"])
    preview = Path(row["preview_path"] or row["video_path"])

    await bot.send_video(
        settings.telegram_user_id, FSInputFile(preview),
        caption=f"🎬 Draft 🇺🇸 — Day {day['day_number']}/30 (v{row['version']})",
        width=1080, height=1920,
    )
    es_preview = row["es_preview_path"] or row["es_video_path"]
    if es_preview and Path(es_preview).exists():
        await bot.send_video(
            settings.telegram_user_id, FSInputFile(Path(es_preview)),
            caption=f"🎬 Version 🇪🇸 — Day {day['day_number']}/30 (v{row['version']})",
            width=1080, height=1920,
        )
    notes = draft.get("confidence_notes") or ""
    handles = shoutout.get_handles(d)
    shout_block = ""
    if handles:
        shout_block = (
            f"<b>X shoutout (added to X posts):</b>\n"
            f"{html.escape(shoutout.shoutout_line(handles, 'en'))}\n\n"
        )
    es_block = ""
    if draft.get("voiceover_script_es"):
        es_block = (
            f"<b>Voiceover (ES):</b>\n{html.escape(draft['voiceover_script_es'])}\n\n"
            f"<b>X caption (ES):</b>\n{html.escape(draft.get('caption_x_es', ''))}\n\n"
        )
    sig = signature.signature_text("en")
    sig_block = (f"<b>Sign-off (end of every post):</b>\n{html.escape(sig)}\n\n"
                 if sig else "")
    # Every interpolated field below is LLM/user-authored free text — escape it
    # or a stray '<' (e.g. in a caption) makes Telegram reject the whole message.
    text = (
        f"<b>Day {day['day_number']}/30 — v{row['version']}</b>\n\n"
        f"<b>Theme:</b> {html.escape(draft['theme'])}\n\n"
        f"<b>Voiceover (EN):</b>\n{html.escape(draft['voiceover_script'])}\n\n"
        f"<b>X caption (EN):</b>\n{html.escape(draft['caption_x'])}\n\n"
        + shout_block
        + es_block
        + f"<b>LinkedIn caption:</b>\n{html.escape(draft['caption_linkedin'])}\n\n"
        + sig_block
        + (f"\n⚠️ <i>{html.escape(notes)}</i>\n" if notes else "")
        + "\n✍️ Reply with text or a voice note to request changes."
    )
    await bot.send_message(settings.telegram_user_id, text[:4000],
                           reply_markup=_kb(d), parse_mode="HTML")


def _review_kb(d: date) -> InlineKeyboardMarkup:
    """Buttons under a script-review message. "Approve & build" kicks off the
    voiceover+render phase; the founder can still force a full redo or discard."""
    ds = d.isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve & build", callback_data=f"approvebuild|{ds}")],
        [InlineKeyboardButton(text="🔄 Full redo", callback_data=f"redo|{ds}"),
         InlineKeyboardButton(text="🗑 Discard", callback_data=f"discard|{ds}")],
    ])


def _clip_times(d: date) -> dict[str, str]:
    """clip filename -> local HH:MM capture time (for labeling each beat with
    the footage that backs it)."""
    times: dict[str, str] = {}
    for c in db.get_clips(d):
        local = c["created_local"] or ""
        # created_local is ISO like 2026-06-12T07:02:04-07:00
        hhmm = local[11:16] if len(local) >= 16 else ""
        times[c["filename"]] = hhmm
    return times


def _beats_block(draft: dict, times: dict[str, str]) -> str:
    """Render the EDL beat-by-beat: each line shows its clip + local capture
    time, the EN narration, and ES underneath when bilingual."""
    lines = []
    for i, e in enumerate(draft.get("edl", []), 1):
        clip = e.get("clip", "?")
        when = times.get(clip, "")
        head = f"<b>{i}.</b> 🎞 {html.escape(clip)}" + (f" · {when}" if when else "")
        lines.append(head)
        lines.append(html.escape(e.get("narration", "").strip()))
        es = (e.get("narration_es") or "").strip()
        if es:
            lines.append(f"🇪🇸 {html.escape(es)}")
        lines.append("")  # blank line between beats
    return "\n".join(lines).rstrip()


def _hook_kb(d: date, shown: list[int]) -> InlineKeyboardMarkup:
    """Pick-one keyboard under the candidate hook previews. Button labels are
    display positions (Hook 1, Hook 2, …); the callback carries the REAL
    candidate index so a skipped candidate can't shift the mapping. `none` skips
    the cold-open; the agent's top candidate is the default if nothing is tapped."""
    ds = d.isoformat()
    picks = [InlineKeyboardButton(text=f"Hook {pos + 1}", callback_data=f"hook|{ds}|{real}")
             for pos, real in enumerate(shown)]
    return InlineKeyboardMarkup(inline_keyboard=[
        picks, [InlineKeyboardButton(text="🚫 No hook", callback_data=f"hook|{ds}|none")]])


def _mark_hook_kb(markup: InlineKeyboardMarkup, selected: str) -> InlineKeyboardMarkup:
    """Re-render an existing hook keyboard with a ✅ on the button whose
    callback_data is `selected` (and the check stripped from the others), so the
    founder's pick is visibly persistent instead of a fleeting toast."""
    rows = []
    for row in markup.inline_keyboard:
        out = []
        for btn in row:
            base = btn.text.removeprefix("✅ ")
            text = f"✅ {base}" if btn.callback_data == selected else base
            out.append(InlineKeyboardButton(text=text, callback_data=btn.callback_data))
        rows.append(out)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_hook_picker(bot: Bot, d: date, draft: Draft) -> None:
    """Encode each proposed hook as a short 1-2s preview clip (with the clip's
    natural sound), send them, and offer a pick-one keyboard. Best-effort: a
    candidate whose clip is missing or won't encode is skipped. No-op when the
    feature is off or the draft proposed no usable hooks."""
    if not (settings.first_frame_hook and draft.hook_candidates):
        return
    row = db.get_draft(d)
    version = row["version"]
    metas = pipeline.load_clip_metas(d)
    day_dir = settings.day_dir(d)
    shown: list[int] = []  # real candidate indices that previewed OK
    for i, h in enumerate(draft.hook_candidates):
        meta = metas.get(h.clip)
        if not meta:
            continue
        out_rel = f"work/hook_previews_v{version}/cand_{i}.mp4"
        try:
            await render_svc.hook_preview(day_dir, h, meta, out_rel)
        except Exception as exc:  # don't let a bad candidate block the gate
            db.log_event(d, "hook_preview_failed", f"cand {i} ({h.clip}): {exc!r}")
            continue
        mm, ss = divmod(int(h.start_s), 60)
        cap = (f"🎬 Hook {len(shown) + 1} — {h.reason or 'opening grab'} · "
               f"{h.clip} @ {mm}:{ss:02d}")
        await bot.send_video(settings.telegram_user_id,
                             FSInputFile(day_dir / out_rel), caption=cap[:1000])
        shown.append(i)
    if not shown:
        return
    await bot.send_message(
        settings.telegram_user_id,
        "👆 Pick the opening <b>hook</b> — a 1-2s cold-open shown before the "
        "video to grab attention. Default is Hook 1; tap to change or skip it.",
        reply_markup=_hook_kb(d, shown), parse_mode="HTML")


async def send_script_review(bot: Bot, d: date) -> None:
    """Send the drafted script for review BEFORE any voiceover/render. The
    script is shown beat-by-beat (clip + local capture time + EN/ES narration)
    with an Approve & build keyboard. No video is sent — that's the whole point
    of the gate (the founder reviews the cheap words first). When the hook
    feature is on, the proposed cold-open candidates follow as short clips with
    a pick-one keyboard."""
    row = db.get_draft(d)
    day = db.get_day(d)
    draft = json.loads(row["draft_json"])
    times = _clip_times(d)
    text = (
        f"<b>📝 Script review — Day {day['day_number']}/30 (v{row['version']})</b>\n"
        f"<b>Theme:</b> {html.escape(draft['theme'])}\n\n"
        + _beats_block(draft, times)
        + "\n\nReview the script. Reply with text to revise it (no render yet), "
        "or tap ✅ to build the voiceover + video."
    )
    await bot.send_message(settings.telegram_user_id, text[:4000],
                           reply_markup=_review_kb(d), parse_mode="HTML")
    await send_hook_picker(bot, d, Draft.model_validate_json(row["draft_json"]))


async def _run_heavy(bot: Bot, coro, working_msg: str):
    global _busy
    if _busy:
        await bot.send_message(settings.telegram_user_id,
                               "⏳ Already working on something — give me a minute.")
        return
    _busy = True
    try:
        await bot.send_message(settings.telegram_user_id, working_msg)
        await coro
    except Exception as exc:  # surfaced to the user, logged in events
        db.log_event(None, "error", repr(exc))
        await bot.send_message(settings.telegram_user_id, f"❌ {exc}")
    finally:
        _busy = False


async def _make_and_preview(bot: Bot, d: date):
    """Build today's draft. With the script-review gate ON, `run_pipeline`
    stops at `script_review` (returns no render) and we send the script for
    review; with it OFF, it renders and we send the video preview."""
    _, _, result = await pipeline.run_pipeline(d)
    if result is None:  # gate ON — stopped at script_review, nothing rendered
        await send_script_review(bot, d)
    else:
        await send_draft_preview(bot, d)


async def _render_and_preview(bot: Bot, d: date):
    """Approve-time: run the voiceover + render phase for an already-reviewed
    script, then send the video preview (the usual approval gate)."""
    await pipeline.render_phase(d)
    await send_draft_preview(bot, d)


async def _revise_script_and_review(bot: Bot, d: date, feedback: str):
    """Script-feedback while in `script_review`: revise the script (no render)
    and re-send it for review."""
    _, version, changed = await pipeline.revise_script(d, feedback)
    label = ", ".join(changed) if changed else "nothing (Claude saw no needed change)"
    await bot.send_message(settings.telegram_user_id,
                           f"✏️ v{version}: revised {label} (not rendered yet).")
    await send_script_review(bot, d)


async def _feedback_and_preview(bot: Bot, d: date, feedback: str):
    _, version, _, changed = await pipeline.apply_feedback(d, feedback)
    label = ", ".join(changed) if changed else "nothing (Claude saw no needed change)"
    await bot.send_message(settings.telegram_user_id,
                           f"🔧 v{version}: changed {label}.")
    await send_draft_preview(bot, d)


# ---------- commands ----------

_MENU_TEXT = (
    "🧭 <b>Menu</b>\n"
    "📊 /status — where things stand today\n"
    "📝 /context — context gathered so far today\n"
    "📁 /folder — today's Drive folder link\n"
    "🎬 /preview — resend the latest draft\n"
    "🛠 /makeit — build today's draft now\n"
    "🔄 /redo — re-draft from scratch\n"
    "✍️ /note &lt;text&gt; — add context (or just send text / a voice note)\n"
    "📣 /shoutout &lt;@a @b&gt; — mention X handles in today's X post"
)

# Native Telegram command list (the menu button + "/" autocomplete).
BOT_COMMANDS = [
    BotCommand(command="menu", description="Show the menu"),
    BotCommand(command="status", description="Where things stand today"),
    BotCommand(command="context", description="Context gathered so far today"),
    BotCommand(command="folder", description="Today's Drive folder link"),
    BotCommand(command="preview", description="Resend the latest draft"),
    BotCommand(command="makeit", description="Build today's draft now"),
    BotCommand(command="redo", description="Re-draft from scratch"),
    BotCommand(command="note", description="Add context for today"),
    BotCommand(command="shoutout", description="Mention X handles in today's X post"),
]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Status", callback_data="menu|status"),
         InlineKeyboardButton(text="📝 Context", callback_data="menu|context")],
        [InlineKeyboardButton(text="📁 Drive folder", callback_data="menu|folder"),
         InlineKeyboardButton(text="🎬 Preview", callback_data="menu|preview")],
        [InlineKeyboardButton(text="🛠 Build now", callback_data="menu|build")],
    ])


def _status_text(d: date) -> str:
    day = db.get_day(d) or db.get_or_create_day(d)
    clips = db.get_clips(d)
    raw = settings.day_dir(d) / "raw"
    n_raw = len(list(raw.glob("*"))) if raw.exists() else 0
    posts = db.get_posts(d)
    lines = [f"📊 Day {day['day_number']}/30 ({d}) — <b>{day['status']}</b>",
             f"Clips: {max(len(clips), n_raw)} synced"]
    for p in posts:
        lines.append(f"{p['platform']}: {p['status']}"
                     + (f" → {p['published_url']}" if p["published_url"] else ""))
    return "\n".join(lines)


def _context_text(d: date) -> str:
    """Today's accumulated notes — typed notes AND transcribed voice notes
    (both land as notes/note_HHMMSS.md via _save_note)."""
    notes_dir = settings.day_dir(d) / "notes"
    items = []
    if notes_dir.exists():
        for p in sorted(notes_dir.glob("note_*.md")):
            txt = p.read_text().strip()
            if not txt:
                continue
            stamp = p.stem.removeprefix("note_")  # HHMMSS
            hhmm = f"{stamp[:2]}:{stamp[2:4]}" if len(stamp) >= 4 else stamp
            items.append(f"<b>{hhmm}</b> — {html.escape(txt)}")
    n = settings.day_number(d)
    if not items:
        return (f"📝 Day {n}/30 ({d}) — no context yet today.\n"
                "Send a voice note or text, or /note &lt;something&gt;.")
    return (f"📝 Day {n}/30 ({d}) — context so far ({len(items)}):\n\n"
            + "\n\n".join(items))


async def _folder_text(d: date) -> str:
    try:
        link = await drive.day_folder_link(d)
        return f"📁 Day {settings.day_number(d)}/30 ({d}) Drive folder:\n{link}"
    except Exception:
        return ("📁 Couldn't fetch the Drive folder link right now "
                "(the folder may not exist yet). Try again shortly.")


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 SF journal bot ready. /menu for everything.\n"
        "Send clips right here (as a file for full quality) or to the Drive "
        f"folder; I'll handle the rest at "
        f"{settings.pipeline_hour:02d}:{settings.pipeline_minute:02d}."
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer(_MENU_TEXT, reply_markup=_menu_kb(), parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(_status_text(_today()), parse_mode="HTML")


@router.message(Command("context"))
async def cmd_context(message: Message):
    await message.answer(_context_text(_today()), parse_mode="HTML")


@router.message(Command("folder"))
async def cmd_folder(message: Message):
    await message.answer(await _folder_text(_today()))


@router.message(Command("preview"))
async def cmd_preview(message: Message, bot: Bot):
    sr_day = _script_review_day()
    if sr_day:  # in the gate — there's no video yet, resend the script instead
        await send_script_review(bot, sr_day)
        return
    d = _feedback_day() or _today()
    if not db.get_draft(d):
        await message.answer("No draft yet today — send /makeit to build one.")
        return
    await send_draft_preview(bot, d)


@router.message(Command("makeit"))
async def cmd_makeit(message: Message, bot: Bot):
    d = _today()
    asyncio.create_task(_run_heavy(bot, _make_and_preview(bot, d),
                                   "🛠 Building today's draft — this takes a few minutes…"))


@router.message(Command("redo"))
async def cmd_redo(message: Message, bot: Bot):
    d = _feedback_day() or _today()
    asyncio.create_task(_run_heavy(bot, _make_and_preview(bot, d),
                                   "🔄 Re-drafting from scratch…"))


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Usage: /note <what happened today>")
        return
    _save_note(_today(), command.args)
    await message.answer("📝 Noted.")


@router.message(Command("shoutout"))
async def cmd_shoutout(message: Message, command: CommandObject):
    """Set today's X shoutout handles — mentioned in the X post only, never in
    the video. `/shoutout @a @b` sets them; `/shoutout` (no args) shows/clears
    via `/shoutout clear`."""
    d = _feedback_day() or _today()
    arg = (command.args or "").strip()
    if not arg:
        handles = shoutout.get_handles(d)
        if handles:
            await message.answer(
                "📣 Today's X shoutout:\n"
                f"{shoutout.shoutout_line(handles, 'en')}\n\n"
                "Replace with /shoutout @a @b, or remove with /shoutout clear.")
        else:
            await message.answer(
                "No shoutout set for today. Add one with "
                "/shoutout @handle1 @handle2 — they get mentioned in the X post "
                "(not the video).")
        return
    if arg.lower() in ("clear", "none", "off"):
        shoutout.set_handles(d, [])
        await message.answer("📣 Cleared today's shoutout.")
        return
    handles = shoutout.parse_handles(arg)
    if not handles:
        await message.answer("🤷 No valid X handles found. Try /shoutout @abed_hamami @ionpetropoulos")
        return
    shoutout.set_handles(d, handles)
    await message.answer(
        "📣 Shoutout set — it'll be added to today's X post:\n"
        f"{shoutout.shoutout_line(handles, 'en')}")


# ---------- free text & voice ----------

def _save_note(d: date, text: str) -> None:
    notes = settings.day_dir(d) / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(settings.tz).strftime("%H%M%S")
    (notes / f"note_{stamp}.md").write_text(text)
    db.log_event(d, "note", text[:200])


async def _route_text(message: Message, bot: Bot, text: str):
    """Free text routing (voice notes never reach here — they're always context):
    - today in `script_review` → SCRIPT feedback (revise script, NO render);
    - today awaiting approval/rendered → VIDEO feedback (revise + re-render);
    - otherwise → saved as today's context note.
    Both feedback paths are today-only (no cross-day mix-ups)."""
    sr_day = _script_review_day()
    if sr_day:
        asyncio.create_task(_run_heavy(
            bot, _revise_script_and_review(bot, sr_day, text),
            "✏️ Revising the script…"))
        return
    fb_day = _feedback_day()
    if fb_day:
        asyncio.create_task(_run_heavy(
            bot, _feedback_and_preview(bot, fb_day, text),
            "🔧 Applying your feedback…"))
    else:
        _save_note(_today(), text)
        await message.answer("📝 Saved as today's note.")


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot):
    d = _today()
    notes = settings.day_dir(d) / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(settings.tz).strftime("%H%M%S")
    ogg = notes / f"voice_{stamp}.ogg"
    await bot.download(message.voice, destination=ogg)
    # transcribe_text joins SEGMENT text (not word-gated), so long voice notes
    # aren't silently clipped when a chunk's word alignment comes back empty.
    text = await analyze.transcribe_text(ogg)
    if not text:
        await message.answer("🤷 Couldn't transcribe that voice note.")
        return
    await message.answer(f"🎤 Heard: “{text[:3900]}”")
    # Voice notes are ALWAYS context for today's video, never feedback on a
    # prior draft — the founder dictates the day's story, not edit requests.
    _save_note(d, text)
    await message.answer("📝 Saved as today's context.")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot):
    await _route_text(message, bot, message.text)


# ---------- media uploads (clips → the day's raw/ folder) ----------

def _ext_from_mime(mime: str | None, default: str) -> str:
    if mime:
        ext = (mimetypes.guess_extension(mime) or "").lower()
        if ext in (".jpe", ".jpeg"):
            ext = ".jpg"
        if ext:
            return ext
    return default


def _unique_dest(raw: Path, name: str) -> Path:
    """Avoid clobbering same-named uploads (e.g. two IMG_1234.MOV)."""
    dest = raw / name
    stem, suf = dest.stem, dest.suffix
    i = 1
    while dest.exists():
        dest = raw / f"{stem}_{i}{suf}"
        i += 1
    return dest


def _register_day_clips(d: date) -> int:
    """Mirror stage_sync: ensure the day row exists and bump it to 'synced'
    once raw/ has clips. Returns the current clip count. Never downgrades a
    day that's already past collecting."""
    db.get_or_create_day(d)
    raw = settings.day_dir(d) / "raw"
    n = len([p for p in raw.glob("*") if p.is_file()]) if raw.exists() else 0
    day = db.get_day(d)
    if n and day and day["status"] == "collecting":
        db.set_status(d, "synced")
    return n


def _move_kb(d: date, name: str) -> InlineKeyboardMarkup | None:
    """Quick correction for the midnight-boundary case: refile to ±1 day."""
    row = []
    for delta, label in ((-1, "⬅️ Yesterday"), (1, "Tomorrow ➡️")):
        data = f"mv|{d.isoformat()}|{delta}|{name}"
        if len(data.encode()) <= 64:  # Telegram callback_data limit
            row.append(InlineKeyboardButton(text=label, callback_data=data))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


async def _ingest(message: Message, bot: Bot, tg_file, name: str, kind: str) -> None:
    d = _today()
    if Path(name).suffix.lower() not in drive.MEDIA_EXTS:
        await message.answer(
            f"🤷 <b>{name}</b> isn't a clip or photo I can use.\n"
            f"Accepted: {', '.join(sorted(drive.MEDIA_EXTS))}.",
            parse_mode="HTML")
        return
    raw = settings.day_dir(d) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(raw, name)
    try:
        await bot.download(tg_file, destination=dest)
    except Exception as exc:  # 20MB bot-download cap is the usual culprit
        dest.unlink(missing_ok=True)
        await message.answer(
            f"❌ Couldn't pull <b>{name}</b> — {str(exc)[:150]}\n"
            "Telegram caps bot downloads at 20MB. For big 4K/HDR clips, "
            "drop them in the Drive folder instead.",
            parse_mode="HTML")
        return
    n = _register_day_clips(d)
    db.log_event(d, "upload", f"{kind}:{dest.name}")
    # pre-process this clip now (keyframes + transcript + vision summary) so it's
    # ready before the build; non-blocking, runs concurrently with idle time and
    # never touches the single _busy build slot.
    asyncio.create_task(pipeline.safe_eager_sweep(d))
    text = (f"📥 Saved <b>{dest.name}</b> to Day {settings.day_number(d)}/30 "
            f"({d:%a %b %d}) — {n} clip(s) so far.")
    if kind == "video":
        text += "\n💡 Tip: send as a <i>file</i> (not “video”) to keep full 4K/HDR quality."
    kb = _move_kb(d, dest.name)
    if kb:
        text += "\nWrong day? Move it:"
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(F.video)
async def on_video(message: Message, bot: Bot):
    v = message.video
    name = (getattr(v, "file_name", None)
            or f"tg_{datetime.now(settings.tz):%H%M%S}_{v.file_unique_id}"
               f"{_ext_from_mime(v.mime_type, '.mp4')}")
    await _ingest(message, bot, v, name, "video")


@router.message(F.document)
async def on_document(message: Message, bot: Bot):
    doc = message.document
    name = (doc.file_name
            or f"tg_{datetime.now(settings.tz):%H%M%S}_{doc.file_unique_id}"
               f"{_ext_from_mime(doc.mime_type, '')}")
    await _ingest(message, bot, doc, name, "document")


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    ph = message.photo[-1]  # largest size
    name = f"tg_{datetime.now(settings.tz):%H%M%S}_{ph.file_unique_id}.jpg"
    await _ingest(message, bot, ph, name, "photo")


@router.callback_query(F.data.startswith("mv|"))
async def on_move_clip(query: CallbackQuery, bot: Bot):
    _, ds, delta, name = query.data.split("|", 3)
    src_day = date.fromisoformat(ds)
    dst_day = src_day + timedelta(days=int(delta))
    src = settings.day_dir(src_day) / "raw" / name
    if not src.exists():
        await query.answer("File not found — already moved?", show_alert=True)
        return
    dst_raw = settings.day_dir(dst_day) / "raw"
    dst_raw.mkdir(parents=True, exist_ok=True)
    dst = _unique_dest(dst_raw, name)
    shutil.move(str(src), str(dst))
    _register_day_clips(src_day)
    n = _register_day_clips(dst_day)
    db.log_event(dst_day, "upload_moved", f"{name} from {src_day}")
    await query.answer(f"Moved to {dst_day}")
    await query.message.edit_text(
        f"📥 Moved <b>{name}</b> → Day {settings.day_number(dst_day)}/30 "
        f"({dst_day:%a %b %d}) — {n} clip(s).", parse_mode="HTML")


# ---------- callbacks ----------

@router.callback_query(F.data.startswith("hook|"))
async def on_hook_pick(query: CallbackQuery):
    """Record the founder's cold-open pick (hook|<date>|<idx> or hook|<date>|none).
    Leaves the keyboard up so they can change their mind before Approve & build."""
    _, ds, sel = query.data.split("|")
    d = date.fromisoformat(ds)
    row = db.get_draft(d)
    if not row:
        await query.answer("No draft to attach a hook to", show_alert=True)
        return
    draft = Draft.model_validate_json(row["draft_json"])
    if sel == "none":
        hook.select(d, draft, None)
        await query.answer("Cold-open off 🚫")
    else:
        chosen = hook.select(d, draft, int(sel))
        # Label by display POSITION (the button the founder tapped), not the raw
        # candidate index — they diverge when a candidate was skipped.
        pos = next((i + 1 for row in query.message.reply_markup.inline_keyboard
                    for i, b in enumerate(row) if b.callback_data == query.data), None)
        await query.answer(f"Hook {pos} set ✅" if chosen and pos
                           else "Couldn't set that hook")
    # Persist the pick visually — without this, the only feedback is a toast that
    # is easy to miss, so a successful tap looks like nothing happened.
    try:
        await query.message.edit_reply_markup(
            reply_markup=_mark_hook_kb(query.message.reply_markup, query.data))
    except Exception:
        pass  # re-tapped same button (unchanged markup) or message too old


@router.callback_query(F.data.startswith("approvebuild|"))
async def on_approve_build(query: CallbackQuery, bot: Bot):
    """Script-review approval: kick off voiceover + render for the reviewed
    script. Guards against stale taps (day already past script_review)."""
    d = date.fromisoformat(query.data.split("|")[1])
    day = db.get_day(d)
    if not day or day["status"] != "script_review":
        await query.answer("Already past the script — check /status", show_alert=True)
        return
    await query.answer("Building the video")
    await _clear_buttons(query)
    asyncio.create_task(_run_heavy(
        bot, _render_and_preview(bot, d),
        "🎬 Script approved — generating voiceover + video…"))


@router.callback_query(F.data.startswith(("pub8|", "pubnow|")))
async def on_publish(query: CallbackQuery, bot: Bot):
    action, ds = query.data.split("|")
    d = date.fromisoformat(ds)
    db.set_status(d, "approved")
    when = None
    if action == "pub8":
        when = publishing.next_publish_time(datetime.now(settings.tz))
    await query.answer("Publishing…" if when is None else f"Scheduled {when:%a %H:%M}")

    async def _go():
        results = await publishing.publish_day(d, when=when)
        lines = []
        for r in results:
            if r["ok"]:
                lines.append(f"✅ {r['platform']}: "
                             + ("queued for " + when.strftime("%a %H:%M %Z") if when else "publishing now"))
            else:
                lines.append(f"❌ {r['platform']}: {r['error'][:200]}")
        await bot.send_message(settings.telegram_user_id, "\n".join(lines))

    asyncio.create_task(_run_heavy(bot, _go(), "📤 Uploading to Zernio…"))


@router.callback_query(F.data.startswith("addli|"))
async def on_add_linkedin(query: CallbackQuery, bot: Bot):
    """Publish LinkedIn as a LATE-ADDED channel — only_platforms keeps any
    already-scheduled platform (e.g. X) untouched, so this never double-posts.
    Callback: 'addli|<date>' or 'addli|8am|<date>' schedules the next 8am;
    'addli|now|<date>' publishes immediately."""
    parts = query.data.split("|")
    mode, ds = (parts[1], parts[2]) if len(parts) == 3 else ("8am", parts[1])
    d = date.fromisoformat(ds)
    db.set_status(d, "approved")
    when = None if mode == "now" else publishing.next_publish_time(
        datetime.now(settings.tz))
    await query.answer("Publishing LinkedIn…" if when is None
                       else f"Scheduling LinkedIn {when:%a %H:%M}")

    async def _go():
        results = await publishing.publish_day(d, when=when,
                                               only_platforms={"linkedin"})
        lines = []
        for r in results:
            if r["ok"]:
                lines.append(f"✅ {r['platform']} ({r['lang']}): "
                             + ("publishing now" if when is None
                                else "queued for " + when.strftime("%a %H:%M %Z")))
            else:
                lines.append(f"❌ {r['platform']} ({r.get('lang', '')}): "
                             f"{r['error'][:200]}")
        await bot.send_message(settings.telegram_user_id,
                               "\n".join(lines) or "Nothing to publish.")

    asyncio.create_task(_run_heavy(bot, _go(), "📤 Uploading LinkedIn to Zernio…"))


@router.callback_query(F.data.startswith("regenvo|"))
async def on_regen_vo(query: CallbackQuery, bot: Bot):
    d = date.fromisoformat(query.data.split("|")[1])
    await query.answer("New take coming up")

    async def _go():
        await pipeline.regenerate_voiceover(d)
        await send_draft_preview(bot, d)

    asyncio.create_task(_run_heavy(bot, _go(), "🎙 Regenerating voiceover…"))


@router.callback_query(F.data.startswith("redo|"))
async def on_redo(query: CallbackQuery, bot: Bot):
    d = date.fromisoformat(query.data.split("|")[1])
    await query.answer("Re-drafting")
    asyncio.create_task(_run_heavy(bot, _make_and_preview(bot, d),
                                   "🔄 Re-drafting from scratch…"))


@router.callback_query(F.data.startswith("discard|"))
async def on_discard(query: CallbackQuery):
    ds = query.data.split("|")[1]
    await query.answer()
    await query.message.answer(
        f"Really discard day {ds}? This skips publishing.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Yes, discard", callback_data=f"discardyes|{ds}"),
            InlineKeyboardButton(text="Keep it", callback_data="noop"),
        ]]))


@router.callback_query(F.data.startswith("discardyes|"))
async def on_discard_yes(query: CallbackQuery):
    d = date.fromisoformat(query.data.split("|")[1])
    db.set_status(d, "discarded")
    await query.answer("Discarded")
    await query.message.answer(f"🗑 Day {d} discarded.")


@router.callback_query(F.data == "noop")
async def on_noop(query: CallbackQuery):
    await query.answer("Kept")


async def _clear_buttons(query: CallbackQuery) -> None:
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass  # message too old or already edited — not worth failing over


@router.callback_query(F.data.startswith("build|"))
async def on_build(query: CallbackQuery, bot: Bot):
    d = date.fromisoformat(query.data.split("|")[1])
    day = db.get_day(d)
    if day and day["status"] not in ("collecting", "synced"):
        await query.answer("Already built — check /status", show_alert=True)
        return
    await query.answer("Building")
    await _clear_buttons(query)
    asyncio.create_task(_run_heavy(bot, _make_and_preview(bot, d),
                                   "🤖 On it — building the video…"))


@router.callback_query(F.data.startswith("menu|"))
async def on_menu(query: CallbackQuery, bot: Bot):
    action = query.data.split("|", 1)[1]
    d = _today()
    if action == "status":
        await query.answer()
        await query.message.answer(_status_text(d), parse_mode="HTML")
    elif action == "context":
        await query.answer()
        await query.message.answer(_context_text(d), parse_mode="HTML")
    elif action == "folder":
        await query.answer("Fetching link…")
        await query.message.answer(await _folder_text(d))
    elif action == "preview":
        sr_day = _script_review_day()
        if sr_day:  # script-review gate: no video yet, resend the script
            await query.answer()
            await send_script_review(bot, sr_day)
            return
        pd = _feedback_day() or d
        if not db.get_draft(pd):
            await query.answer("No draft yet today", show_alert=True)
        else:
            await query.answer()
            await send_draft_preview(bot, pd)
    elif action == "build":
        day = db.get_day(d)
        if day and day["status"] not in ("collecting", "synced"):
            await query.answer("Already built — check /status", show_alert=True)
            return
        await query.answer("Building")
        asyncio.create_task(_run_heavy(bot, _make_and_preview(bot, d),
                                       "🤖 On it — building the video…"))


@router.callback_query(F.data == "nobuild")
async def on_nobuild(query: CallbackQuery):
    await query.answer()
    await _clear_buttons(query)
    await query.message.answer("⏳ OK, I'll wait. Send /makeit whenever you're ready.")
