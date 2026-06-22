"""Regenerate the voiceover (current voice settings) for a day's existing
draft and send the refreshed preview — same as the bot's 🎙 button.

  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app auto-vlog \
      python scripts/regen_vo.py [YYYY-MM-DD]
"""
import asyncio
import sys
from datetime import date, datetime

from aiogram import Bot

from app import db, notify
from app.bot import send_draft_preview
from app.config import settings
from app.pipeline import regenerate_voiceover


async def main() -> None:
    d = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
         else datetime.now(settings.tz).date())
    db.connect()
    bot = Bot(token=settings.telegram_bot_token)
    notify.set_notifier(
        lambda text: bot.send_message(settings.telegram_user_id, text))
    try:
        draft, version, result = await regenerate_voiceover(d)
        await send_draft_preview(bot, d)
        print(f"DONE: v{version} re-voiced -> {result.video}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
