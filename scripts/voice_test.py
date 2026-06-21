"""Send A/B voice variations of today's script to Telegram as audio files.

Uses a shortened cut of the latest draft's English script so the samples are
comparable and cheap on ElevenLabs characters.

  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app video-journal \
      python scripts/voice_test.py [YYYY-MM-DD]
"""
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.types import FSInputFile

from app import db
from app.config import settings
from app.services import voiceover

# (label, voice name in the ElevenLabs library, settings)
ENERGETIC = {"stability": 0.35, "similarity_boost": 0.75, "style": 0.45}
CASUAL = {"stability": 0.45, "similarity_boost": 0.75, "style": 0.3}
VARIATION_SETS = {
    "voices": [
        ("1. Will — current setup (baseline)", "Will", voiceover.default_voice_settings()),
        ("2. Will — energetic settings", "Will", ENERGETIC),
        ("3. Liam — young creator energy", "Liam", ENERGETIC),
        ("4. Roger — laid-back founder", "Roger", CASUAL),
        ("5. Charlie — confident, punchy", "Charlie", ENERGETIC),
        ("6. Eric — smooth conversational", "Eric", CASUAL),
    ],
    "eric": [
        ("1. Eric — energetic, normal speed", "Eric", {**ENERGETIC, "speed": 1.0}),
        ("2. Eric — energetic, 10% faster", "Eric", {**ENERGETIC, "speed": 1.1}),
        ("3. Eric — max energy, 15% faster", "Eric",
         {"stability": 0.3, "similarity_boost": 0.75, "style": 0.55, "speed": 1.15}),
    ],
}


def short_cut(script: str, max_chars: int = 380) -> str:
    if len(script) <= max_chars:
        return script
    cut = script[:max_chars]
    return cut[:cut.rfind(".") + 1] or cut


async def voice_ids_by_name() -> dict[str, str]:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get("https://api.elevenlabs.io/v1/voices",
                             headers={"xi-api-key": settings.elevenlabs_api_key})
        r.raise_for_status()
    return {v["name"].split(" ")[0]: v["voice_id"] for v in r.json()["voices"]
            if v.get("category") == "premade"}


async def main() -> None:
    args = sys.argv[1:]
    set_name = next((a for a in args if a in VARIATION_SETS), "voices")
    date_arg = next((a for a in args if a.count("-") == 2), None)
    variations = VARIATION_SETS[set_name]
    d = (date.fromisoformat(date_arg) if date_arg
         else datetime.now(settings.tz).date())
    db.connect()
    row = db.get_draft(d)
    if not row:
        raise SystemExit(f"no draft for {d}")
    script = short_cut(json.loads(row["draft_json"])["voiceover_script"])
    print(f"sample script ({len(script)} chars): {script[:80]}…")

    ids = await voice_ids_by_name()
    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.send_message(
            settings.telegram_user_id,
            f"🎙 Voice test ({set_name}) — same lines. "
            "Reply with the number you like best.")
        for label, name, vsettings in variations:
            if name not in ids:
                print(f"skip {label}: voice '{name}' not in library")
                continue
            mp3 = Path(f"/tmp/voice_{label[0]}.mp3")
            await voiceover.generate(script, mp3, Path("/tmp/align.json"),
                                     voice_id=ids[name], voice_settings=vsettings)
            await bot.send_audio(settings.telegram_user_id, FSInputFile(mp3),
                                 title=label, caption=label)
            print("sent:", label)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
