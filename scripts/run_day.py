"""Run the full pipeline for a day and send the Telegram preview.

Same code path the bot's /makeit uses, runnable from the server CLI:
  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app video-journal \
      python scripts/run_day.py [YYYY-MM-DD]
"""
import asyncio
import sys
from datetime import date, datetime

from aiogram import Bot

from app import db, notify
from app.bot import send_draft_preview, send_script_review
from app.config import settings
from app.pipeline import run_pipeline


async def main() -> None:
    d = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
         else datetime.now(settings.tz).date())
    db.connect()
    bot = Bot(token=settings.telegram_bot_token)
    notify.set_notifier(
        lambda text: bot.send_message(settings.telegram_user_id, text))
    try:
        draft, version, result = await run_pipeline(d)
        if result is None:  # SCRIPT_REVIEW gate: stopped at script_review
            await send_script_review(bot, d)
            print(f"SCRIPT_REVIEW: v{version} '{draft.theme}' — awaiting approval "
                  "(no render). Approve in Telegram to build.")
        else:
            await send_draft_preview(bot, d)
            print(f"DONE: v{version} '{draft.theme}' -> {result.video} "
                  f"({result.duration_s:.1f}s)")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
