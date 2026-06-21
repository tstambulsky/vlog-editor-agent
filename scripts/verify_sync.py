"""Verify voice <-> caption <-> frame correlation for a rendered day.

Independent of the alignment that PRODUCED the subtitles: re-transcribes the
FINAL rendered video's audio with faster-whisper word timestamps (ground truth
of when each word is actually spoken), then checks the burned ASS caption shown
at each moment matches that audio. Also maps each caption to its active beat
(clip) and dumps a downscaled frame per caption line so caption<->frame
coherence can be eyeballed.

  docker compose run --rm --no-deps \
    -v /tmp/verify_sync.py:/srv/verify_sync.py -e PYTHONPATH=/srv/app \
    video-journal python /srv/verify_sync.py 2026-06-11 en
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from app import db
from app.config import settings
from app.pipeline import _beat_durations, day_dirs
from app.schemas import Draft
from app.services import analyze

d = sys.argv[1]
lang = sys.argv[2] if len(sys.argv) > 2 else "en"
from datetime import date
dd = date.fromisoformat(d)
dirs = day_dirs(dd)
day_n = settings.day_number(dd)
ass = dirs["subs"]


def parse_ass(p: Path):
    lines = []
    for ln in p.read_text().splitlines():
        if not ln.startswith("Dialogue:"):
            continue
        parts = ln.split(",", 9)
        s, e, text = parts[1], parts[2], parts[9]
        def secs(t):
            h, m, rest = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(rest)
        lines.append({"start": secs(s), "end": secs(e),
                      "text": re.sub(r"\{[^}]*\}", "", text).strip()})
    return lines


def norm(w):
    return re.sub(r"[^\w]", "", w.lower())


db.connect()
row = db.get_draft(dd)
draft = Draft.model_validate_json(row["draft_json"])
version = row["version"]
vid = dirs["base"] / "output" / f"day{day_n:02d}_v{version}_{lang}.mp4"
assf = ass / f"vo_v{version}_{lang}.ass"

# 1) independent ground-truth transcription of the FINAL video audio
wav = Path("/tmp/final_audio.wav")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(vid),
                "-ar", "16000", "-ac", "1", str(wav)], check=True)
import asyncio
truth = asyncio.run(analyze.transcribe_words(wav))
print(f"independent transcription: {len(truth)} words\n")

# 2) caption lines + active beat per line
caps = parse_ass(assf)
words = json.loads((dirs["voiceover"] / f"words_v{version}_{lang}.json").read_text())
vo_dur = max((w["end"] for w in words), default=0.0)
beats = _beat_durations(dd, draft, version, lang, vo_dur)
beat_bounds, acc = [], 0.0
for b in beats:
    acc += b
    beat_bounds.append(acc)

def active_beat(t):
    for i, b in enumerate(beat_bounds):
        if t < b:
            return i
    return len(beat_bounds) - 1

# 3) per-caption check: does the audio at that window say the caption words?
print(f"{'t':>6}  {'beat':>4}  {'caption':<34}  spoken@window (independent)")
print("-" * 100)
mism = 0
verify_dir = dirs["base"] / "verify"
verify_dir.mkdir(exist_ok=True)
frames = []
for i, c in enumerate(caps):
    mid = (c["start"] + c["end"]) / 2
    win = [norm(w["word"]) for w in truth
           if c["start"] - 0.2 <= (w["start"] + w["end"]) / 2 <= c["end"] + 0.2]
    capset = [norm(w) for w in c["text"].split() if norm(w)]
    hit = sum(1 for w in capset if w in win)
    ok = "OK" if capset and hit >= max(1, len(capset) - 1) else "??"
    if ok == "??":
        mism += 1
    print(f"{c['start']:6.2f}  {active_beat(mid):>4}  {c['text'][:34]:<34}  "
          f"{' '.join(win)[:40]}  {ok}")
    # frame at caption midpoint, downscaled
    fp = verify_dir / f"{lang}_{i:02d}_{c['start']:.2f}.jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{mid:.2f}",
                    "-i", str(vid), "-vframes", "1", "-vf", "scale=360:-1",
                    "-q:v", "5", str(fp)], check=True)
    frames.append(fp.name)

print(f"\ncaption/voice mismatches: {mism}/{len(caps)}")
print(f"frames in: {verify_dir}")
print("FRAMES:" + ",".join(frames))
