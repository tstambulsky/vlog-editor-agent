"""APScheduler jobs — all times in America/Los_Angeles regardless of server TZ."""
import asyncio
import logging
import shutil
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import db, pipeline, publishing
from app.bot import build_kb, send_draft_preview, send_script_review
from app.config import settings
from app.services import drive

log = logging.getLogger(__name__)


def _today():
    return datetime.now(settings.tz).date()


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.tz)

    async def notify(text: str, reply_markup=None):
        try:
            await bot.send_message(settings.telegram_user_id, text,
                                   reply_markup=reply_markup)
        except Exception:
            log.exception("telegram notify failed")

    async def folder_link(d) -> str:
        try:
            return "\n" + await drive.day_folder_link(d)
        except Exception:
            log.warning("couldn't get Drive folder link for %s", d, exc_info=True)
            return ""

    async def sync_poll():
        """Pick up freshly uploaded clips during the day and confirm them."""
        d = _today()
        day = db.get_day(d)
        if day and day["status"] not in ("collecting", "synced"):
            return
        try:
            new = await pipeline.stage_sync(d)
        except Exception as exc:
            log.warning("sync poll failed: %s", exc)
            return
        if new:
            total = len(list((settings.day_dir(d) / "raw").glob("*")))
            await notify(f"📥 {len(new)} new clip(s) synced for Day "
                         f"{settings.day_number(d)} — {total} total.")
            # pre-process the new arrivals now, don't wait for the next sweep
            asyncio.create_task(pipeline.safe_eager_sweep(d))

    async def eager_process():
        """Self-healing net: pre-process any clip in today's raw/ that still
        lacks its keyframes/transcript (covers bot-was-down / Drive-only)."""
        d = _today()
        day = db.get_day(d)
        if day and day["status"] not in ("collecting", "synced"):
            return  # the day is already drafting/rendering/published
        try:
            n = await pipeline.eager_sweep(d)
        except Exception:
            log.exception("eager sweep failed")
            return
        if n:
            log.info("eager-processed %d clip(s) for %s", n, d)

    async def nightly_pipeline():
        d = _today()
        day = db.get_or_create_day(d)
        if day["status"] not in ("collecting", "synced"):
            return  # already built today (e.g. via /makeit)
        if not settings.auto_pipeline:
            uploaded = await drive.list_remote_clips(d)
            await notify(f"🕤 Day {settings.day_number(d)} check-in: "
                         f"{len(uploaded)} clip(s) in the Drive folder. "
                         "Build the video now?"
                         + await folder_link(d),
                         reply_markup=build_kb(d))
            return
        raw = settings.day_dir(d) / "raw"
        has_local = raw.exists() and any(raw.iterdir())
        remote = await drive.list_remote_clips(d) if not has_local else [1]
        if not has_local and not remote:
            await notify(f"😴 No clips for Day {settings.day_number(d)} yet. "
                         "Upload some and send /makeit, or let today slide."
                         + await folder_link(d))
            return
        try:
            _, _, result = await pipeline.run_pipeline(d)
            if result is None:  # gate ON — stopped at script_review, no video yet
                await send_script_review(bot, d)
            else:
                await send_draft_preview(bot, d)
        except Exception as exc:
            db.log_event(d, "error", repr(exc))
            await notify(f"❌ Tonight's pipeline failed: {exc}\nFix and send /makeit.")

    async def create_tomorrow_folder():
        tomorrow = _today() + timedelta(days=1)
        try:
            await drive.create_day_folder(tomorrow)
            await notify(f"📁 Drive folder ready for Day "
                         f"{settings.day_number(tomorrow)}: {tomorrow.isoformat()}"
                         + await folder_link(tomorrow))
        except Exception as exc:
            await notify(f"⚠️ Couldn't create tomorrow's Drive folder: {exc}")

    async def daily_folder_link():
        """6pm SF: send today's Drive folder link so the day's clips can go in
        before the 22:30 build. The folder was created the night before at 22:00."""
        d = _today()
        await notify(f"📁 Day {settings.day_number(d)} ({d.isoformat()}) — "
                     "today's Drive folder:" + await folder_link(d))

    async def post_poll():
        try:
            resolved = await publishing.poll_pending_posts()
        except Exception:
            log.exception("post poll failed")
            return
        for r in resolved:
            if r["ok"]:
                await notify(f"🎉 Published on {r['platform']}"
                             + (f": {r['url']}" if r.get("url") else " (no URL returned)"))
            else:
                await notify(f"❌ {r['platform']} publish failed: {r['error'][:300]}\n"
                             "Approve again to retry.")

    async def cleanup_old_days():
        """Free disk: drop raw footage and render intermediates once a day is
        settled (originals live in Drive; outputs/drafts/VO are kept). The
        server has ~24GB free — 30 days of raw iPhone clips would not fit."""
        cutoff = (_today() - timedelta(days=2)).isoformat()
        rows = db.connect().execute(
            "SELECT date FROM days WHERE date<? AND status IN ('published','discarded','skipped')",
            (cutoff,)).fetchall()
        for day in rows:
            base = settings.data_dir / "days" / day["date"]
            if not base.exists():
                continue
            targets = [base / "raw", base / "work" / "frames",
                       *(base / "work").glob("segments_*")]
            for target in targets:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                    db.log_event(None, "pruned", str(target))

    sched.add_job(cleanup_old_days, CronTrigger(hour=3, minute=0),
                  id="cleanup_old_days", coalesce=True, misfire_grace_time=3600)
    sched.add_job(sync_poll, IntervalTrigger(minutes=settings.sync_interval_min),
                  id="sync_poll", coalesce=True, max_instances=1)
    sched.add_job(eager_process, IntervalTrigger(minutes=settings.eager_interval_min),
                  id="eager_process", coalesce=True, max_instances=1,
                  misfire_grace_time=300)
    sched.add_job(nightly_pipeline,
                  CronTrigger(hour=settings.pipeline_hour, minute=settings.pipeline_minute),
                  id="nightly_pipeline", coalesce=True, max_instances=1,
                  misfire_grace_time=3600)
    sched.add_job(create_tomorrow_folder, CronTrigger(hour=22, minute=0),
                  id="create_tomorrow_folder", coalesce=True, misfire_grace_time=3600)
    sched.add_job(daily_folder_link, CronTrigger(hour=18, minute=0),
                  id="daily_folder_link", coalesce=True, misfire_grace_time=3600)
    sched.add_job(post_poll, IntervalTrigger(minutes=10),
                  id="post_poll", coalesce=True, max_instances=1)
    return sched
