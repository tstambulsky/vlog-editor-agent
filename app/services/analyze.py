"""Deterministic per-clip pre-processing: ffprobe metadata, keyframes,
faster-whisper transcripts, photo probing/HEIC conversion. No LLM here."""
import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.schemas import PHOTO_EXTS

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
MAX_FRAMES_PER_CLIP = 12

_whisper_model = None
_whisper_lock = asyncio.Lock()


def normalize_ts(raw: str) -> tuple[str, float]:
    """Make every clip's capture time directly comparable in the trip timezone.

    Inputs arrive with mixed timezones: video `creation_time` from ffprobe is
    UTC (trailing 'Z'/+00:00); photo EXIF DateTimeOriginal is NAIVE *local* wall
    time (no offset). Treating them on one axis without normalizing scrambles the
    chronology (a 19:10 local photo would sort as 19:10 UTC = 12:10 local).

    Rule: aware timestamps are converted to `settings.tz`; naive ones are assumed
    to already be local (NOT shifted — only stamped with the trip tz). Returns
    (iso_local, epoch) where iso_local is tz-aware in the trip tz and epoch is a
    sortable POSIX timestamp. Unparseable input -> ("", 0.0) so it sorts first.
    """
    if not raw:
        return "", 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "", 0.0
    if dt.tzinfo is None:  # naive EXIF = already-local wall time
        dt = dt.replace(tzinfo=settings.tz)
    else:                  # aware (UTC video) -> trip-local
        dt = dt.astimezone(settings.tz)
    return dt.isoformat(), dt.timestamp()


@dataclass
class ClipMeta:
    filename: str
    duration_s: float
    created_ts: str           # raw capture time as probed (UTC video / naive photo)
    width: int
    height: int
    hdr: bool
    has_audio: bool
    is_photo: bool = False  # default keeps old clips.json files loadable
    # tz-normalized capture time (trip-local ISO) + sortable epoch. Defaulted so
    # old clips.json files (without these keys) still load via ClipMeta(**v).
    created_local: str = ""
    created_epoch: float = 0.0

    def __post_init__(self):
        # Backfill the normalized fields from the raw ts whenever they're absent
        # (fresh probe or an old clips.json that predates them).
        if not self.created_local and not self.created_epoch and self.created_ts:
            self.created_local, self.created_epoch = normalize_ts(self.created_ts)


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}: {out.stderr[:300]}")
    return json.loads(out.stdout)


def probe_clip(path: Path) -> ClipMeta:
    info = ffprobe(path)
    vstreams = [s for s in info["streams"] if s["codec_type"] == "video"]
    if not vstreams:
        raise RuntimeError(f"{path.name} has no video stream")
    v = vstreams[0]
    fmt = info.get("format", {})
    return ClipMeta(
        filename=path.name,
        duration_s=float(fmt.get("duration") or v.get("duration") or 0),
        created_ts=fmt.get("tags", {}).get("creation_time", ""),
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        hdr=v.get("color_transfer", "") in HDR_TRANSFERS,
        has_audio=any(s["codec_type"] == "audio" for s in info["streams"]),
    )


def probe_photo(path: Path) -> ClipMeta:
    from PIL import Image
    with Image.open(path) as im:
        width, height = im.size
        exif = im.getexif()
        # DateTimeOriginal lives in the EXIF IFD; DateTime at the top level
        raw_dt = exif.get_ifd(0x8769).get(36867) or exif.get(306)
    if raw_dt:
        created = raw_dt.replace(":", "-", 2).replace(" ", "T")
    else:
        created = datetime.fromtimestamp(path.stat().st_mtime,
                                         tz=timezone.utc).isoformat()
    return ClipMeta(filename=path.name, duration_s=0.0, created_ts=created,
                    width=width, height=height, hdr=False, has_audio=False,
                    is_photo=True)


