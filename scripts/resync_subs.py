"""Re-time subtitles/beats for a day's CURRENT draft by re-aligning the
EXISTING voiceover (no new TTS — same audio, same version), then re-render and
send the refreshed preview. Use after the forced-alignment fix to fix "out of
sync" subs on an already-voiced day.

  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app vlog-agent \
      python scripts/resync_subs.py [YYYY-MM-DD]
"""
import asyncio
import sys
from datetime import date, datetime

from aiogram import Bot

from app import db, notify
from app.bot import send_draft_preview
from app.config import settings
from app.pipeline import stage_render, stage_voiceover
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

    bot = Bot(token=settings.telegram_bot_token)
    notify.set_notifier(
        lambda text: bot.send_message(settings.telegram_user_id, text))
    try:
        langs = ["en"] + (["es"] if settings.bilingual and draft.has_spanish
                          else [])
        for lang in langs:
            script = (draft.voiceover_script if lang == "en"
                      else draft.voiceover_script_es)
            # mp3 already exists for this version -> stage_voiceover skips TTS
            # and only recomputes word timings (WhisperX) + the .ass file.
            vo_rel, vo_dur = await stage_voiceover(d, version, script, lang=lang)
            result = await stage_render(d, draft, version, vo_rel, vo_dur,
                                        lang=lang)
            print(f"{lang}: vo={vo_dur:.1f}s video={result.duration_s:.1f}s")
        db.set_status(d, "awaiting_approval")
        await send_draft_preview(bot, d)
        print("DONE")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
