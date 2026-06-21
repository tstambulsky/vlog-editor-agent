"""Approve a script-review day from the CLI: run voiceover + render and send
the preview — the exact path the "✅ Approve & build" button takes.

Use when a day is parked at `script_review` and you want to build it without
tapping the button (the hook pick in hook.json is honored at render time).

  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app video-journal \
      python scripts/approve_build.py [YYYY-MM-DD]
"""
import asyncio
import sys
from datetime import date, datetime

from aiogram import Bot

from app import db, notify
from app.bot import send_draft_preview
from app.config import settings
from app.pipeline import render_phase


async def main() -> None:
    d = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
         else datetime.now(settings.tz).date())
    db.connect()
    day = db.get_day(d)
    if not day or day["status"] != "script_review":
        raise SystemExit(
            f"{d} is not at script_review (status={day['status'] if day else None})")

    bot = Bot(token=settings.telegram_bot_token)
    notify.set_notifier(
        lambda text: bot.send_message(settings.telegram_user_id, text))
    try:
        draft, version, result = await render_phase(d)
        await send_draft_preview(bot, d)
        print(f"DONE: v{version} '{draft.theme}' -> {result.video} "
              f"({result.duration_s:.1f}s)")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
