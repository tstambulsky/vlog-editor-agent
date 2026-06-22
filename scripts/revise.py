"""Apply text feedback to a day's current draft (the revision path), then send
the refreshed Telegram preview. Same path the bot uses when you reply to a draft
with feedback — exposed as a CLI so a revision can be driven without Telegram.

  docker compose -f docker-compose.home.yml run --rm --no-deps -e PYTHONPATH=/srv/app \
      auto-vlog python scripts/revise.py [YYYY-MM-DD] "feedback text"
"""
import asyncio
import sys
from datetime import date, datetime

from aiogram import Bot

from app import db, notify
from app.bot import send_draft_preview
from app.config import settings
from app.pipeline import apply_feedback


async def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit('usage: revise.py YYYY-MM-DD "feedback text"')
    d = date.fromisoformat(sys.argv[1])
    feedback = sys.argv[2]

    db.connect()
    bot = Bot(token=settings.telegram_bot_token)
    notify.set_notifier(
        lambda text: bot.send_message(settings.telegram_user_id, text))
    try:
        draft, version, _result, changed = await apply_feedback(d, feedback)
        print(f"v{version} '{draft.theme}' — regenerated: {changed}")
        await send_draft_preview(bot, d)
        print("DONE")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
