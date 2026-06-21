"""Prompt builders for the Claude Code reasoning calls.

The CLI runs with cwd = the day's work folder, so prompts reference
relative paths (frames/, transcripts/) that Claude reads itself.
"""
import json
from datetime import date, datetime
from pathlib import Path

from app import db
from app.config import settings
from app.schemas import CLIP_ANALYSIS_VERSION, ClipAnalysis, is_photo

EDITORIAL_PATH = Path(__file__).resolve().parent.parent / "prompts" / "editorial.md"
HUMANIZER_PATH = Path(__file__).resolve().parent.parent / "prompts" / "humanizer.md"

def draft_schema_doc() -> str:
    es_edl = '"narration_es": "the same beat in natural Spanish", ' if settings.bilingual else ""
    es_fields = """
  "caption_x_es": "Spanish X caption, <=280 chars",
  "caption_linkedin_es": "Spanish LinkedIn caption",""" if settings.bilingual else ""
    hook_field = """
  "hook_candidates": [{"clip": "IMG_0231.MOV", "start_s": 6.0, "duration_s": 1.5,
                       "reason": "why this 1-2s moment grabs attention"}],""" if settings.first_frame_hook else ""
    return """{
  "theme": "short theme of the day",
  "story_summary": "2-3 sentences describing the story you chose",
  "edl": [{"clip": "IMG_0231.MOV", "start_s": 4.0,
           "narration": "the exact English sentence(s) spoken WHILE this clip is on screen",
           """ + es_edl + """"why": "reason this clip fits these words"}],
  "caption_x": "X caption, <=280 chars",
  "caption_linkedin": "LinkedIn caption",""" + es_fields + hook_field + """
  "confidence_notes": "anything you were unsure about (optional)"
}"""


HOOK_RULES = """
## Opening hook (cold-open) — hook_candidates
Also propose 2-3 hook_candidates: the single most scroll-stopping 1-2s VISUAL
moment across ALL of today's clips — the shot that makes someone stop scrolling
in the first second (motion, a face, sudden action, a striking or surprising
view). The hook is a COLD-OPEN played BEFORE the narrated story, so it is NOT
bound by capture order — pick any clip, any moment. For each candidate give the
clip filename (a VIDEO, never a PHOTO), start_s, duration_s (1.0-2.0), and a
one-line reason. The hook carries NO narration — it plays the clip's own sound.
Use the key-moment timestamps above to point at exact moments. Order them
best-first; the first is the default if the founder doesn't pick one.
"""


BILINGUAL_RULES = """
## Spanish version
Every EDL beat also needs narration_es, plus caption_x_es and caption_linkedin_es:
the SAME story told in natural, casual Latin American Spanish — written from
scratch, never a word-for-word translation. Keep each narration_es within ±15%
of its English beat's length so the cuts land similarly. The author is
Argentine; warm, direct Spanish is right, but keep it neutral for all of LATAM.
"""


def _local_time(c) -> str:
    """Human capture time in trip-local tz for the prompt. Prefers the
    tz-normalized `created_local` (set at analysis); falls back to the raw ts."""
    iso = (c["created_local"] if "created_local" in c.keys() else "") or c["created_ts"]
    if not iso:
        return "unknown time"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%H:%M (%a)")
    except ValueError:
        return iso


def _load_clip_analysis(d: date, stem: str) -> ClipAnalysis | None:
    """The cached per-clip vision summary, or None when absent/stale/corrupt
    (drafting then falls back to telling Claude to read the raw frames)."""
    path = settings.day_dir(d) / "work" / "clip_analysis" / f"{stem}.json"
    if not path.exists():
        return None
    try:
        a = ClipAnalysis.model_validate_json(path.read_text())
    except Exception:
        return None
    return a if a.analysis_version == CLIP_ANALYSIS_VERSION else None


