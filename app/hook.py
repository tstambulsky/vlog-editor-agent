"""Per-day first-frame hook selection — a RENDER concern only.

The agent proposes a few 1-2s cold-open "hooks" (the most attention-grabbing
moment across the day's clips) on the Draft; the founder picks one in the
script-review gate. Like the X shoutout, the *selection* lives in a sibling file
in the day's folder (`hook.json`, next to notes/) rather than in the Draft/EDL,
so it can't perturb the beat-sync EDL and is applied deterministically at render
time. The candidates themselves ride inside the Draft (`hook_candidates`); this
file just records which one (or none) the founder chose.

hook.json shapes:
    {"clip": "IMG_0231.MOV", "start_s": 3.0, "duration_s": 1.5}  # a chosen hook
    {"none": true}                                                # cold-open off
No file → fall back to the agent's top candidate (when the feature is on).
"""
import json
from datetime import date

from app.config import settings
from app.schemas import Draft, HookCandidate


def _path(d: date):
    return settings.day_dir(d) / "hook.json"


def select(d: date, draft: Draft, idx: int | None) -> HookCandidate | None:
    """Record the founder's pick. idx into draft.hook_candidates, or None to turn
    the cold-open off. Returns the chosen candidate (None when turned off)."""
    p = _path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    if idx is None or not (0 <= idx < len(draft.hook_candidates)):
        p.write_text(json.dumps({"none": True}))
        return None
    h = draft.hook_candidates[idx]
    p.write_text(h.model_dump_json())
    return h


def clear(d: date) -> None:
    """Forget any prior pick (a fresh draft re-offers from candidate 0)."""
    _path(d).unlink(missing_ok=True)


def resolve(d: date, draft: Draft) -> HookCandidate | None:
    """The hook to actually prepend at render time.

    An explicit pick in hook.json wins (including an explicit "no hook"). With no
    pick on file, default to the agent's top candidate when the feature is on —
    so the cold-open works even in fully-automatic mode (gate off)."""
    p = _path(d)
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        if data.get("none"):
            return None
        if data.get("clip"):
            try:
                return HookCandidate.model_validate(data)
            except Exception:
                pass
    if settings.first_frame_hook and draft.hook_candidates:
        return draft.hook_candidates[0]
    return None
