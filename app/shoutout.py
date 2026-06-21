"""Per-day X shoutout handles — a TEXT-POST concern only.

The founder can ask to mention a few X handles in the day's post to drive
interaction (people who showed them around, etc.). These people do NOT appear
in the video and never touch the EDL/narration/Draft schema — the shoutout
lives in a sibling file in the day's folder (`shoutouts.json`, next to notes/)
and is appended to the X caption at preview/publish time.

Storage outside the Draft is deliberate: it keeps the beat-sync EDL untouched
(so it can't trip the pre-refactor Draft.model_validate break) and keeps the
shoutout deterministic — the @handles always go out verbatim, never paraphrased
by the LLM.
"""
import json
import re
from datetime import date

from app.config import settings

# X handles: 1-15 word chars, optionally prefixed with @. We store them bare.
_HANDLE_RE = re.compile(r"@?([A-Za-z0-9_]{1,15})")

_LEAD = {
    "en": "Shoutout to",
    "es": "Shoutout a",
}


def _path(d: date):
    return settings.day_dir(d) / "shoutouts.json"


def parse_handles(text: str) -> list[str]:
    """Extract bare handles from free text ('@a, @b' or 'a b'), de-duped,
    order-preserving. Structured extraction — not an intent classifier."""
    seen: dict[str, None] = {}
    for raw in re.split(r"[\s,]+", text.strip()):
        if not raw:
            continue
        m = _HANDLE_RE.fullmatch(raw)
        if m:
            seen.setdefault(m.group(1), None)
    return list(seen)


def get_handles(d: date) -> list[str]:
    p = _path(d)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [h for h in data.get("handles", []) if h]


def set_handles(d: date, handles: list[str]) -> None:
    """Replace the day's handles. Empty list clears the file (no artifact)."""
    p = _path(d)
    if not handles:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"handles": handles}, ensure_ascii=False))


def shoutout_line(handles: list[str], lang: str = "en") -> str:
    """Render the shoutout sentence, e.g.
    'Shoutout to the friends who showed me so much of SF today: @a and @b.'
    Returns '' for no handles."""
    if not handles:
        return ""
    mentions = [f"@{h}" for h in handles]
    if len(mentions) == 1:
        joined = mentions[0]
    elif len(mentions) == 2:
        joined = f"{mentions[0]} and {mentions[1]}" if lang == "en" \
            else f"{mentions[0]} y {mentions[1]}"
    else:
        sep = "and" if lang == "en" else "y"
        joined = ", ".join(mentions[:-1]) + f", {sep} {mentions[-1]}"
    return f"{_LEAD.get(lang, _LEAD['en'])} {joined}."


def apply_to_caption(caption: str, d: date, platform: str, lang: str = "en") -> str:
    """Append the shoutout line to a caption when handles exist.

    X gets the literal @handles (they resolve and drive interaction). On
    LinkedIn bare X @handles don't link to anyone and we don't have the
    people's real names, so we add nothing there — the reliable behavior is
    the X shoutout. Skips silently if no handles or already present.
    """
    if platform != "twitter":
        return caption
    line = shoutout_line(get_handles(d), lang)
    if not line or line in caption:
        return caption
    sep = "\n\n" if caption and not caption.endswith("\n") else ""
    return f"{caption}{sep}{line}"
