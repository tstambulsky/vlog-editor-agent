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

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
EDITORIAL_PATH = PROMPTS_DIR / "editorial.md"
# Anti-AI-writing stack: humanizer + stop-slop are vendored open-source skills
# (MIT); x-voice.md is this repo's own house rules and wins on conflicts.
WRITING_DIR = PROMPTS_DIR / "writing"
HUMANIZER_PATH = WRITING_DIR / "humanizer.md"
STOP_SLOP_PATH = WRITING_DIR / "stop-slop.md"
REFERENCE_VOICES_PATH = WRITING_DIR / "reference-voices.md"
HOUSE_RULES_PATH = WRITING_DIR / "x-voice.md"


def editorial() -> str:
    """The series bible for THIS trip (place/days filled in) + the house writing
    rules, so every drafting/revision prompt carries the same voice contract."""
    bible = (EDITORIAL_PATH.read_text()
             .replace("{{PLACE_LOWER}}", settings.trip_place.lower())
             .replace("{{PLACE}}", settings.trip_place)
             .replace("{{DAYS}}", str(settings.trip_days)))
    return f"{bible}\n\n{HOUSE_RULES_PATH.read_text()}"


def writing_rules() -> str:
    """Full anti-slop reference for the humanize + review passes. The house
    rules go LAST because they override the generic skills on conflicts. Any
    reference file except the house rules is optional (skipped if missing)."""
    parts = [("Humanizer skill", HUMANIZER_PATH), ("Stop-slop skill", STOP_SLOP_PATH),
             ("Reference voices on X (measured)", REFERENCE_VOICES_PATH),
             ("HOUSE RULES (override everything above)", HOUSE_RULES_PATH)]
    return "\n\n".join(f"<<<<< {title} >>>>>\n{path.read_text()}"
                       for title, path in parts
                       if path.exists() or path == HOUSE_RULES_PATH)


def tweet_first_line(d: date) -> str:
    return f"day {settings.day_number(d)}/{settings.trip_days} in {settings.trip_place.lower()}"


def _trip_context_section() -> str:
    return settings.trip_context.strip() or "(no extra context about the trip)"


def corrections_section(limit: int = 15) -> str:
    """The founder's past corrections (script + tweet feedback, all days): the
    best signal of what this drafting loop keeps getting wrong."""
    rows = db.recent_corrections(limit)
    if not rows:
        return "(none yet)"
    return "\n".join(f"- [{r['kind']}, {r['date']}] {r['text']}" for r in rows)

def draft_schema_doc() -> str:
    es_edl = '"narration_es": "the same beat in natural Spanish", ' if settings.bilingual else ""
    es_fields = """
  "caption_x_es": "Spanish X caption, <=600 chars",
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
  "caption_x": "X caption in the tweet format of the House rules (fixed first line), <=600 chars",
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
    base = f"""{editorial()}

# Today's task — Day {day_n}/{settings.trip_days} ({d.isoformat()})
The tweet's first line today is exactly: `{tweet_first_line(d)}`

## About this trip
{_trip_context_section()}

## The founder's past corrections (do NOT repeat these mistakes)
{corrections_section()}

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