def convert_heic(path: Path) -> Path:
    """HEIC/HEIF -> JPEG next to the original (ffmpeg can't decode HEIC)."""
    out = path.with_suffix(".jpg")
    if out.exists():
        return out
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        exif = im.info.get("exif")
        kwargs = {"exif": exif} if exif else {}
        im.convert("RGB").save(out, "JPEG", quality=92, **kwargs)
    return out


async def extract_photo_frame(photo: Path, out_dir: Path) -> None:
    """A photo's 'keyframe' for Claude is just the photo itself, downscaled."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.glob("*.jpg")):
        return  # idempotent
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-i", str(photo),
        "-vf", "scale=640:-2", "-frames:v", "1", "-q:v", "5",
        str(out_dir / "f_001.jpg"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"photo frame failed for {photo.name}: {stderr.decode()[:300]}")


async def extract_frames(clip: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.glob("*.jpg")):
        return  # idempotent
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-i", str(clip),
        "-vf", "fps=1/2,scale=640:-2",
        "-frames:v", str(MAX_FRAMES_PER_CLIP), "-q:v", "5",
        str(out_dir / "f_%03d.jpg"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"frame extraction failed for {clip.name}: {stderr.decode()[:300]}")


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(settings.whisper_model, device="cpu",
                                      compute_type="int8", cpu_threads=4)
    return _whisper_model


def _transcribe_sync(path: Path) -> dict:
    model = _get_whisper()
    segments, info = model.transcribe(str(path), vad_filter=True)
    return {
        "language": info.language,
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
    }


async def transcribe(path: Path, out_path: Path) -> Path:
    if out_path.exists():
        return out_path  # idempotent
    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with _whisper_lock:  # whisper is CPU-heavy; one at a time
        result = await asyncio.to_thread(_transcribe_sync, path)
    # Atomic write: an interrupted run must never leave a half-written json the
    # batch build (or the cache) would later fail to parse — write then rename.
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    tmp.replace(out_path)
    return out_path


async def process_clip(clip: Path, frames_root: Path,
                       transcripts_dir: Path) -> ClipMeta:
    """Idempotent per-clip mechanical work: probe -> keyframes -> transcript.

    Safe to call repeatedly (frame/transcript primitives skip-if-exists; ffprobe
    is cheap). Raises RuntimeError if the clip can't be probed so the caller can
    quarantine it. Does NOT touch the DB or the LLM — the pipeline orchestrator
    owns the DB upsert and the per-clip vision summary."""
    photo = clip.suffix.lower() in PHOTO_EXTS
    meta = await asyncio.to_thread(probe_photo if photo else probe_clip, clip)
    frames_dir = frames_root / clip.stem
    if photo:
        await extract_photo_frame(clip, frames_dir)
    else:
        await extract_frames(clip, frames_dir)
    t_path = transcripts_dir / f"{clip.stem}.json"
    if meta.has_audio:
        await transcribe(clip, t_path)
    elif not t_path.exists():
        t_path.parent.mkdir(parents=True, exist_ok=True)
        t_path.write_text('{"language": null, "segments": []}')
    return meta


async def transcribe_words(path: Path) -> list[dict]:
    """Word-level timestamps (used on the voiceover as alignment fallback)."""
    def _run():
        model = _get_whisper()
        segments, _ = model.transcribe(str(path), word_timestamps=True)
        return [{"word": w.word.strip(), "start": w.start, "end": w.end}
                for s in segments for w in (s.words or [])]
    async with _whisper_lock:
        return await asyncio.to_thread(_run)


async def transcribe_text(path: Path) -> str:
    """Full plain-text transcription of a short audio (voice context notes).

    Unlike transcribe_words, this joins SEGMENT text, so a segment whose
    word-level alignment came back empty is NOT dropped — we keep every word
    spoken. Use this whenever only the text matters (no timing needed)."""
    def _run():
        model = _get_whisper()
        segments, _ = model.transcribe(str(path), vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
    async with _whisper_lock:
        return await asyncio.to_thread(_run)
