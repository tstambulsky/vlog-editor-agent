"""Reasoning-layer smoke test: analyze synthetic clips, then have the REAL
Claude Code CLI draft the day. Requires Claude to be logged in on the host;
no other credentials.

Run on the server:
  docker compose run --rm --no-deps -e DATA_DIR=/tmp/vj -e PYTHONPATH=/srv/app \
      auto-vlog python scripts/smoke_draft.py
"""
import asyncio
import json

from app import db, pipeline
from app.config import settings
from scripts.smoke_render import make_clips

DAY = settings.trip_start_date


async def main() -> None:
    db.connect()
    raw = settings.day_dir(DAY) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    make_clips(raw)
    db.get_or_create_day(DAY)

    metas = await pipeline.stage_analyze(DAY)
    print("analyzed:", ", ".join(metas))

    notes = settings.day_dir(DAY) / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "note_1200.md").write_text(
        "Heads up: this is a TEST day with synthetic color-bar clips, there is "
        "nothing real to see in the footage. Write the draft anyway as if it "
        "were day 1: arriving in San Francisco and setting up this automated "
        "video journal experiment."
    )

    draft, version = await pipeline.stage_draft(DAY)
    print(json.dumps(draft.model_dump(), ensure_ascii=False, indent=2))
    print(f"\nOK: draft v{version}, EDL total {draft.total_seconds:.1f}s, "
          f"script {len(draft.voiceover_script.split())} words, "
          f"caption_x {len(draft.caption_x)} chars")


if __name__ == "__main__":
    asyncio.run(main())
