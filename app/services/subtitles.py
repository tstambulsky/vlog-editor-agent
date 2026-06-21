"""Word timings -> burned-in ASS subtitles (2-4 word groups, bottom-center)."""
from pathlib import Path

MAX_WORDS_PER_LINE = 3
MAX_LINE_SECONDS = 2.5
SENTENCE_END = ".!?"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,1,2,80,80,280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def group_words(words: list[dict]) -> list[dict]:
    """Group word timings into short subtitle lines."""
    lines: list[dict] = []
    current: list[dict] = []
    for w in words:
        if not w.get("word"):
            continue
        current.append(w)
        too_long = (w["end"] - current[0]["start"]) >= MAX_LINE_SECONDS
        ends_sentence = w["word"][-1] in SENTENCE_END or w["word"][-1] == ","
        if len(current) >= MAX_WORDS_PER_LINE or too_long or ends_sentence:
            lines.append(_flush(current))
            current = []
    if current:
        lines.append(_flush(current))
    # avoid overlaps: each line ends no later than the next one starts
    for a, b in zip(lines, lines[1:]):
        a["end"] = min(a["end"], b["start"])
    return lines


def _flush(group: list[dict]) -> dict:
    return {
        "text": " ".join(w["word"] for w in group),
        "start": group[0]["start"],
        "end": group[-1]["end"],
    }


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(words: list[dict], out_path: Path) -> Path:
    events = []
    for line in group_words(words):
        text = line["text"].replace("\n", " ").replace("{", "(").replace("}", ")")
        events.append(
            f"Dialogue: 0,{_ts(line['start'])},{_ts(line['end'])},Default,,0,0,0,,{text}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(ASS_HEADER + "\n".join(events) + "\n")
    return out_path