def _clips_section(d: date) -> str:
    # db.get_clips returns clips in true capture order (tz-normalized epoch), so
    # this list is already chronological — present it as such to Claude. Each
    # clip leads with its CACHED vision summary (computed once at arrival); the
    # raw frames stay available as a relative path for verifying a detail. When
    # no valid summary exists yet, fall back to the old "read the frames" block.
    lines = []
    i = 0
    for c in db.get_clips(d):
        if is_photo(c["filename"]) and not settings.include_photos:
            continue  # photos disabled — the video is clips-only
        i += 1
        stem = Path(c["filename"]).stem
        when = _local_time(c)
        a = _load_clip_analysis(d, stem)
        if is_photo(c["filename"]):
            if a:
                lines.append(
                    f"{i}. {c['filename']}: PHOTO (still image), taken {when}\n"
                    f"   summary: {a.summary}\n"
                    + (f"   visual: {a.visual_details}\n" if a.visual_details else "")
                    + (f"   image: frames/{stem}/ (open it to verify a detail)").rstrip()
                )
            else:
                lines.append(
                    f"{i}. {c['filename']}: PHOTO (still image), taken {when}\n"
                    f"   image: frames/{stem}/ (look at it)"
                )
            continue
        transcript = ""
        if c["transcript_path"] and Path(c["transcript_path"]).exists():
            data = json.loads(Path(c["transcript_path"]).read_text())
            text = " ".join(s["text"].strip() for s in data.get("segments", []))
            transcript = text[:600] or "(no speech)"
        else:
            transcript = "(no transcript)"
        if a:
            moments = "; ".join(f"{m.timestamp_s:.0f}s {m.what}" for m in a.key_moments)
            block = [f"{i}. {c['filename']}: {c['duration_s']:.1f}s, recorded {when}",
                     f"   summary: {a.summary}"]
            if moments:
                block.append(f"   key moments: {moments}")
            if a.visual_details:
                block.append(f"   visual: {a.visual_details}")
            if not a.usable:
                block.append(f"   ⚠ low quality: {a.quality_notes or 'shaky/dark'}")
            block.append(f"   speech: {transcript}")
            block.append(f"   frames: frames/{stem}/ (open these only to verify a detail)")
            lines.append("\n".join(block))
        else:
            lines.append(
                f"{i}. {c['filename']}: {c['duration_s']:.1f}s, recorded {when}\n"
                f"   frames: frames/{stem}/ (look at them)\n"
                f"   speech: {transcript}"
            )
    return "\n".join(lines)


def _notes_section(d: date) -> str:
    notes_dir = settings.day_dir(d) / "notes"
    if not notes_dir.exists():
        return "(no notes today)"
    texts = [p.read_text().strip() for p in sorted(notes_dir.glob("*.md"))]
    return "\n---\n".join(t for t in texts if t) or "(no notes today)"


def _recent_scripts_section() -> str:
    recent = db.recent_published_drafts(3)
    if not recent:
        return "(none yet — this is one of the first days)"
    parts = []
    for r in recent:
        parts.append(f"Theme: {r.get('theme')}\nScript: {r.get('voiceover_script')}\n"
                     f"X caption: {r.get('caption_x')}")
    return "\n---\n".join(parts)


def _trip_so_far_section() -> str:
    themes = db.all_day_themes()
    if not themes:
        return "(this is the first day of the trip)"
    return "\n".join(
        f"- Day {n}: {theme}" + (" (not published)" if status in ("discarded", "skipped") else "")
        for n, theme, status in themes)


def build_clip_analysis_prompt(stem: str, *, is_photo: bool,
                               transcript: str) -> str:
    """One-clip vision summary, cached and reused at draft time. cwd is the day's
    work folder, so frames/<stem>/ is a relative path Claude reads itself."""
    if is_photo:
        frame_note = (f"The still image is in frames/{stem}/ (one file). Open it "
                      "and describe only what you can actually SEE.")
        moments_field = ""
    else:
        frame_note = (
            f"Keyframes are in frames/{stem}/ as f_001.jpg, f_002.jpg, … sampled "
            "one every 2 seconds (so f_NNN is at about (NNN-1)*2 seconds into the "
            "clip). Open several and describe only what you can actually SEE.")
        moments_field = ('  "key_moments": [{"timestamp_s": 0.0, "what": "what is '
                         'visible then"}],   // 1-5 notable moments, time order\n')
    return f"""Summarize one short {'photo' if is_photo else 'video clip'} for a \
daily travel video. {frame_note}

Speech transcribed from the clip (may be empty or noisy ASR):
{transcript or "(no speech)"}

Reply with ONLY one JSON object, no other text:
{{
  "summary": "1-2 sentences: what this clip shows",
{moments_field}  "visual_details": "people / location / notable action you can identify",
  "usable": true,
  "quality_notes": "set usable=false and explain ONLY if too dark/blurry/shaky to use; else empty"
}}

Describe only what is visible — do not invent places, names, or events. If you
can't tell, say so in the field rather than guessing."""


