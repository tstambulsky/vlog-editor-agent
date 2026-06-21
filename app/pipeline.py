"""Pipeline orchestration. Each stage is idempotent (checks artifacts/DB) so
re-running after a crash resumes where it left off. Telegram-free on purpose —
the bot/scheduler drive it and own the messaging."""
import asyncio
import json
import logging
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from app import db, hook as hooks, prompts
from app.claude_cli import run_structured
from app.notify import notify
from app.config import settings
from app.schemas import (CLIP_ANALYSIS_VERSION, MAX_SCRIPT_WORDS,
                         MIN_SCRIPT_WORDS, PHOTO_EXTS, ClipAnalysis,
                         ClipAnalysisReply, Draft, HumanizedText, is_photo,
                         sort_edl_chronologically, valid_hook_candidates,
                         validate_edl_against_clips)
from app.services import align, analyze, drive, render as render_svc, voiceover
from app.services.analyze import ClipMeta
from app.services.subtitles import write_ass
from app.services.voiceover import words_from_alignment

log = logging.getLogger(__name__)
_pipeline_lock = asyncio.Lock()
# Serializes eager per-clip sweeps against each other. Distinct from
# _pipeline_lock (a full build): a sweep yields to a running build, never blocks
# it. See eager_sweep.
_eager_lock = asyncio.Lock()
HEIC_EXTS = {".heic", ".heif"}


class PipelineError(RuntimeError):
    pass


