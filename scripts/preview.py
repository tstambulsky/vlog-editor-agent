"""Fast low-res EN preview of a day's current draft for visual QA.

Reuses the existing voiceover + ASS subtitles (no TTS, no Claude), and renders
at PREVIEW_SCALE (default 540x960) with the ultrafast preset into a distinct
`output/dayNN_vM_en_fast.mp4`, so it never clobbers the publish-quality render.
A full iteration is ~6 min; this targets well under a minute.

  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app video-journal \
      python scripts/preview.py [YYYY-MM-DD]

Flags:
  --subs   also burn subtitles (default: off, for speed)
"""
import asyncio
import sys
import time
from datetime import date, datetime

from app import db
from app.config import settings
from app.pipeline import _mp3_duration, day_dirs, stage_render
from app.schemas import Draft


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    burn_subs = "--subs" in sys.argv
    d = (date.fromisoformat(args[0]) if args
         else datetime.now(settings.tz).date())

    db.connect()
    row = db.get_draft(d)
    if not row:
        raise SystemExit(f"no draft for {d}")
    draft = Draft.model_validate_json(row["draft_json"])
    version = row["version"]
    dirs = day_dirs(d)

    vo_rel = f"work/voiceover/vo_v{version}_en.mp3"
    vo_dur = _mp3_duration(dirs["base"] / vo_rel)
    if vo_dur <= 0:
        raise SystemExit(f"missing voiceover {vo_rel} — run a full build first")

    # toggle subtitle burn for this run only
    settings.burn_subtitles = burn_subs
    t0 = time.monotonic()
    result = await stage_render(d, draft, version, vo_rel, vo_dur,
                                lang="en", preview=True)
    elapsed = time.monotonic() - t0
    print(f"PREVIEW {result.video} "
          f"({result.duration_s:.1f}s video, subs={'on' if burn_subs else 'off'}, "
          f"scale={settings.preview_scale}, rendered in {elapsed:.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())
