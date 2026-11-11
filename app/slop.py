"""Mechanical checks on the copy that don't need judgment (the LLM reviewer does
the judging). Each finding is a short instruction the rewrite pass can act on.
These look at FORM only (punctuation, case, emoji), never at intent."""
import re

_DASHES = ("—", "–")  # em dash, en dash
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF]")
# a word with an uppercase letter that is NOT an acronym (AI, MRR, API) and not a handle
_UPPER_WORD = re.compile(r"(?<![@\w])(?=\w*[a-z])\w*[A-Z]\w*")
_LONE_I = re.compile(r"(?<![@\w])I(?=$|[\s'’.,!])", re.M)


def lint_narration(text: str) -> list[str]:
    issues = []
    if any(ch in text for ch in _DASHES):
        issues.append("narration: remove every em/en dash (use a comma or a period)")
    if text.rstrip().endswith("?"):
        issues.append("narration: the last sentence is a question; end on the last real moment")
    return issues


def lint_tweet(text: str, first_line: str, *, lowercase: bool = True) -> list[str]:
    issues = []
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != first_line:
        issues.append(f"tweet: the first line must be exactly `{first_line}`")
    if any(ch in text for ch in _DASHES):
        issues.append("tweet: remove every em/en dash")
    if _EMOJI.search(text):
        issues.append("tweet: remove all emoji")
    if text.rstrip().endswith("?"):
        issues.append("tweet: no closing question; end on a fact or just stop")
    upper = sorted(set(_UPPER_WORD.findall(text)) | set(_LONE_I.findall(text)))
    if lowercase and upper:
        issues.append("tweet: lowercase these words (only acronyms and @handles keep caps): "
                      + ", ".join(upper[:10]))
    if "#" in text:
        issues.append("tweet: no hashtags")
    return issues
