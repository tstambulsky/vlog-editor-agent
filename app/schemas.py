from pathlib import Path

from pydantic import BaseModel, Field, computed_field

MAX_SCRIPT_WORDS = 230  # validation ceiling; the real target is the configurable
MIN_SCRIPT_WORDS = 60   # script_words_min/max budget (+ humanizer ±15% headroom)

# pipeline-usable stills (HEIC is converted to .jpg before any of this)
PHOTO_EXTS = {".jpg", ".jpeg", ".png"}


def is_photo(filename: str) -> bool:
    return Path(filename).suffix.lower() in PHOTO_EXTS


# --- per-clip vision cache ---------------------------------------------------
# A clip is summarized ONCE (at arrival / first analyze) and the result cached to
# work/clip_analysis/<stem>.json, reused at draft + every redraft instead of
# re-reading the raw keyframes each time. Bump CLIP_ANALYSIS_VERSION to force a
# recompute of every cached summary (schema or analysis-prompt change).
CLIP_ANALYSIS_VERSION = 1


class KeyMoment(BaseModel):
    timestamp_s: float = Field(ge=0, description="seconds into the clip")
    what: str = Field(description="one line: what's visible at that moment")


class ClipAnalysisReply(BaseModel):
    """The descriptive fields the model returns. Bookkeeping fields
    (analysis_version/clip/is_photo) are merged in by code afterwards, so the
    model can never spoof the cache version."""
    summary: str = Field(min_length=1, description="1-2 sentence scene summary")
    key_moments: list[KeyMoment] = Field(default_factory=list)
    visual_details: str = ""
    usable: bool = True
    quality_notes: str = ""


class ClipAnalysis(ClipAnalysisReply):
    analysis_version: int  # == CLIP_ANALYSIS_VERSION when the cache is valid
    clip: str
    is_photo: bool = False
    transcript_excerpt: str = ""


class EdlEntry(BaseModel):
    """One beat: a clip shown for exactly as long as its narration is spoken."""
    clip: str = Field(description="filename of the source clip in raw/")
    start_s: float = Field(ge=0, description="where to start within the clip")
    narration: str = Field(min_length=10,
                           description="English sentence(s) spoken over this clip")
    narration_es: str = ""
    why: str = ""


class HookCandidate(BaseModel):
    """A proposed 1-2s cold-open shown BEFORE the narrated story to grab
    attention. NOT a beat: it carries no narration, is exempt from the
    chronological sort and beat-sync, and plays the clip's own natural sound.
    Lives on the Draft but is invisible to voiceover_script/beats()."""
    clip: str = Field(description="filename of the source clip in raw/ (a video, not a photo)")
    start_s: float = Field(ge=0, description="where the hook starts within the clip")
    duration_s: float = Field(ge=0.8, le=2.5, description="how long the cold-open plays (1-2s)")
    reason: str = Field(default="", description="why this moment grabs attention")


class Draft(BaseModel):
    theme: str
    story_summary: str
    edl: list[EdlEntry] = Field(min_length=1)
    caption_x: str = Field(max_length=600)
    caption_linkedin: str
    caption_x_es: str = Field(default="", max_length=600)
    caption_linkedin_es: str = ""
    confidence_notes: str = ""
    # Proposed cold-open hooks (the founder picks one; default = the first).
    # Default empty so drafts saved before this field still validate.
    hook_candidates: list[HookCandidate] = Field(default_factory=list, max_length=4)

    @computed_field
    @property
    def voiceover_script(self) -> str:
        return " ".join(e.narration.strip() for e in self.edl)

    @computed_field
    @property
    def voiceover_script_es(self) -> str:
        return " ".join(e.narration_es.strip() for e in self.edl
                        if e.narration_es.strip())

    @property
    def has_spanish(self) -> bool:
        return all(e.narration_es.strip() for e in self.edl)

    def beats(self, lang: str) -> list[str]:
        return [e.narration if lang == "en" else e.narration_es for e in self.edl]


class HumanizedText(BaseModel):
    """Reply of the post-draft humanizer pass: the spoken narration rewritten
    beat-by-beat (SAME count/order as the EDL so beat-sync survives) plus the two
    rewritten EN captions. Applied back onto the Draft in pipeline.stage_humanize.
    """
    narrations: list[str] = Field(
        min_length=1,
        description="rewritten narration, one string per EDL beat, same order")
    caption_x: str = Field(max_length=280)
    caption_linkedin: str


def sort_edl_chronologically(
    draft: Draft, clip_epoch: dict[str, float]
) -> tuple[Draft, bool]:
    """Enforce capture-time order on the EDL, deterministically.

    Claude writes the story and picks clips by narrative; code guarantees the
    visuals never jump backwards in time. Beats are sorted by their clip's
    normalized capture epoch (`created_epoch`, trip-local). The sort is STABLE,
    so multiple beats reusing the same clip keep the author's relative order, and
    a tie on epoch (e.g. two stills with identical EXIF seconds) is left as-is.

    Because `Draft.voiceover_script` is a computed field that joins
    `edl[].narration` in list order, reordering the beats here AUTOMATICALLY
    rebuilds both the EN and ES spoken scripts to match the new visual order —
    EDL order is the single source of truth; the script can never narrate a
    different order than the footage. Returns (new_draft, was_out_of_order).
    """
    keyed = list(enumerate(draft.edl))
    ordered = sorted(keyed, key=lambda p: (clip_epoch.get(p[1].clip, 0.0), p[0]))
    was_out_of_order = [i for i, _ in ordered] != list(range(len(keyed)))
    if not was_out_of_order:
        return draft, False
    new = draft.model_copy(update={"edl": [e for _, e in ordered]})
    return new, True


def validate_edl_against_clips(draft: Draft, clip_durations: dict[str, float]) -> list[str]:
    """Checks the schema can't do alone. Returns error strings (-> retry)."""
    errors = []
    for e in draft.edl:
        if e.clip not in clip_durations:
            errors.append(f"EDL references unknown clip '{e.clip}'. "
                          f"Available: {sorted(clip_durations)}")
        elif is_photo(e.clip):
            pass  # a still has no footage to run out of
        elif e.start_s > clip_durations[e.clip] - 2:
            errors.append(f"start_s={e.start_s} leaves under 2s of footage in "
                          f"'{e.clip}' ({clip_durations[e.clip]:.1f}s long)")
    for label, script in (("narration", draft.voiceover_script),
                          ("narration_es", draft.voiceover_script_es)):
        words = len(script.split())
        if script and not MIN_SCRIPT_WORDS <= words <= MAX_SCRIPT_WORDS:
            errors.append(f"total {label} is {words} words — must be between "
                          f"{MIN_SCRIPT_WORDS} and {MAX_SCRIPT_WORDS}")
    return errors


def valid_hook_candidates(
    draft: Draft, clip_durations: dict[str, float]
) -> list[HookCandidate]:
    """Drop hook candidates the renderer can't honor — silently, so a bad hook
    never fails the whole draft (the script still ships, just without that
    cold-open option). A candidate survives only if its clip exists, is a video
    (not a still — the hook is moving footage), and starts inside the footage
    with at least ~0.8s of runway (the render clamps the on-screen time to what's
    available, freezing the last frame if it must)."""
    out: list[HookCandidate] = []
    for h in draft.hook_candidates:
        dur = clip_durations.get(h.clip)
        if dur is None or is_photo(h.clip):
            continue
        if dur - h.start_s < 0.8:
            continue
        out.append(h)
    return out
