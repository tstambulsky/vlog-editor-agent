"""Per-day camera-clock corrections — a CHRONOLOGY concern only.

The Ray-Ban Meta glasses intermittently stamp clips with a WRONG date (their
camera clock drifts/resets), so an otherwise-good clip lands on the wrong day and
scrambles the capture-order sort against the iPhone clips. We can't trust the raw
probe for those, so two things live in a sibling file in the day's folder
(`clockfix.json`, next to notes/) rather than in the DB/Draft:

    {"overrides": {"<filename>": "<iso_local>"}, "suspect": ["<filename>", ...]}

- `overrides` are AUTHORITATIVE local-ISO capture times (sticky, highest
  priority): a clip with an override always sorts at that time, even though the
  raw probe keeps returning the wrong stamp on every rebuild — that's what makes
  the fix idempotent. Auto-detected wrong-clock clips get an override pinned to
  the day (same time-of-day, corrected date); `/retime` overwrites it with the
  founder's exact HH:MM.
- `suspect` is the auto-flagged set, shown with a ⚠️ in the script-review gate so
  the founder knows which clips to double-check (and clear via `/retime`).

Storage outside the Draft/DB is deliberate (same reasoning as `shoutout`/`hook`):
it keeps the beat-sync EDL untouched and the correction deterministic. `apply` is
re-run on every build and re-pins the sticky override from the sidecar.
"""
import json
from datetime import date, datetime
from pathlib import Path

from app.config import settings


def _path(d: date) -> Path:
    return settings.day_dir(d) / "clockfix.json"


def _load(d: date) -> dict:
    p = _path(d)
    if not p.exists():
        return {"overrides": {}, "suspect": []}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"overrides": {}, "suspect": []}
    data.setdefault("overrides", {})
    data.setdefault("suspect", [])
    return data


def _save(d: date, data: dict) -> None:
    p = _path(d)
    if not data.get("overrides") and not data.get("suspect"):
        p.unlink(missing_ok=True)  # no corrections -> no artifact
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False))


def get_overrides(d: date) -> dict[str, str]:
    return dict(_load(d).get("overrides", {}))


def get_suspects(d: date) -> list[str]:
    return list(_load(d).get("suspect", []))


def set_override(d: date, filename: str, iso_local: str) -> None:
    """Pin an authoritative local-ISO capture time for a clip (sticky)."""
    data = _load(d)
    data["overrides"][filename] = iso_local
    _save(d, data)


def add_suspect(d: date, filename: str) -> None:
    data = _load(d)
    if filename not in data["suspect"]:
        data["suspect"].append(filename)
    _save(d, data)


def remove_suspect(d: date, filename: str) -> None:
    data = _load(d)
    if filename in data["suspect"]:
        data["suspect"].remove(filename)
    _save(d, data)


def local_for_day(d: date, hh: int, mm: int) -> str:
    """ISO datetime for journal day `d` at hh:mm in the trip timezone."""
    dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=settings.tz)
    return dt.isoformat()


def epoch_of(iso_local: str) -> float:
    """POSIX epoch of an aware ISO local string (0.0 if unparseable)."""
    if not iso_local:
        return 0.0
    try:
        return datetime.fromisoformat(iso_local).timestamp()
    except ValueError:
        return 0.0


def redate_to_day(d: date, created_local: str) -> str:
    """Given an off-day local ISO, keep its TIME-OF-DAY but land it on journal
    day `d` (in the trip tz). Returns "" if the input can't be parsed."""
    if not created_local:
        return ""
    try:
        dt = datetime.fromisoformat(created_local)
    except ValueError:
        return ""
    dt = dt.replace(year=d.year, month=d.month, day=d.day, tzinfo=settings.tz)
    return dt.isoformat()


def apply(d: date, meta):
    """Correct/pin a fresh ClipMeta's capture time for journal day `d`.

    Priority:
      1. An override for this clip wins (STICKY) — set created_local/epoch from
         it. Re-applied on every build from the sidecar, so a wrong-clock clip
         whose raw probe never gets better still sorts at the corrected time.
      2. Else, if the parsed DATE of the clip's created_local != `d`, it's a
         wrong-clock clip: redate it to `d` (same time-of-day), write that as an
         override, flag it suspect, and apply the redated time.
      3. Otherwise leave it alone.

    Returns the (possibly mutated) meta. Idempotent across rebuilds.
    """
    overrides = get_overrides(d)
    iso = overrides.get(meta.filename)
    if iso:
        meta.created_local = iso
        meta.created_epoch = epoch_of(iso)
        return meta

    local = meta.created_local or ""
    parsed_date = local[:10]  # ISO date prefix, e.g. 2026-06-12
    if parsed_date and parsed_date != d.isoformat():
        redated = redate_to_day(d, local)
        if redated:
            set_override(d, meta.filename, redated)
            add_suspect(d, meta.filename)
            meta.created_local = redated
            meta.created_epoch = epoch_of(redated)
    return meta
