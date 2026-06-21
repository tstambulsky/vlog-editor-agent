"""Deterministic ffmpeg renderer: narration-synced beats -> concat -> VO audio.

Clip audio is muted by design: segments are encoded video-only and the
voiceover is the final video's single audio track.

Each EDL entry is a beat: its clip stays on screen for exactly as long as its
narration takes to speak (beat_durations, derived from the TTS alignment), so
what you hear always matches what you see. Cut timings differ per language,
so segments are encoded per language.

All commands run with cwd = the day directory and relative paths (keeps the
ass= filter free of path-escaping issues). ffmpeg is niced so nightly encodes
don't disturb whatever else runs on the server.
"""
import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import settings
from app.schemas import Draft, HookCandidate
from app.services.analyze import ClipMeta, ffprobe

# HDR (Dolby Vision / HLG) -> SDR bt709 tone mapping, then vertical crop
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
VERTICAL = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1"

TELEGRAM_LIMIT_BYTES = 49 * 1024 * 1024


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    video: Path          # publish-quality file
    preview: Path        # file to send via Telegram (same as video if small enough)
    duration_s: float


async def _ffmpeg(args: list[str], cwd: Path, timeout: int = 1800) -> None:
    proc = await asyncio.create_subprocess_exec(
        "nice", "-n", "15", "ffmpeg", "-y", "-v", "error", *args,
        cwd=cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg failed: {stderr.decode(errors='replace')[:800]}")


def _vertical(scale: str) -> str:
    w, h = scale.split(":")
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps=30,setsar=1")


async def _make_segment(day_dir: Path, idx: int, entry, meta: ClipMeta,
                        seg_dir: Path, duration: float, lang: str,
                        scale: str = "1080:1920", preset: str = "veryfast") -> str:
    """Encode one beat: `duration` seconds on screen, freezing the last frame
    if the clip runs out of footage. `scale`/`preset` let a fast preview render
    low-res with ultrafast (see scripts/preview.py)."""
    out_rel = f"work/{seg_dir.name}/seg_{lang}_{idx:02d}.mp4"
    if (day_dir / out_rel).exists():  # resume-friendly
        return out_rel

    w, h = scale.split(":")
    # segments are video-only: the voiceover is the final video's only audio
    if meta.is_photo:  # still image, slow Ken Burns zoom so it doesn't freeze
        frames = max(1, round(duration * 30))
        # supersample 2x the target, zoom 1.0 -> 1.12 over the beat, centered
        kenburns = (f"scale={int(w)*2}:{int(h)*2}:force_original_aspect_ratio=increase,"
                    f"crop={int(w)*2}:{int(h)*2},"
                    f"zoompan=z='min(1.12,1+0.12*on/{frames})':"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={w}x{h}:fps=30,setsar=1")
        await _ffmpeg([
            "-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}",
            "-i", f"raw/{entry.clip}",
            "-vf", kenburns, "-t", f"{duration:.3f}", "-an",
            "-c:v", "libx264", "-crf", "20", "-preset", preset, "-pix_fmt", "yuv420p",
            out_rel,
        ], cwd=day_dir)
        return out_rel

    available = max(0.5, meta.duration_s - entry.start_s)
    in_dur = min(available, duration)
    freeze = max(0.0, duration - in_dur)

    vf = (TONEMAP + "," if meta.hdr else "") + _vertical(scale)
    if freeze > 0:
        vf += f",tpad=stop_mode=clone:stop_duration={freeze + 0.2:.2f}"

    await _ffmpeg([
        "-ss", f"{entry.start_s:.3f}", "-t", f"{in_dur:.3f}",
        "-i", f"raw/{entry.clip}",
        "-map", "0:v:0", "-vf", vf, "-t", f"{duration:.3f}", "-an",
        "-c:v", "libx264", "-crf", "20", "-preset", preset, "-pix_fmt", "yuv420p",
        out_rel,
    ], cwd=day_dir)
    return out_rel


async def _make_hook_segment(day_dir: Path, hook: HookCandidate, meta: ClipMeta,
                             out_rel: str, audio_stream: dict, scale: str,
                             preset: str) -> str:
    """Encode the 1-2s cold-open as a self-contained A/V clip whose codec params
    MATCH the body's final output (same scale, h264 high yuv420p 30fps, AAC at
    the body's sample-rate/channels) so it can be concat-demuxed onto the body
    with `-c copy`. Unlike a beat segment, the hook KEEPS audio — the clip's own
    natural sound, loudnorm'd to sit at the voiceover's level (silent when the
    clip has no audio track)."""
    if (day_dir / out_rel).exists():  # resume-friendly
        return out_rel
    (day_dir / out_rel).parent.mkdir(parents=True, exist_ok=True)

    available = max(0.5, meta.duration_s - hook.start_s)
    in_dur = min(available, hook.duration_s)
    freeze = max(0.0, hook.duration_s - in_dur)
    vf = (TONEMAP + "," if meta.hdr else "") + _vertical(scale)
    if freeze > 0:
        vf += f",tpad=stop_mode=clone:stop_duration={freeze + 0.2:.2f}"

    sr = int(audio_stream.get("sample_rate") or 48000)
    ch = int(audio_stream.get("channels") or 2)
    venc = ["-c:v", "libx264", "-crf", "23", "-preset", preset,
            "-profile:v", "high", "-pix_fmt", "yuv420p"]
    aenc = ["-c:a", "aac", "-b:a", "128k", "-ar", str(sr), "-ac", str(ch)]
    if meta.has_audio:
        args = [
            "-ss", f"{hook.start_s:.3f}", "-t", f"{in_dur:.3f}", "-i", f"raw/{hook.clip}",
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", vf, "-af", f"aresample={sr},loudnorm=I=-14:TP=-1.5:LRA=11,apad",
            "-t", f"{hook.duration_s:.3f}", *venc, *aenc, out_rel,
        ]
    else:  # no audio track on the clip -> a matching strip of silence
        cl = "stereo" if ch == 2 else "mono"
        args = [
            "-ss", f"{hook.start_s:.3f}", "-t", f"{in_dur:.3f}", "-i", f"raw/{hook.clip}",
            "-f", "lavfi", "-t", f"{hook.duration_s:.3f}", "-i", f"anullsrc=r={sr}:cl={cl}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", vf, "-t", f"{hook.duration_s:.3f}", *venc, *aenc, out_rel,
        ]
    await _ffmpeg(args, cwd=day_dir)
    return out_rel


async def hook_preview(day_dir: Path, hook: HookCandidate, meta: ClipMeta,
                       out_rel: str) -> str:
    """A standalone, low-res 1-2s preview of a candidate hook (with the clip's
    natural sound) for the founder to watch in Telegram before picking. Not the
    final segment — the build re-encodes the chosen one to match the body."""
    return await _make_hook_segment(
        day_dir, hook, meta, out_rel,
        audio_stream={"sample_rate": 48000, "channels": 2},
        scale=settings.preview_scale, preset="ultrafast")


async def _prepend_hook(day_dir: Path, hook: HookCandidate, meta: ClipMeta,
                        body_rel: str, audio_stream: dict, scale: str,
                        preset: str) -> str:
    """Render the hook segment and concat-copy [hook, body] -> a new final file.
    The body render is left untouched (so its voiceover + burned subtitles stay
    aligned to its own t=0); the hook simply rides in front. Returns the new
    final path (day-relative)."""
    stem = Path(body_rel).stem
    hook_rel = f"work/hooks/{stem}_hook.mp4"
    await _make_hook_segment(day_dir, hook, meta, hook_rel, audio_stream, scale, preset)

    join_rel = f"work/hooks/{stem}_join.txt"
    (day_dir / join_rel).parent.mkdir(parents=True, exist_ok=True)
    (day_dir / join_rel).write_text(
        f"file '{(day_dir / hook_rel).resolve()}'\n"
        f"file '{(day_dir / body_rel).resolve()}'\n")
    final_rel = f"output/{stem}_hk.mp4"
    await _ffmpeg(["-f", "concat", "-safe", "0", "-i", join_rel,
                   "-c", "copy", "-movflags", "+faststart", final_rel],
                  cwd=day_dir)
    return final_rel


async def render(day_dir: Path, draft: Draft, clip_metas: dict[str, ClipMeta],
                 vo_rel: str, ass_rel: str, version: int,
                 beat_durations: list[float], vo_duration_s: float,
                 suffix: str = "", preview: bool = False,
                 hook: HookCandidate | None = None) -> RenderResult:
    if len(beat_durations) != len(draft.edl):
        raise RenderError(f"{len(beat_durations)} beat durations for "
                          f"{len(draft.edl)} EDL entries")
    lang = suffix.lstrip("_") or "en"
    # A fast preview encodes low-res + ultrafast into its OWN segment cache and
    # output name, so it never clobbers (or gets clobbered by) the real render.
    scale = settings.preview_scale if preview else "1080:1920"
    seg_preset = settings.preview_preset if preview else "veryfast"
    final_preset = settings.preview_preset if preview else "medium"
    tag = "_fast" if preview else ""
    seg_dir = day_dir / "work" / f"segments_v{version}{tag}"
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_names = []
    for i, (entry, dur) in enumerate(zip(draft.edl, beat_durations)):
        rel = await _make_segment(day_dir, i, entry, clip_metas[entry.clip],
                                  seg_dir, dur, lang, scale=scale, preset=seg_preset)
        seg_names.append(Path(rel).name)

    concat_list = seg_dir / f"list_{lang}.txt"
    concat_list.write_text("".join(f"file '{n}'\n" for n in seg_names))
    concat_rel = f"work/{seg_dir.name}/concat_{lang}.mp4"
    await _ffmpeg(["-f", "concat", "-safe", "0",
                   "-i", f"work/{seg_dir.name}/list_{lang}.txt",
                   "-c", "copy", concat_rel], cwd=day_dir)

    out_dir = day_dir / "output"
    out_dir.mkdir(exist_ok=True)
    day_n = settings.day_number(date.fromisoformat(day_dir.name))
    out_rel = f"output/day{day_n:02d}_v{version}{suffix}{tag}.mp4"

    # Voiceover is the ONLY audio (clip sound is muted). Beats sum ≈ narration
    # length; end 0.3s after the last spoken word — no silent tail. vo_duration_s
    # is the last word's end (alignment), not the mp3 length, so trailing TTS
    # silence is cut too. Hard-cap at 60s.
    total_beats = sum(beat_durations)
    limit = min(settings.max_video_seconds, max(vo_duration_s + 0.3, 1.0))
    pad = max(0.0, limit - total_beats)
    fade_start = max(0.0, limit - 0.3)

    vchain = f"tpad=stop_mode=clone:stop_duration={pad:.2f}"
    if settings.burn_subtitles:
        vchain += f",ass={ass_rel}"
    filter_complex = (
        "[1:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11,apad,"
        f"afade=t=out:st={fade_start:.2f}:d=0.3[aout];"
        f"[0:v]{vchain}[vout]"
    )
    await _ffmpeg([
        "-i", concat_rel, "-i", vo_rel,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]", "-t", f"{limit:.2f}",
        "-c:v", "libx264", "-crf", "23", "-preset", final_preset,
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        out_rel,
    ], cwd=day_dir)

    # Cap is checked on the BODY (the beat-synced part). A hook adds ≤2.5s in
    # front and is exempt from the cap — it's a cold-open, not narrated time.
    body_info = ffprobe(day_dir / out_rel)
    duration = float(body_info["format"]["duration"])
    if duration > settings.max_video_seconds + 1:
        raise RenderError(f"Final video is {duration:.1f}s (> {settings.max_video_seconds}s)")
    astreams = [s for s in body_info["streams"] if s["codec_type"] == "audio"]
    if not astreams:
        raise RenderError("Final video has no audio stream")

    # Prepend the chosen 1-2s hook (with the clip's natural sound) in front of
    # the untouched body via a copy-concat, matching the body's audio params.
    final_rel = out_rel
    if hook and hook.clip in clip_metas:
        final_rel = await _prepend_hook(day_dir, hook, clip_metas[hook.clip],
                                        out_rel, astreams[0], scale, final_preset)
        duration = float(ffprobe(day_dir / final_rel)["format"]["duration"])
    out_path = day_dir / final_rel

    preview_path = out_path
    if out_path.stat().st_size > TELEGRAM_LIMIT_BYTES:
        preview_rel = final_rel.replace(".mp4", "_preview.mp4")
        await _ffmpeg(["-i", final_rel, "-c:v", "libx264", "-crf", "28",
                       "-preset", "veryfast", "-vf", "scale=720:1280",
                       "-c:a", "copy", "-movflags", "+faststart", preview_rel],
                      cwd=day_dir)
        preview_path = day_dir / preview_rel

    return RenderResult(video=out_path, preview=preview_path, duration_s=duration)