def build_draft_prompt(d: date, feedback: str | None = None,
                       previous_draft: dict | None = None) -> str:
    day_n = settings.day_number(d)
    base = f"""{EDITORIAL_PATH.read_text()}

# Today's task — Day {day_n}/30 ({d.isoformat()})

Create today's draft. You are in the day's work folder. Each clip below comes
with a pre-computed summary and key moments of what's in the footage — use those
to decide what to use. Open a clip's keyframes in frames/<clip-stem>/ only when
you need to verify a specific detail (a clip with no summary has a "look at them"
note — read those frames).

## Today's clips (listed in CAPTURE ORDER — earliest first; times are local)
{_clips_section(d)}

## The founder's own notes about today
## (ground truth — locations/people in the clips. Trust these over your guesses
## from the keyframes when they conflict, and map narration to the right clip.
## When a note names a specific place, person, or brand — e.g. "Naval Ravikant's
## AngelList founders' coffee" — name it that way in the narration. Do NOT
## generalize a named place into a vague description ("a members-only spot").)
{_notes_section(d)}

## The trip so far (the series is one continuous story — build on it, refer
## back when natural, and never repeat a theme)
{_trip_so_far_section()}

## Recently published days (do NOT repeat their phrasing or caption wording)
{_recent_scripts_section()}
{BILINGUAL_RULES if settings.bilingual else ""}
## Output
Reply with ONLY one JSON object, no other text, following exactly this shape:
{draft_schema_doc()}

Constraints — how the video is assembled (this is the contract):
- The video is built beat by beat, in EDL order. Each clip appears on screen
  for EXACTLY as long as its narration takes to speak
  (~{2.6 * settings.voice_speed:.1f} words/second). THE WORDS AND THE IMAGE
  MUST MATCH: never narrate something the viewer cannot see in that clip at
  that moment.
- 1-2 sentences per beat (roughly 4-10 seconds spoken). Use more beats with
  shorter narrations rather than long monologues over one clip.
- clip filenames must come from the list above; pick start_s so the clip has
  enough footage from there to cover its narration — if it runs out, the last
  frame freezes on screen (avoid this).
- VARIETY — no looping: prefer a DIFFERENT clip for every beat. Reuse a clip
  for at most 2 beats total, and only when the SECOND beat's start_s is LATER
  than the first (keep advancing forward through the clip — NEVER set a later
  beat of the same clip to an earlier or equal start_s; replaying a moment reads
  as an ugly loop). With N clips available, keep the beat count close to N — do
  NOT pad a longer story by repeating the same footage; write fewer, longer
  beats instead.
- Items marked PHOTO are still images: a photo beat holds the picture on
  screen for exactly its narration's length. Use one when the photo tells the
  moment better than any footage; always set start_s to 0 for photos, and
  keep photo beats on the shorter side (one sentence).
- Total narration across all beats: {settings.script_words_min}-{settings.script_words_max} words (≈{settings.script_words_min / (2.6 * settings.voice_speed):.0f}-{settings.script_words_max / (2.6 * settings.voice_speed):.0f} seconds).
- General observations/reflections are fine, but anchor them to the beat whose
  footage best illustrates them.
- The final video plays clips in CAPTURE ORDER (earliest first) — the system
  re-sorts the EDL by capture time after you write it. So write the narration as
  a story that flows in chronological order; don't plan visual flashbacks, they
  won't survive the sort.
"""
    if settings.first_frame_hook:
        base += HOOK_RULES
    if feedback and previous_draft:
        base += f"""
## Revision request
This is a REVISION of an existing draft. The founder said:
"{feedback}"

Current draft:
{json.dumps(previous_draft, ensure_ascii=False, indent=2)}

Apply the feedback. Keep everything the founder did not ask to change AS IS,
word for word — unchanged fields let the system skip regenerating audio/video.
"""
    return base


def build_humanize_prompt(beats: list[str], caption_x: str,
                          caption_linkedin: str) -> str:
    """Second-pass prompt: run the vendored `humanizer` skill over the spoken
    narration (one beat per EDL entry) and the two EN captions. The narration is
    rewritten beat-by-beat — NOT flattened — so the EDL beat→clip mapping and the
    beat-sync timing survive. cwd is the day's work folder (the skill's tool list
    is irrelevant here; we hand it the text inline)."""
    n = len(beats)
    beats_json = json.dumps(beats, ensure_ascii=False, indent=1)
    return f"""{HUMANIZER_PATH.read_text()}

---
# Now apply everything above to THIS travel-video copy

You are given (a) the spoken NARRATION as an ordered list of beats and (b) two
social CAPTIONS. Rewrite all of them to remove the AI-writing patterns described
above, keeping the meaning, the facts, and the casual first-person founder voice.

HARD CONSTRAINTS (the video is already cut to these — breaking them breaks the render):
- Return EXACTLY {n} narration beats, in the same order. Beat i must still cover
  the same moment as input beat i — never merge, split, drop, or reorder beats.
- Keep each beat within ±15% of its original word count: each clip stays on
  screen for exactly as long as its beat is spoken, so length changes desync the cut.
- caption_x must stay ≤ 280 characters.
- Preserve every proper noun, event name, @handle and #hashtag VERBATIM — do not
  invent, translate, or drop names, places, or facts. These are English; do not translate.

Narration beats (JSON list, {n} items):
{beats_json}

caption_x:
{caption_x}

caption_linkedin:
{caption_linkedin}

Reply with ONLY this JSON, no other text:
{{"narrations": ["beat 1 rewritten", "... {n} items, same order"], "caption_x": "...", "caption_linkedin": "..."}}
"""


def build_caption_trim_prompt(script: str) -> str:
    return f"""This voiceover script is too long to fit in 60 seconds when spoken.
Shorten it to 100-115 words, keeping the hook, the story, and the reflection.
Reply with ONLY this JSON: {{"voiceover_script": "..."}}

Script:
{script}
"""
