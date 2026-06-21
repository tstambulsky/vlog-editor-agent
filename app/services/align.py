"""Tight subtitle/beat word timing via WhisperX (transcribe -> forced align).

Two stages: faster-whisper transcribes the VO mp3 into sentence-level segments
(reliable boundaries, drifty per-word times), then a wav2vec2 phoneme model
force-aligns the words inside EACH segment's own audio slice. That per-segment
slicing is the whole point — it keeps timing tight throughout, including the
dense opening. (Aligning the whole clip as one [0,dur] segment, the first
version of this, made CTC misplace the early words so captions ran ahead of the
voice.)

The transcription supplies only TIMING; its TEXT is mis-heard ASR output
("Jet lag" -> "JITLAG", "founders" -> "Members"). Since the TTS reads our exact
script, `retext_to_script` re-maps the timed words back onto the KNOWN script
tokens (difflib), emitting the script verbatim with the transferred timings — so
on-screen captions can NEVER diverge from the script. Output shape matches
`analyze.transcribe_words` so the rest of the pipeline is unchanged;
`pipeline.stage_voiceover` falls back to plain whisper if this raises (e.g.
torch absent in the dev image) and re-applies `retext_to_script` there too, so
even the fallback shows script text.

RAM-sensitive: faster-whisper (~0.5-1GB) plus the wav2vec2 align model (~360MB)
on a 3.7GB server. We hold one align model at a time and free it the moment a
language is done (EN/ES render sequentially).
"""
import asyncio
import gc
import re

from pathlib import Path

from app.config import settings

_align_lock = asyncio.Lock()  # wav2vec2 is CPU/RAM-heavy; one at a time


def _interpolate_missing(words: list[dict]) -> list[dict]:
    """Fill start/end for words wav2vec2 couldn't time (digits, symbols).

    Carries the previous word's end forward / the next word's start backward so
    downstream beat math and subtitle grouping never see a None timestamp.
    """
    n = len(words)
    for i, w in enumerate(words):
        if w.get("start") is None:
            prev_end = next((words[j]["end"] for j in range(i - 1, -1, -1)
                             if words[j].get("end") is not None), None)
            nxt_start = next((words[j]["start"] for j in range(i + 1, n)
                              if words[j].get("start") is not None), None)
            w["start"] = prev_end if prev_end is not None else (nxt_start or 0.0)
        if w.get("end") is None:
            nxt_start = next((words[j]["start"] for j in range(i + 1, n)
                              if words[j].get("start") is not None), None)
            w["end"] = nxt_start if nxt_start is not None else w["start"]
    return words


