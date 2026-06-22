"""Re-render a day's current draft reusing its existing voiceovers
(e.g., after a renderer fix), then send the refreshed preview.

  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app auto-vlog \
      python scripts/rerender.py [YYYY-MM-DD]
"""
import asyncio
import sys
from datetime import date, datetime

from aiogram import Bot

from app import db, notify
from app.bot import send_draft_preview
from app.config import settings
from app.pipeline import _mp3_duration, day_dirs, stage_render
from app.schemas import Draft


async def main() -> None:
    d = (date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
         else datetime.now(settings.tz).date())
    db.connect()
    row = db.get_draft(d)
    if not row:
        raise SystemExit(f"no draft for {d}")
    draft = Draft.model_validate_json(row["draft_json"])
    version = row["version"]
    dirs = day_dirs(d)

    bot = Bot(token=settings.telegram_bot_token)
    notify.set_notifier(
        lambda text: bot.send_message(settings.telegram_user_id, text))
    try:
        langs = ["en"] + (["es"] if draft.has_spanish else [])
        for lang in langs:
            vo_rel = f"work/voiceover/vo_v{version}_{lang}.mp3"
            vo_dur = _mp3_duration(dirs["base"] / vo_rel)
            if vo_dur <= 0:
                raise SystemExit(f"missing voiceover {vo_rel}")
            result = await stage_render(d, draft, version, vo_rel, vo_dur, lang=lang)
            print(f"{lang}: vo={vo_dur:.1f}s video={result.duration_s:.1f}s")
        db.set_status(d, "awaiting_approval")
        await send_draft_preview(bot, d)
        print("DONE")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
