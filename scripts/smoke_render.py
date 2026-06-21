"""End-to-end render smoke test with synthetic footage — no external APIs.

Exercises probe -> frames -> ASS subtitles -> segments (incl. HDR tone map,
landscape crop, silent-clip anullsrc) -> concat -> VO mix -> validation.

Run inside the dev image:
  docker run --rm -v "$PWD":/srv/app -e DATA_DIR=/tmp/vj video-journal-dev \
      python scripts/smoke_render.py
"""
import asyncio
import subprocess
import sys

from app.config import settings
from app.schemas import Draft, HookCandidate
from app.services import analyze
from app.services.render import render
from app.services.subtitles import write_ass

DAY = settings.trip_start_date  # day 1


def sh(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


def make_clips(raw) -> None:
    # landscape with audio (tests center-crop + ambient duck)
    sh("ffmpeg", "-y", "-v", "error",
       "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=8",
       "-f", "lavfi", "-i", "sine=frequency=600:duration=8",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       str(raw / "IMG_0001.MOV"))
    # portrait, no audio (tests anullsrc path)
    sh("ffmpeg", "-y", "-v", "error",
       "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=8",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
       str(raw / "IMG_0002.MOV"))
    # HLG-tagged (tests the HDR tone-mapping filter chain end to end)
    sh("ffmpeg", "-y", "-v", "error",
       "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=8",
       "-f", "lavfi", "-i", "sine=frequency=300:duration=8",
       "-vf", "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       str(raw / "IMG_0003.MOV"))
    # photo, delivered as HEIC like an iPhone would (tests conversion + still beat)
    sh("ffmpeg", "-y", "-v", "error",
       "-f", "lavfi", "-i", "testsrc2=size=3024x4032:rate=1:duration=1",
       "-frames:v", "1", str(raw / "tmp_photo.png"))
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image
    with Image.open(raw / "tmp_photo.png") as im:
        im.save(raw / "IMG_0004.heic", format="HEIF")
    (raw / "tmp_photo.png").unlink()


async def main() -> None:
    day_dir = settings.day_dir(DAY)
    raw = day_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    make_clips(raw)

    jpg = analyze.convert_heic(raw / "IMG_0004.heic")
    assert jpg.name == "IMG_0004.jpg" and jpg.stat().st_size > 0

    metas = {}
    for clip in sorted(p for p in raw.iterdir() if p.suffix != ".heic"):
        if clip.suffix == ".jpg":
            meta = analyze.probe_photo(clip)
            await analyze.extract_photo_frame(clip, day_dir / "work" / "frames" / clip.stem)
        else:
            meta = analyze.probe_clip(clip)
            await analyze.extract_frames(clip, day_dir / "work" / "frames" / clip.stem)
        metas[meta.filename] = meta
        n_frames = len(list((day_dir / "work" / "frames" / clip.stem).glob("*.jpg")))
        print(f"probed {meta.filename}: {meta.duration_s:.1f}s hdr={meta.hdr} "
              f"audio={meta.has_audio} photo={meta.is_photo} frames={n_frames}")
    assert metas["IMG_0003.MOV"].hdr, "HDR flag not detected on HLG clip"
    assert not metas["IMG_0002.MOV"].has_audio
    assert metas["IMG_0004.jpg"].is_photo and metas["IMG_0004.jpg"].width == 3024

    vo = day_dir / "work" / "voiceover" / "vo_v1.mp3"
    vo.parent.mkdir(parents=True, exist_ok=True)
    sh("ffmpeg", "-y", "-v", "error", "-f", "lavfi",
       "-i", "sine=frequency=220:duration=11", "-b:a", "96k", str(vo))

    script_words = ("Day one in San Francisco. Today I want to see if the "
                    "founder energy here is actually real or not.").split()
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.42}
             for i, w in enumerate(script_words)]
    write_ass(words, day_dir / "work" / "subs" / "vo_v1.ass")

    draft = Draft(
        theme="smoke", story_summary="synthetic end-to-end render test",
        edl=[{"clip": "IMG_0001.MOV", "start_s": 1, "narration": "Day one in San Francisco today.", "why": "landscape"},
             {"clip": "IMG_0002.MOV", "start_s": 0, "narration": "I want to see if the founder energy here is real.", "why": "silent portrait"},
             {"clip": "IMG_0004.jpg", "start_s": 0, "narration": "This still photo holds the screen while narrated.", "why": "photo beat"},
             {"clip": "IMG_0003.MOV", "start_s": 2, "narration": "This last beat outruns its clip on purpose to test the freeze.", "why": "hdr + freeze"}],
        caption_x="Day 1/30 in San Francisco.", caption_linkedin="Day 1/30.",
    )
    # beats: last one (7s from start_s=2 of an 8s clip = 6s available) tests
    # the per-segment last-frame freeze
    preview = "--preview" in sys.argv
    import time
    t0 = time.monotonic()
    result = await render(day_dir, draft, metas,
                          vo_rel="work/voiceover/vo_v1.mp3",
                          ass_rel="work/subs/vo_v1.ass",
                          version=1, beat_durations=[3.0, 4.0, 3.0, 7.0],
                          vo_duration_s=11.0, preview=preview)
    elapsed = time.monotonic() - t0
    size_mb = result.video.stat().st_size / 1e6
    info = analyze.ffprobe(result.video)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    if preview:
        pw, ph = (int(x) for x in settings.preview_scale.split(":"))
        assert (v["width"], v["height"]) == (pw, ph), f"bad dims {v['width']}x{v['height']}"
        assert result.video.name.endswith("_fast.mp4"), result.video.name
    else:
        assert (v["width"], v["height"]) == (1080, 1920), f"bad dims {v['width']}x{v['height']}"
    print(f"\nOK: {result.video} — {result.duration_s:.2f}s, {size_mb:.1f}MB, "
          f"{v['width']}x{v['height']}, rendered in {elapsed:.1f}s, "
          f"preview={'same file' if result.preview == result.video else result.preview}")

    if not preview:
        # Cold-open hook: a 1.5s clip prepended via copy-concat. Uses the silent
        # portrait clip to exercise the anullsrc branch + audio-param matching
        # against the body's voiceover track.
        hook = HookCandidate(clip="IMG_0002.MOV", start_s=2.0, duration_s=1.5,
                             reason="smoke hook")
        hooked = await render(day_dir, draft, metas,
                              vo_rel="work/voiceover/vo_v1.mp3",
                              ass_rel="work/subs/vo_v1.ass",
                              version=2, beat_durations=[3.0, 4.0, 3.0, 7.0],
                              vo_duration_s=11.0, hook=hook)
        hinfo = analyze.ffprobe(hooked.video)
        assert hooked.video.name.endswith("_hk.mp4"), hooked.video.name
        assert any(s["codec_type"] == "audio" for s in hinfo["streams"]), "hooked video lost audio"
        assert hooked.duration_s > result.duration_s + 1.0, \
            f"hook did not extend video: {hooked.duration_s:.2f} vs body {result.duration_s:.2f}"
        print(f"OK hook: {hooked.video.name} — {hooked.duration_s:.2f}s "
              f"(body {result.duration_s:.2f}s + ~1.5s cold-open)")


if __name__ == "__main__":
    asyncio.run(main())