def _norm_match(tok: str) -> str:
    """Lowercased alphanumerics only, for matching (not for display).

    Strips punctuation and casing so "Founders" matches "founders" and
    "café." matches "cafe". Contractions/hyphens collapse ("Ravikant's" ->
    "ravikants", "jet-lag" -> "jetlag"); that's fine — we only need the
    SequenceMatcher to find the right alignment, the emitted text is the
    untouched script token.
    """
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def retext_to_script(timed: list[dict], script: str) -> list[dict]:
    """Re-map ASR-timed words onto the KNOWN script, emitting script text.

    `timed` carries correct TIMING but mis-heard TEXT (it's an ASR transcript
    of the VO). We have the exact script the TTS read, so this returns one entry
    per SCRIPT token — the script token verbatim (casing + punctuation) — each
    carrying a start/end transferred from the matched aligned word. difflib over
    normalized tokens survives substitutions ("Jet lag" heard as "JITLAG"),
    drops, and merges. Script tokens with no match (insertions) get their timing
    interpolated from neighbours. Timings are clamped monotonic non-decreasing.

    If `script` is empty (shouldn't happen in the pipeline) the timed words are
    returned unchanged so we never emit nothing.
    """
    from difflib import SequenceMatcher

    script_toks = script.split()
    if not script_toks:
        return timed
    if not timed:  # no timing at all: lay the script out with null timings,
        return _interpolate_missing(  # interpolation pins everything to 0.0
            [{"word": t, "start": None, "end": None} for t in script_toks])

    s = [_norm_match(t) for t in script_toks]
    w = [_norm_match(t["word"]) for t in timed]
    out: list[dict] = [{"word": t, "start": None, "end": None}
                       for t in script_toks]
    for tag, a0, a1, b0, b1 in SequenceMatcher(
            None, s, w, autojunk=False).get_opcodes():
        if tag in ("equal", "replace"):
            span = max(1, a1 - a0)
            for k in range(a0, a1):
                # map each script token in the span to a proportional timed word
                j = min(b1 - 1, b0 + (k - a0) * (b1 - b0) // span) if b1 > b0 else b0
                if b0 <= j < b1:
                    out[k]["start"] = timed[j]["start"]
                    out[k]["end"] = timed[j]["end"]
        # 'delete' (script tokens with no timed match) -> leave None, interpolate
        # 'insert' (extra timed words) -> nothing to emit, the script wins
    out = _interpolate_missing(out)
    # clamp monotonic so subtitle grouping / beat math never see times go back
    prev = 0.0
    for entry in out:
        entry["start"] = max(float(entry["start"]), prev)
        entry["end"] = max(float(entry["end"]), entry["start"])
        prev = entry["start"]
    return out


def _asr_segments(mp3: str, lang: str, duration: float) -> list[dict]:
    """Sentence-level anchor segments (rough start/end + text) from faster-whisper.

    WhisperX aligns each segment within ITS OWN audio slice, so good per-segment
    boundaries are what keep word timing tight. A single [0,dur] segment makes
    CTC alignment misplace the early words (they get compressed earlier than
    spoken — the "captions run ahead of the voice" bug). faster-whisper's
    segment boundaries are reliable even though its per-word times drift, and
    the wav2vec2 pass refines the words inside each slice.
    """
    from app.services import analyze
    segs, _ = analyze._get_whisper().transcribe(mp3, language=lang,
                                                word_timestamps=False)
    out = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
           for s in segs if (s.text or "").strip()]
    # Free faster-whisper before the wav2vec2 align + ffmpeg render so peak RAM
    # stays at the wav2vec2-only level (~2.5GB) — both models resident at once
    # OOM-kills the 3.7GB box. It lazily reloads if needed again (e.g. ES).
    analyze._whisper_model = None
    gc.collect()
    if not out:
        out = [{"start": 0.0, "end": duration, "text": ""}]
    return out


def _align_sync(script: str, mp3: str, lang: str, duration: float) -> list[dict]:
    import whisperx  # lazy: torch only present in the runtime image

    segments = _asr_segments(mp3, lang, duration)
    model, metadata = whisperx.load_align_model(
        language_code=lang, device="cpu",
        model_name=settings.align_model_es if lang == "es" else None,
    )
    try:
        audio = whisperx.load_audio(mp3)
        result = whisperx.align(segments, model, metadata, audio, "cpu",
                                return_char_alignments=False)
        raw = result.get("word_segments") or [
            w for s in result.get("segments", []) for w in s.get("words", [])
        ]
        words = [{"word": (w.get("word") or "").strip(),
                  "start": w.get("start"), "end": w.get("end")}
                 for w in raw if (w.get("word") or "").strip()]
        words = _interpolate_missing(words)
        # wav2vec2 returns numpy floats; cast to native float so the timings
        # survive json.dumps downstream (np.float64 is not JSON-serializable).
        for w in words:
            w["start"], w["end"] = float(w["start"]), float(w["end"])
        # The transcription gave us TIMING; its TEXT is mis-heard ASR. Re-map
        # onto the known script so captions show exactly what the TTS read.
        return retext_to_script(words, script)
    finally:
        del model, metadata  # free ~0.4-1.2GB before the ffmpeg encode runs
        gc.collect()


async def align_script(script: str, mp3: Path, lang: str,
                       duration: float) -> list[dict]:
    """Tight word timings for the speech in `mp3` (transcribe -> wav2vec2 align).
    The transcription supplies timing only; `retext_to_script` then re-maps the
    timed words onto `script`, so the returned word TEXT is the known script
    verbatim. Raises on any failure so the caller can fall back to plain
    whisper (which is itself re-texted to the script in `stage_voiceover`)."""
    if settings.subtitle_aligner != "whisperx":
        raise RuntimeError("whisperx aligner disabled (subtitle_aligner)")
    async with _align_lock:
        return await asyncio.to_thread(_align_sync, script, str(mp3), lang,
                                       duration)