def day_dirs(d: date) -> dict[str, Path]:
    base = settings.day_dir(d)
    dirs = {
        "base": base, "raw": base / "raw", "notes": base / "notes",
        "work": base / "work", "frames": base / "work" / "frames",
        "transcripts": base / "work" / "transcripts",
        "clip_analysis": base / "work" / "clip_analysis",
        "voiceover": base / "work" / "voiceover", "subs": base / "work" / "subs",
        "output": base / "output",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


async def stage_sync(d: date) -> list[str]:
    dirs = day_dirs(d)
    db.get_or_create_day(d)
    new = await drive.sync_day(d)
    if any(dirs["raw"].iterdir()):
        db.set_status(d, "synced")
    return new


def _media_files(d: date) -> list[Path]:
    """Raw media to process, name-sorted. Photos/HEIC only when enabled (off by
    default — clips-only). A HEIC original is dropped when its converted .jpg
    sibling already exists, so the pair is never processed twice (same stem)."""
    dirs = day_dirs(d)
    if not dirs["raw"].exists():
        return []
    photo_exts = PHOTO_EXTS if settings.include_photos else set()
    heic_exts = HEIC_EXTS if settings.include_photos else set()
    exts = drive.VIDEO_EXTS | photo_exts | heic_exts
    files = sorted(p for p in dirs["raw"].iterdir() if p.suffix.lower() in exts)
    jpg_stems = {p.stem for p in files if p.suffix.lower() == ".jpg"}
    return [p for p in files
            if not (p.suffix.lower() in heic_exts and p.stem in jpg_stems)]


def _clip_needs_processing(d: date, clip: Path) -> bool:
    """True unless the clip's MECHANICAL artifacts (keyframes + transcript) are
    already on disk. The vision summary is handled inside ensure_clip_processed
    (skip-if-valid) and deliberately NOT gated here: a clip whose summary failed
    is retried at the next build, not every sweep — avoids hammering the
    reasoning layer during a sustained outage."""
    dirs = day_dirs(d)
    frames = dirs["frames"] / clip.stem
    transcript = dirs["transcripts"] / f"{clip.stem}.json"
    has_frames = frames.exists() and any(frames.glob("*.jpg"))
    return not (has_frames and transcript.exists())


async def ensure_clip_analysis(d: date, clip_name: str, *, is_photo: bool) -> None:
    """Cache a per-clip vision summary to work/clip_analysis/<stem>.json,
    skip-if-valid. Best-effort: a failure logs and returns (drafting falls back
    to reading frames). Goes through the reasoning layer like every LLM call."""
    dirs = day_dirs(d)
    stem = Path(clip_name).stem
    out = dirs["clip_analysis"] / f"{stem}.json"
    if out.exists():
        try:
            cached = ClipAnalysis.model_validate_json(out.read_text())
            if cached.analysis_version == CLIP_ANALYSIS_VERSION:
                return
        except (ValidationError, ValueError):
            pass  # corrupt / older version -> recompute
    transcript = ""
    t_path = dirs["transcripts"] / f"{stem}.json"
    if t_path.exists():
        try:
            data = json.loads(t_path.read_text())
            transcript = " ".join(s["text"].strip()
                                  for s in data.get("segments", []))[:600]
        except (json.JSONDecodeError, KeyError, TypeError):
            transcript = ""
    prompt = prompts.build_clip_analysis_prompt(stem, is_photo=is_photo,
                                                transcript=transcript)
    try:
        reply = await run_structured(prompt, cwd=dirs["work"],
                                     schema=ClipAnalysisReply)
    except Exception as exc:
        db.log_event(d, "clip_analysis_failed", f"{clip_name}: {str(exc)[:200]}")
        return
    analysis = ClipAnalysis(analysis_version=CLIP_ANALYSIS_VERSION,
                            clip=clip_name, is_photo=is_photo,
                            transcript_excerpt=transcript, **reply.model_dump())
    dirs["clip_analysis"].mkdir(parents=True, exist_ok=True)
    out.write_text(analysis.model_dump_json(indent=1))


async def ensure_clip_processed(d: date, clip: Path) -> ClipMeta | None:
    """Process ONE clip end to end (HEIC convert -> keyframes/transcript -> DB
    upsert -> vision summary) and return its ClipMeta. Idempotent and
    self-contained; returns None (logging a quarantine) for a clip that fails to
    probe. Never raises for a single bad clip — the caller keeps going."""
    dirs = day_dirs(d)
    if settings.include_photos and clip.suffix.lower() in HEIC_EXTS:
        try:
            clip = await asyncio.to_thread(analyze.convert_heic, clip)
        except Exception as exc:
            db.log_event(d, "clip_quarantined", f"{clip.name}: HEIC conversion: {exc}")
            return None
    try:
        meta = await analyze.process_clip(clip, dirs["frames"], dirs["transcripts"])
    except RuntimeError as exc:
        db.log_event(d, "clip_quarantined", f"{clip.name}: {exc}")
        return None
    t_path = dirs["transcripts"] / f"{Path(meta.filename).stem}.json"
    db.upsert_clip(d, meta.filename, meta.duration_s, meta.created_ts,
                   meta.hdr, str(t_path),
                   created_local=meta.created_local,
                   created_epoch=meta.created_epoch)
    await ensure_clip_analysis(d, meta.filename, is_photo=meta.is_photo)
    return meta


async def eager_sweep(d: date) -> int:
    """Process every not-yet-processed clip in raw/ for `d`. Returns the count
    processed. Yields to a running build: if _pipeline_lock is held it does
    nothing (the build's stage_analyze will handle the clips), and it bails out
    between clips if a build starts mid-sweep."""
    if _pipeline_lock.locked():
        return 0
    async with _eager_lock:
        pending = [p for p in _media_files(d) if _clip_needs_processing(d, p)]
        count = 0
        for clip in pending:
            if _pipeline_lock.locked():
                break
            if await ensure_clip_processed(d, clip) is not None:
                count += 1
        return count


async def safe_eager_sweep(d: date) -> None:
    """Fire-and-forget wrapper for triggers (bot/scheduler): never propagates."""
    try:
        await eager_sweep(d)
    except Exception:
        log.exception("eager sweep failed for %s", d)


async def stage_analyze(d: date) -> dict[str, ClipMeta]:
    dirs = day_dirs(d)
    media = _media_files(d)
    if not media:
        raise PipelineError("No clips in raw/ — nothing to analyze")
    metas: dict[str, ClipMeta] = {}
    for clip in media:  # idempotent: self-heals anything eager processing missed
        meta = await ensure_clip_processed(d, clip)
        if meta is not None:
            metas[meta.filename] = meta
    if not metas:
        raise PipelineError("All clips failed ffprobe validation")
    (dirs["work"] / "clips.json").write_text(json.dumps(
        {k: vars(v) for k, v in metas.items()}, indent=1))
    db.set_status(d, "analyzed")
    return metas


def load_clip_metas(d: date) -> dict[str, ClipMeta]:
    path = settings.day_dir(d) / "work" / "clips.json"
    data = json.loads(path.read_text())
    return {k: ClipMeta(**v) for k, v in data.items()}


async def stage_draft(d: date, feedback: str | None = None) -> tuple[Draft, int]:
    dirs = day_dirs(d)
    day = db.get_or_create_day(d)
    version = (day["current_version"] or 0) + 1
    previous = None
    if feedback:
        row = db.get_draft(d)
        previous = json.loads(row["draft_json"]) if row else None
    clip_durations = {c["filename"]: c["duration_s"] for c in db.get_clips(d)}
    # Backfill any missing/stale vision summaries so the prompt is summary-led
    # even on the first draft / old days / after a CLIP_ANALYSIS_VERSION bump.
    # Free when the cache is already valid (skip-if-valid).
    for c in db.get_clips(d):
        if is_photo(c["filename"]) and not settings.include_photos:
            continue
        await ensure_clip_analysis(d, c["filename"],
                                   is_photo=is_photo(c["filename"]))
    prompt = prompts.build_draft_prompt(d, feedback=feedback, previous_draft=previous)

    def _validate(dr: Draft) -> list[str]:
        errors = validate_edl_against_clips(dr, clip_durations)
        if settings.bilingual and not dr.has_spanish:
            errors.append("voiceover_script_es is required (bilingual mode): "
                          "write the Spanish version of the script and captions")
        return errors

    draft = await run_structured(prompt, cwd=dirs["work"], schema=Draft,
                                 extra_validate=_validate)

    # Code enforces chronological order: Claude picks clips by narrative, but the
    # visuals must never jump backwards in time. Sorting the EDL also rebuilds the
    # voiceover script (a computed field over edl[].narration), so audio and
    # footage stay in lock-step. See schemas.sort_edl_chronologically.
    clip_epoch = {c["filename"]: (c["created_epoch"] or 0.0) for c in db.get_clips(d)}
    draft, was_out_of_order = sort_edl_chronologically(draft, clip_epoch)
    if was_out_of_order:
        order = " -> ".join(e.clip for e in draft.edl)
        log.info("EDL was out of capture order; re-sorted to: %s", order)
        db.log_event(d, "edl_resorted", f"v{version}: {order}")
    epochs = [clip_epoch.get(e.clip, 0.0) for e in draft.edl]
    assert epochs == sorted(epochs), f"EDL not chronological after sort: {epochs}"

    # Fresh drafts only: run the humanizer pass. Skipped on revisions so it can't
    # overwrite the founder's explicit wording (and so apply_feedback's
    # unchanged-field re-render skip still works).
    if settings.humanize and not feedback:
        draft = await stage_humanize(d, draft)

    # Keep only hook candidates the renderer can honor (existing video clip with
    # enough footage), and reset any prior pick so this draft re-offers from its
    # own candidate 0 (a stale pick could point at a clip this draft dropped).
    draft = draft.model_copy(update={
        "hook_candidates": valid_hook_candidates(draft, clip_durations)})
    hooks.clear(d)

    db.save_draft(d, version, draft.model_dump(),
                  trigger="revision" if feedback else "initial",
                  feedback_text=feedback)
    db.set_status(d, "drafted")
    return draft, version


async def stage_humanize(d: date, draft: Draft) -> Draft:
    """Second reasoning pass (the vendored `humanizer` skill): rewrite the spoken
    narration + the X/LinkedIn captions to strip AI-writing patterns. Rewrites
    each EDL beat IN PLACE (same count/order), so the beat→clip mapping and
    beat-sync timing are preserved. EN fields only; narration_es/why/clip/start_s
    are untouched. Best-effort: a failed/invalid pass logs and returns the draft
    unchanged rather than blocking the build."""
    beats = [e.narration for e in draft.edl]
    n = len(beats)
    prompt = prompts.build_humanize_prompt(beats, draft.caption_x,
                                           draft.caption_linkedin)

    def _validate(h: HumanizedText) -> list[str]:
        errors = []
        if len(h.narrations) != n:
            errors.append(f"return EXACTLY {n} narration beats, same order "
                          f"(got {len(h.narrations)})")
        if any(len(s.strip()) < 10 for s in h.narrations):
            errors.append("each narration beat must be at least 10 characters")
        total = len(" ".join(h.narrations).split())
        if not MIN_SCRIPT_WORDS <= total <= MAX_SCRIPT_WORDS:
            errors.append(f"total narration is {total} words — keep it between "
                          f"{MIN_SCRIPT_WORDS} and {MAX_SCRIPT_WORDS}")
        return errors

    try:
        h = await run_structured(prompt, cwd=day_dirs(d)["work"],
                                 schema=HumanizedText, extra_validate=_validate)
    except Exception:
        log.exception("humanizer pass failed — keeping the un-humanized draft")
        db.log_event(d, "humanize_failed", "kept original draft")
        return draft

    new_edl = [e.model_copy(update={"narration": h.narrations[i].strip()})
               for i, e in enumerate(draft.edl)]
    log.info("humanizer pass applied (%d beats + captions)", n)
    return draft.model_copy(update={
        "edl": new_edl,
        "caption_x": h.caption_x.strip(),
        "caption_linkedin": h.caption_linkedin.strip(),
    })


async def stage_voiceover(d: date, version: int, script: str,
                          lang: str = "en") -> tuple[str, float]:
    """Generate VO + ASS subtitles for one language. Returns (vo_rel, duration)."""
    dirs = day_dirs(d)
    stem = f"vo_v{version}_{lang}"
    mp3 = dirs["voiceover"] / f"{stem}.mp3"
    alignment_path = dirs["voiceover"] / f"alignment_v{version}_{lang}.json"
    if not mp3.exists():
        await voiceover.generate(script, mp3, alignment_path)

    # Timing source for subs AND beats. Preferred: WhisperX forced alignment of
    # the KNOWN script (wav2vec2) — tight word boundaries. Fallbacks, in order:
    # free-form whisper transcription, then ElevenLabs alignment (drifts ~1.5s).
    # Every source is ASR (mis-heard text), so we re-map onto the known script:
    # the ONLY thing that sets subtitle word TEXT is the script, never the ASR.
    aligned = True  # align_script already re-texted to the script internally
    try:
        words = await align.align_script(script, mp3, lang, _mp3_duration(mp3))
    except Exception as exc:  # torch missing, model load fail, bad alignment
        log.warning("forced alignment failed (%s); falling back to whisper", exc)
        words = await analyze.transcribe_words(mp3)
        aligned = False
    if not words:
        words = await analyze.transcribe_words(mp3)
        aligned = False
    if not words:
        data = json.loads(alignment_path.read_text()) if alignment_path.exists() else {}
        words = words_from_alignment(data.get("normalized_alignment")
                                     or data.get("alignment") or {})
        aligned = False
    if not aligned:  # fallback timing source: still force the script text on top
        words = align.retext_to_script(words, script)
    (dirs["voiceover"] / f"words_{stem.removeprefix('vo_')}.json").write_text(
        json.dumps(words))
    write_ass(words, dirs["subs"] / f"{stem}.ass")

    vo_dur = max((w["end"] for w in words), default=0.0) or _mp3_duration(mp3)
    if lang == "en":
        db.update_draft_paths(d, version, vo_path=str(mp3))
    return f"work/voiceover/{stem}.mp3", vo_dur


def _beat_durations(d: date, draft: Draft, version: int, lang: str,
                    vo_dur: float) -> list[float]:
    vo_dir = settings.day_dir(d) / "work" / "voiceover"
    words_file = vo_dir / f"words_v{version}_{lang}.json"
    if words_file.exists():  # whisper times measured on the actual audio
        return voiceover.beat_durations_from_words(
            json.loads(words_file.read_text()), draft.beats(lang), vo_dur)
    align_file = vo_dir / f"alignment_v{version}_{lang}.json"
    raw = {}
    if align_file.exists():
        raw = json.loads(align_file.read_text()).get("alignment") or {}
    return voiceover.beat_durations(raw, draft.beats(lang), vo_dur)


async def stage_render(d: date, draft: Draft, version: int, vo_rel: str,
                       vo_dur: float, lang: str = "en",
                       preview: bool = False) -> render_svc.RenderResult:
    dirs = day_dirs(d)
    result = await render_svc.render(
        dirs["base"], draft, load_clip_metas(d),
        vo_rel=vo_rel, ass_rel=f"work/subs/vo_v{version}_{lang}.ass",
        version=version, beat_durations=_beat_durations(d, draft, version, lang, vo_dur),
        vo_duration_s=vo_dur, suffix=f"_{lang}", preview=preview,
        hook=hooks.resolve(d, draft),
    )
    if preview:  # throwaway QA render — don't touch DB state or paths
        return result
    if lang == "en":
        db.update_draft_paths(d, version, video_path=str(result.video),
                              preview_path=str(result.preview))
    else:
        db.update_draft_paths(d, version, es_video_path=str(result.video),
                              es_preview_path=str(result.preview))
    db.set_status(d, "rendered")
    return result


async def _voiceover_and_render(d: date, draft: Draft, version: int,
                                lang: str) -> render_svc.RenderResult:
    script = draft.voiceover_script if lang == "en" else draft.voiceover_script_es
    vo_rel, vo_dur = await stage_voiceover(d, version, script, lang=lang)
    return await stage_render(d, draft, version, vo_rel, vo_dur, lang=lang)


async def run_pipeline(
    d: date,
) -> tuple[Draft, int, render_svc.RenderResult | None]:
    """sync -> analyze -> draft, then either:

    - SCRIPT_REVIEW on (default): STOP at `script_review` and return
      (draft, version, None). The caller sends the script to Telegram; voiceover
      + render happen later via `render_phase` once the founder approves.
    - SCRIPT_REVIEW off: keep going through VO + render to `awaiting_approval`
      (the original behavior), returning the EN RenderResult.

    Idempotent stages."""
    async with _pipeline_lock:
        day_n = settings.day_number(d)
        await notify(f"🤖 Agent started — building Day {day_n}/30")
        await stage_sync(d)
        metas = await stage_analyze(d)
        if not metas:
            raise PipelineError("No usable clips")
        total_s = sum(m.duration_s for m in metas.values())
        await notify(f"🔍 Analyzed {len(metas)} clips ({total_s/60:.1f} min of footage). "
                     "Claude is now watching them and writing the story…")
        draft, version = await stage_draft(d)
        if settings.script_review:
            db.set_status(d, "script_review")
            await notify(f"✍️ Script ready: “{draft.theme}”. Review it before I "
                         "spend time on the voiceover + render.")
            return draft, version, None
        await notify(f"✍️ Story ready: “{draft.theme}”. Generating the voiceover…")
        result = await _render_all(d, draft, version)
        return draft, version, result


async def _render_all(d: date, draft: Draft, version: int) -> render_svc.RenderResult:
    """Voiceover + render for EN (+ ES if bilingual), then awaiting_approval.
    The render half of a full run — shared by `run_pipeline` (gate off) and
    `render_phase` (gate on, after approval). Assumes the lock is already held."""
    day_n = settings.day_number(d)
    result = await _voiceover_and_render(d, draft, version, "en")
    if settings.bilingual and draft.has_spanish:
        await notify("🇪🇸 English video done. Building the Spanish version…")
        await _voiceover_and_render(d, draft, version, "es")
    db.set_status(d, "awaiting_approval")
    await notify(f"🤖 Agent finished — Day {day_n} draft took shape "
                 f"({result.duration_s:.0f}s video). Preview coming up.")
    return result


async def render_phase(d: date) -> tuple[Draft, int, render_svc.RenderResult]:
    """Approve-time half of the gated flow: take the reviewed script and run
    voiceover + render → `awaiting_approval`. Triggered by the "✅ Approve &
    build" button (and the gate-aware scripts). The script is NOT changed here;
    the EDL/clip mapping and chronological order already baked in by stage_draft
    carry straight through to the render."""
    async with _pipeline_lock:
        row = db.get_draft(d)
        if not row:
            raise PipelineError("No script to build — draft this day first")
        draft = Draft.model_validate_json(row["draft_json"])
        version = row["version"]
        await notify(f"✅ Script approved — generating the voiceover + render for "
                     f"Day {settings.day_number(d)}/30…")
        result = await _render_all(d, draft, version)
        return draft, version, result


async def revise_script(d: date, feedback: str) -> tuple[Draft, int, list[str]]:
    """Script-review revision: re-run the drafting brain with feedback and
    re-show the script — NO voiceover, NO render. Used while a day is in
    `script_review`. Because it goes through `stage_draft`, the revised EDL
    inherits the same chronological sort + clip-mapping validation as the
    initial draft, so the script stays coherent with the footage. Returns
    (draft, version, changed_fields)."""
    async with _pipeline_lock:
        old_row = db.get_draft(d)
        if not old_row:
            raise PipelineError("No draft exists yet for this day")
        old = json.loads(old_row["draft_json"])
        await notify("🤖 Revising the script with your feedback…")
        draft, version = await stage_draft(d, feedback=feedback)
        changed = [
            name for name, old_v, new_v in (
                ("script", old.get("voiceover_script"), draft.voiceover_script),
                ("script_es", old.get("voiceover_script_es"), draft.voiceover_script_es),
                ("edl", old.get("edl"), [e.model_dump() for e in draft.edl]),
                ("caption_x", old.get("caption_x"), draft.caption_x),
                ("caption_linkedin", old.get("caption_linkedin"), draft.caption_linkedin),
                ("caption_x_es", old.get("caption_x_es"), draft.caption_x_es),
                ("caption_linkedin_es", old.get("caption_linkedin_es"), draft.caption_linkedin_es),
            ) if old_v != new_v
        ]
        db.set_status(d, "script_review")  # stage_draft set it to 'drafted'
        return draft, version, changed


async def apply_feedback(d: date, feedback: str) -> tuple[Draft, int, render_svc.RenderResult, list[str]]:
    """Revision: one reasoning call, then regenerate only what changed."""
    async with _pipeline_lock:
        old_row = db.get_draft(d)
        if not old_row:
            raise PipelineError("No draft exists yet for this day")
        old = json.loads(old_row["draft_json"])
        old_version = old_row["version"]

        await notify("🤖 Agent started — applying your feedback…")
        draft, version = await stage_draft(d, feedback=feedback)
        changed = [
            name for name, old_v, new_v in (
                ("script", old.get("voiceover_script"), draft.voiceover_script),
                ("script_es", old.get("voiceover_script_es"), draft.voiceover_script_es),
                ("edl", old.get("edl"), [e.model_dump() for e in draft.edl]),
                ("caption_x", old.get("caption_x"), draft.caption_x),
                ("caption_linkedin", old.get("caption_linkedin"), draft.caption_linkedin),
                ("caption_x_es", old.get("caption_x_es"), draft.caption_x_es),
                ("caption_linkedin_es", old.get("caption_linkedin_es"), draft.caption_linkedin_es),
            ) if old_v != new_v
        ]

        langs = ["en"] + (["es"] if settings.bilingual and draft.has_spanish else [])
        result = None
        for lang in langs:
            script_changed = ("script" if lang == "en" else "script_es") in changed
            needs_render = script_changed or "edl" in changed
            if not script_changed:
                _copy_vo_artifacts(d, old_version, version, lang)
            if needs_render:
                if script_changed:
                    r = await _voiceover_and_render(d, draft, version, lang)
                else:
                    dirs = day_dirs(d)
                    vo_rel = f"work/voiceover/vo_v{version}_{lang}.mp3"
                    vo_dur = _mp3_duration(dirs["base"] / vo_rel)
                    r = await stage_render(d, draft, version, vo_rel, vo_dur, lang=lang)
            else:  # keep the previous render for this language
                prefix = "" if lang == "en" else "es_"
                paths = {f"{prefix}video_path": old_row[f"{prefix}video_path"],
                         f"{prefix}preview_path": old_row[f"{prefix}preview_path"]}
                db.update_draft_paths(d, version, **{k: v for k, v in paths.items() if v})
                r = render_svc.RenderResult(
                    video=Path(old_row[f"{prefix}video_path"] or ""),
                    preview=Path(old_row[f"{prefix}preview_path"]
                                 or old_row[f"{prefix}video_path"] or ""),
                    duration_s=0.0)
            if lang == "en":
                result = r
        db.set_status(d, "awaiting_approval")
        return draft, version, result, changed


def _copy_vo_artifacts(d: date, old_version: int, version: int, lang: str) -> None:
    """Carry an unchanged voiceover (mp3/subs/alignment) into the new version."""
    dirs = day_dirs(d)
    pairs = [
        (dirs["voiceover"] / f"vo_v{old_version}_{lang}.mp3",
         dirs["voiceover"] / f"vo_v{version}_{lang}.mp3"),
        (dirs["voiceover"] / f"alignment_v{old_version}_{lang}.json",
         dirs["voiceover"] / f"alignment_v{version}_{lang}.json"),
        (dirs["voiceover"] / f"words_v{old_version}_{lang}.json",
         dirs["voiceover"] / f"words_v{version}_{lang}.json"),
        (dirs["subs"] / f"vo_v{old_version}_{lang}.ass",
         dirs["subs"] / f"vo_v{version}_{lang}.ass"),
    ]
    for src, dst in pairs:
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())


async def regenerate_voiceover(d: date) -> tuple[Draft, int, render_svc.RenderResult]:
    """Same scripts, fresh TTS takes (new version), re-render."""
    async with _pipeline_lock:
        row = db.get_draft(d)
        if not row:
            raise PipelineError("No draft exists yet for this day")
        draft = Draft.model_validate_json(row["draft_json"])
        version = row["version"] + 1
        db.save_draft(d, version, draft.model_dump(), trigger="revision",
                      feedback_text="(regenerate voiceover)")
        result = await _voiceover_and_render(d, draft, version, "en")
        if settings.bilingual and draft.has_spanish:
            await _voiceover_and_render(d, draft, version, "es")
        db.set_status(d, "awaiting_approval")
        return draft, version, result


def _mp3_duration(path: Path) -> float:
    try:
        return float(analyze.ffprobe(path)["format"]["duration"])
    except Exception:
        return 0.0