def build_humanize_prompt(beats: list[str], caption_x: str, caption_linkedin: str,
                          *, first_line: str, review_notes: str = "") -> str:
    """Rewrite pass over the spoken narration (one beat per EDL entry) and the
    captions, under the full anti-slop stack (humanizer + stop-slop + founder
    voice + house rules). First pass: review_notes empty. Later passes carry the
    reviewer's issues + the mechanical lint, and must fix exactly those.
    The narration is rewritten beat-by-beat, NOT flattened, so the EDL
    beat->clip mapping and the beat-sync timing survive."""
    n = len(beats)
    beats_json = json.dumps(beats, ensure_ascii=False, indent=1)
    notes = f"""
## A reviewer scored the previous version below the bar. Fix EVERY issue listed:
{review_notes}
""" if review_notes else ""
    return f"""{writing_rules()}

---
# Now apply everything above to THIS travel-video copy

You are given (a) the spoken NARRATION as an ordered list of beats and (b) two
social CAPTIONS. Rewrite them to remove the AI-writing patterns described above,
keeping the meaning, the facts, and the first-person founder voice. Follow the
HOUSE RULES where they differ from the skills (narration = spoken rules, tweet =
tweet rules). If a beat or caption is already clean, keep it word for word.
{notes}
## The founder's past corrections (these are rules too)
{corrections_section()}

HARD CONSTRAINTS (the video is already cut to these; breaking them breaks the render):
- Return EXACTLY {n} narration beats, in the same order. Beat i must still cover
  the same moment as input beat i: never merge, split, drop, or reorder beats.
- Keep each beat within ±15% of its original word count: each clip stays on
  screen for exactly as long as its beat is spoken, so length changes desync the cut.
- caption_x must stay ≤ 600 characters, start with the exact line
  `{first_line}`, and follow the House rules tweet format (case, emoji and
  ending rules included). Keep every @handle verbatim, including its case.
- Preserve every proper noun, event name and @handle; do not invent, translate,
  or drop names, places, or facts. These are English; do not translate.

Narration beats (JSON list, {n} items):
{beats_json}

caption_x:
{caption_x}

caption_linkedin:
{caption_linkedin}

Reply with ONLY this JSON, no other text:
{{"narrations": ["beat 1 rewritten", "... {n} items, same order"], "caption_x": "...", "caption_linkedin": "..."}}
"""


def build_review_prompt(narration: str, caption_x: str, *, lint: list[str]) -> str:
    """Independent reviewer: SCORES the copy on the house rubric, never rewrites.
    Kept separate from the writer so the pass that wrote the text isn't the one
    grading it. `lint` = mechanical findings the code already detected."""
    lint_txt = "\n".join(f"- {x}" for x in lint) or "- (none)"
    return f"""{writing_rules()}

---
# Review task: score, don't rewrite

You are a strict editor reviewing a founder's daily video journal before it goes
out. Score the NARRATION (spoken; judge it with the narration rules) and the
TWEET (judge it with the tweet rules) separately on the House rules rubric:
directness, rhythm, trust, authenticity, density, 1-10 each, total out of 50.
Apply the automatic caps. Be hard to impress: 45+ means a reader could not tell
an AI touched it.

For every point you took off, add one issue: quote the exact offending words and
say what to do instead, prefixed with "narration:" or "tweet:". No issue for
things that are fine.

The founder's past corrections also count as rules:
{corrections_section()}

Mechanical problems the system already found (each one must appear in issues):
{lint_txt}

NARRATION:
{narration}

TWEET:
{caption_x}

Reply with ONLY this JSON, no other text:
{{"narration_score": 0, "tweet_score": 0, "issues": ["narration: \\"...\\" -> ...", "tweet: \\"...\\" -> ..."]}}
"""


def build_caption_trim_prompt(script: str) -> str:
    return f"""This voiceover script is too long to fit in 60 seconds when spoken.
Shorten it to 100-115 words, keeping the hook, the story, and the reflection.
Reply with ONLY this JSON: {{"voiceover_script": "..."}}

Script:
{script}
"""


def build_tweet_revision_prompt(current_tweet: str, feedback: str, *,
                                theme: str, script: str, first_line: str) -> str:
    """Rewrite ONLY the X caption (the tweet) per the founder's feedback, in the
    series voice. Does not touch the spoken script or the video; the caption is
    applied at publish time. Used by the script-review tweet editor."""
    return f"""{editorial()}

---
# Revise ONLY the X caption (the tweet)

You already wrote the tweet below for today's video. Rewrite it to address the
founder's feedback, in the tweet format and voice of the House rules above.
This does NOT touch the video or the spoken script, only the tweet.

HARD CONSTRAINTS:
- Start with the exact line `{first_line}`, then the body in the House rules
  tweet format. ≤ 600 characters.
- The founder's feedback wins over the format rules when they conflict.
- Stay truthful to the video: don't invent facts, places, people, or events that
  aren't in the theme/script. Keep any @handles verbatim.

The founder's past corrections (rules too):
{corrections_section()}

Today's theme: {theme}

The spoken script (context only, do NOT just repeat it):
{script}

Current tweet:
{current_tweet}

Founder's feedback on the tweet:
{feedback}

Reply with ONLY this JSON, no other text:
{{"caption_x": "the rewritten tweet"}}
"""
