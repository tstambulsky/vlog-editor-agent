"""ElevenLabs TTS with character-level timestamps for subtitle alignment."""
import base64
import json
from pathlib import Path

import httpx

from app.config import settings

API = "https://api.elevenlabs.io/v1"


class VoiceoverError(RuntimeError):
    pass


def default_voice_settings() -> dict:
    return {
        "stability": settings.voice_stability,
        "similarity_boost": settings.voice_similarity,
        "style": settings.voice_style,
        "speed": settings.voice_speed,
    }


async def generate(script: str, mp3_path: Path, alignment_path: Path,
                   voice_id: str | None = None,
                   voice_settings: dict | None = None) -> None:
    voice = voice_id or settings.elevenlabs_voice_id
    url = (f"{API}/text-to-speech/{voice}/with-timestamps"
           f"?output_format=mp3_44100_128")
    body = {
        "text": script,
        "model_id": settings.elevenlabs_model_id,
        "voice_settings": voice_settings or default_voice_settings(),
    }
    async with httpx.AsyncClient(timeout=120) as client:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = await client.post(
                    url, json=body,
                    headers={"xi-api-key": settings.elevenlabs_api_key},
                )
                r.raise_for_status()
                break
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise VoiceoverError(
                        f"ElevenLabs rejected the request: {exc.response.text[:300]}"
                    ) from exc
        else:
            raise VoiceoverError(f"ElevenLabs unavailable after retries: {last_exc}")

    data = r.json()
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    mp3_path.write_bytes(base64.b64decode(data["audio_base64"]))
    # raw `alignment` mirrors the input text (needed to map narration beats to
    # times); `normalized_alignment` reflects what was actually spoken (better
    # for subtitles)
    alignment_path.write_text(json.dumps({
        "alignment": data.get("alignment") or {},
        "normalized_alignment": data.get("normalized_alignment") or {},
    }))


def beat_durations(raw_alignment: dict, beats: list[str],
                   total_duration: float) -> list[float]:
    """How long each narration beat takes in the TTS audio.

    raw_alignment must be the non-normalized alignment (characters match the
    input text). Matching counts non-space characters, so whitespace/joining
    differences can't break it. Falls back to word-count proportions.
    """
    chars = raw_alignment.get("characters") or []
    ends = raw_alignment.get("character_end_times_seconds") or []
    seq = [e for c, e in zip(chars, ends) if not c.isspace()]
    if seq:
        boundaries = []
        idx = 0
        for beat in beats:
            idx = min(idx + sum(1 for c in beat if not c.isspace()), len(seq))
            boundaries.append(seq[idx - 1] if idx else 0.0)
        durations = [boundaries[0]] + [b - a for a, b in zip(boundaries, boundaries[1:])]
    else:  # fallback: split the audio proportionally to word counts
        words = [max(1, len(b.split())) for b in beats]
        total_words = sum(words)
        durations = [total_duration * w / total_words for w in words]
    return [max(1.0, d) for d in durations]


def _norm_token(tok: str) -> str:
    return "".join(ch for ch in tok.lower() if ch.isalnum())


def _script_to_whisper_map(script_toks: list[str], whisper_toks: list[str]) -> list[int]:
    """For each script token index, the aligned whisper token index.

    Uses difflib sequence alignment over normalized tokens, so it survives
    substitutions ("thirty"->"30"), drops, and merges that defeat naive
    counting/first-word matching (common words like "the" anchor wrongly).
    """
    from difflib import SequenceMatcher
    s = [_norm_token(t) for t in script_toks]
    w = [_norm_token(t) for t in whisper_toks]
    s2w = [0] * len(s)
    for tag, a0, a1, b0, b1 in SequenceMatcher(None, s, w, autojunk=False).get_opcodes():
        for k in range(a0, a1):
            if tag == "equal":
                s2w[k] = b0 + (k - a0)
            elif tag == "replace":  # spread script span across whisper span
                span = max(1, a1 - a0)
                s2w[k] = min(b1 - 1, b0 + (k - a0) * (b1 - b0) // span) if b1 > b0 else b0
            else:  # delete: script tokens with no whisper match -> anchor at b0
                s2w[k] = min(b0, len(w) - 1)
    return s2w


def beat_durations_from_words(words: list[dict], beats: list[str],
                              total_duration: float) -> list[float]:
    """Beat timing from word timestamps measured on the ACTUAL audio
    (faster-whisper), immune to ElevenLabs alignment drift.

    Each beat's image switches exactly when its narration's first word starts,
    so inter-beat pauses stay on the outgoing shot. Beat->audio mapping uses
    difflib alignment between the script and the whisper transcript.
    """
    counts = [max(1, len(b.split())) for b in beats]
    if not words:  # fallback: split proportionally to word counts
        total = sum(counts)
        return [max(1.0, total_duration * c / total) for c in counts]

    script_toks = [t for b in beats for t in b.split()]
    s2w = _script_to_whisper_map(script_toks, [w["word"] for w in words])

    starts, cum = [0.0], 0
    for c in counts[:-1]:
        cum += c
        w_idx = s2w[cum] if cum < len(s2w) else len(words) - 1
        starts.append(max(words[w_idx]["start"], starts[-1]))

    end = max(total_duration, words[-1]["end"])
    bounds = starts[1:] + [end]
    return [max(0.5, b - a) for a, b in zip(starts, bounds)]


def words_from_alignment(alignment: dict) -> list[dict]:
    """Collapse ElevenLabs character alignment into word timings."""
    chars = alignment.get("characters", [])
    starts = alignment.get("character_start_times_seconds", [])
    ends = alignment.get("character_end_times_seconds", [])
    words: list[dict] = []
    current = ""
    w_start: float | None = None
    w_end = 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if current:
                words.append({"word": current, "start": w_start, "end": w_end})
                current, w_start = "", None
            continue
        if w_start is None:
            w_start = s
        current += ch
        w_end = e
    if current:
        words.append({"word": current, "start": w_start, "end": w_end})
    return words
