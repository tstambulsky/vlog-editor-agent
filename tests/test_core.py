from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.claude_cli import extract_json_block
from app.schemas import (Draft, sort_edl_chronologically,
                         validate_edl_against_clips)
from app.services.subtitles import group_words, write_ass
from app.services.voiceover import (beat_durations, beat_durations_from_words,
                                    words_from_alignment)

NARRATION_A = "Day three in San Francisco and I finally get why people stay here building things."
NARRATION_B = "Worked from a cafe all morning and every single table was a startup conversation."


def make_draft(**overrides):
    base = dict(
        theme="Founder energy",
        story_summary="A day exploring whether the hype is real.",
        edl=[{"clip": "IMG_0001.MOV", "start_s": 0, "narration": NARRATION_A,
              "narration_es": "Día tres en San Francisco y por fin entiendo por qué la gente se queda acá construyendo."},
             {"clip": "IMG_0002.MOV", "start_s": 5, "narration": NARRATION_B,
              "narration_es": "Trabajé desde un café toda la mañana y cada mesa era una conversación de startup."}],
        caption_x="Day 3/30 in San Francisco.\nOne thing I noticed today.",
        caption_linkedin="Day 3/30 — a longer reflection for LinkedIn.",
    )
    base.update(overrides)
    return base


class TestSchemas:
    def test_valid_draft(self):
        d = Draft.model_validate(make_draft())
        assert d.voiceover_script == f"{NARRATION_A} {NARRATION_B}"
        assert d.has_spanish
        assert len(d.beats("es")) == 2

    def test_computed_script_in_dump(self):
        dump = Draft.model_validate(make_draft()).model_dump()
        assert NARRATION_A in dump["voiceover_script"]
        assert dump["voiceover_script_es"].startswith("Día tres")

    def test_missing_narration_rejected(self):
        bad = make_draft(edl=[{"clip": "a.MOV", "start_s": 0}])
        with pytest.raises(ValidationError):
            Draft.model_validate(bad)

    def test_caption_x_length(self):
        with pytest.raises(ValidationError):
            Draft.model_validate(make_draft(caption_x="x" * 601))

    def test_edl_against_clips(self):
        d = Draft.model_validate(make_draft())
        errors = validate_edl_against_clips(
            d, {"IMG_0001.MOV": 30.0, "IMG_0002.MOV": 6.0})
        assert any("IMG_0002.MOV" in e and "under 2s" in e for e in errors)
        ok = validate_edl_against_clips(
            d, {"IMG_0001.MOV": 30.0, "IMG_0002.MOV": 30.0})
        assert not any("under 2s" in e for e in ok)

    def test_unknown_clip(self):
        d = Draft.model_validate(make_draft())
        errors = validate_edl_against_clips(d, {"IMG_0001.MOV": 30.0})
        assert any("unknown clip" in e for e in errors)

    def test_beat_durations_from_words(self):
        # 6 words at 1s each; beats of 2/4 words -> boundary at word 2's start
        words = [{"word": w, "start": i * 1.0, "end": i * 1.0 + 0.8}
                 for i, w in enumerate("one two three four five six".split())]
        durs = beat_durations_from_words(words, ["one two", "three four five six"], 5.8)
        assert durs == [2.0, 3.8]

    def test_beat_durations_from_words_reanchors_on_merge(self):
        # whisper merged the first two tokens -> counts are off by one, but the
        # anchor search must still find "three" and keep the boundary right
        words = [{"word": "one-two", "start": 0.0, "end": 1.8},
                 {"word": "three", "start": 2.0, "end": 2.8},
                 {"word": "four", "start": 3.0, "end": 3.8}]
        durs = beat_durations_from_words(words, ["one two", "three four"], 3.8)
        assert durs == pytest.approx([2.0, 1.8])

    def test_beat_durations_from_words_empty_falls_back(self):
        durs = beat_durations_from_words([], ["a b", "c d e f"], 6.0)
        assert durs == [2.0, 4.0]

    def test_beat_durations_repeated_common_word(self):
        # regression: beat0 ends with "the", beat1 starts with "The" — naive
        # first-word matching anchored on the earlier "the" and cut beat0 short
        b0 = "Day one in the city"          # 5 words, "the" at idx 3
        b1 = "The gate said it"             # 4 words
        toks = (b0 + " " + b1).split()
        words = [{"word": t, "start": i * 1.0, "end": i * 1.0 + 0.9}
                 for i, t in enumerate(toks)]
        durs = beat_durations_from_words(words, [b0, b1], 9.0)
        # beat0 must run until word 5 (the second "The"), i.e. ~5.0s, not ~3.0s
        assert durs[0] == pytest.approx(5.0)

    def test_beat_durations_substitution_keeps_sync(self):
        # whisper writes "30" where the script says "thirty"
        beats = ["Day one of thirty here", "Then the city showed up"]
        whisper = "Day one of 30 here Then the city showed up".split()
        words = [{"word": t, "start": i * 1.0, "end": i * 1.0 + 0.9}
                 for i, t in enumerate(whisper)]
        durs = beat_durations_from_words(words, beats, 10.0)
        assert durs[0] == pytest.approx(5.0)  # boundary at "Then" (word 5)

    def test_photo_beats_skip_footage_check(self):
        d = Draft.model_validate(make_draft(
            edl=[{"clip": "IMG_0001.MOV", "start_s": 0, "narration": NARRATION_A,
                  "narration_es": "Texto en español para el primer beat del día."},
                 {"clip": "IMG_0042.jpg", "start_s": 0, "narration": NARRATION_B,
                  "narration_es": "Texto en español para el beat de la foto."}]))
        errors = validate_edl_against_clips(
            d, {"IMG_0001.MOV": 30.0, "IMG_0042.jpg": 0.0})
        # the photo's 0.0s duration must not trip the footage rule
        assert not any("under 2s" in e for e in errors)

    def test_word_budget(self):
        long_narr = "word " * 250  # above MAX_SCRIPT_WORDS ceiling
        d = Draft.model_validate(make_draft(
            edl=[{"clip": "a.MOV", "start_s": 0, "narration": long_narr}]))
        errors = validate_edl_against_clips(d, {"a.MOV": 30.0})
        assert any("words" in e for e in errors)


class TestBeatDurations:
    def test_from_alignment(self):
        beats = ["Hi there.", "Bye now."]
        text = "Hi there. Bye now."
        chars = list(text)
        ends = [round(0.1 * (i + 1), 2) for i in range(len(chars))]
        raw = {"characters": chars, "character_end_times_seconds": ends}
        durs = beat_durations(raw, beats, total_duration=1.8)
        assert len(durs) == 2
        assert abs(sum(durs) - ends[-1]) < 0.3
        assert durs[0] >= 1.0  # floor

    def test_fallback_proportional(self):
        durs = beat_durations({}, ["one two three four", "one two"], 9.0)
        assert durs[0] == 6.0 and durs[1] == 3.0

    def test_whitespace_robust(self):
        beats = ["Hello  world.", "Again."]
        text = "Hello world. Again."
        chars = list(text)
        ends = [0.1 * (i + 1) for i in range(len(chars))]
        raw = {"characters": chars, "character_end_times_seconds": ends}
        durs = beat_durations(raw, beats, 2.0)
        assert len(durs) == 2


class TestExtractJson:
    def test_fenced(self):
        assert extract_json_block('text ```json\n{"a": 1}\n``` more') == '{"a": 1}'

    def test_bare(self):
        assert extract_json_block('Here you go: {"a": {"b": 2}} done') == '{"a": {"b": 2}}'

    def test_string_with_braces(self):
        assert extract_json_block('{"s": "a } inside", "n": 1}') == '{"s": "a } inside", "n": 1}'

    def test_no_json_raises(self):
        with pytest.raises(Exception):
            extract_json_block("no json here")


class TestSubtitles:
    WORDS = [
        {"word": "Day", "start": 0.0, "end": 0.3},
        {"word": "three", "start": 0.35, "end": 0.7},
        {"word": "in", "start": 0.75, "end": 0.85},
        {"word": "San", "start": 0.9, "end": 1.1},
        {"word": "Francisco.", "start": 1.15, "end": 1.7},
        {"word": "Today", "start": 2.0, "end": 2.4},
    ]

    def test_grouping_max_words(self):
        assert all(len(l["text"].split()) <= 3 for l in group_words(self.WORDS))

    def test_no_overlap(self):
        lines = group_words(self.WORDS)
        for a, b in zip(lines, lines[1:]):
            assert a["end"] <= b["start"]

    def test_write_ass(self, tmp_path):
        out = write_ass(self.WORDS, tmp_path / "t.ass")
        assert "Dialogue:" in out.read_text()


class TestAlignment:
    def test_words_from_alignment(self):
        alignment = {
            "characters": list("hi yo"),
            "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4],
            "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5],
        }
        words = words_from_alignment(alignment)
        assert [w["word"] for w in words] == ["hi", "yo"]

    def test_empty(self):
        assert words_from_alignment({}) == []


class TestShoutout:
    def setup_method(self):
        from app import shoutout
        from app.config import settings
        import tempfile
        self.shoutout = shoutout
        self.settings = settings
        self._old_dir = settings.data_dir
        settings.data_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        self.settings.data_dir = self._old_dir

    def test_parse_handles_with_and_without_at(self):
        assert self.shoutout.parse_handles("@alice_dev @bob_builds") == \
            ["alice_dev", "bob_builds"]
        assert self.shoutout.parse_handles("alice, bob") == ["alice", "bob"]

    def test_parse_handles_dedupes_preserving_order(self):
        assert self.shoutout.parse_handles("@a @b @a") == ["a", "b"]

    def test_parse_handles_ignores_garbage(self):
        # too long (>15) and empty tokens are dropped
        assert self.shoutout.parse_handles("@" + "x" * 20 + " @ok") == ["ok"]

    def test_line_two_handles_en(self):
        line = self.shoutout.shoutout_line(["alice_dev", "bob_builds"], "en")
        assert line == "shoutout to @alice_dev and @bob_builds."

    def test_line_es_uses_y(self):
        line = self.shoutout.shoutout_line(["a", "b"], "es")
        assert "@a y @b." in line

    def test_line_single_and_three(self):
        assert self.shoutout.shoutout_line(["solo"], "en").endswith("@solo.")
        three = self.shoutout.shoutout_line(["a", "b", "c"], "en")
        assert three.endswith("@a, @b, and @c.")

    def test_line_empty(self):
        assert self.shoutout.shoutout_line([], "en") == ""

    def test_set_get_roundtrip(self):
        from datetime import date
        d = date(2026, 6, 13)
        self.shoutout.set_handles(d, ["a", "b"])
        assert self.shoutout.get_handles(d) == ["a", "b"]

    def test_set_empty_clears_file_no_artifact(self):
        from datetime import date
        d = date(2026, 6, 13)
        self.shoutout.set_handles(d, ["a"])
        self.shoutout.set_handles(d, [])
        assert self.shoutout.get_handles(d) == []
        assert not (self.settings.day_dir(d) / "shoutouts.json").exists()

    def test_apply_to_x_caption_appends(self):
        from datetime import date
        d = date(2026, 6, 13)
        self.shoutout.set_handles(d, ["a", "b"])
        out = self.shoutout.apply_to_caption("Day 1/30.", d, "twitter", "en")
        assert out.startswith("Day 1/30.")
        assert "@a and @b." in out

    def test_apply_skips_linkedin(self):
        from datetime import date
        d = date(2026, 6, 13)
        self.shoutout.set_handles(d, ["a", "b"])
        assert self.shoutout.apply_to_caption("Day 1.", d, "linkedin", "en") == "Day 1."

    def test_apply_noop_without_handles(self):
        from datetime import date
        d = date(2026, 6, 13)
        assert self.shoutout.apply_to_caption("Day 1.", d, "twitter", "en") == "Day 1."

    def test_apply_idempotent(self):
        from datetime import date
        d = date(2026, 6, 13)
        self.shoutout.set_handles(d, ["a"])
        once = self.shoutout.apply_to_caption("Day 1.", d, "twitter", "en")
        twice = self.shoutout.apply_to_caption(once, d, "twitter", "en")
        assert once == twice


class TestHook:
    """First-frame hook: candidates ride on the Draft (invisible to beat-sync),
    the founder's pick lives in a sibling hook.json, and resolve() defaults to
    the top candidate so it works gate-off too."""

    def setup_method(self):
        import tempfile
        from app import hook
        from app.config import settings
        self.hook = hook
        self.settings = settings
        self._old_dir = settings.data_dir
        self._old_flag = settings.first_frame_hook
        settings.data_dir = Path(tempfile.mkdtemp())
        settings.first_frame_hook = True

    def teardown_method(self):
        self.settings.data_dir = self._old_dir
        self.settings.first_frame_hook = self._old_flag

    def _draft_with_hooks(self, **kw):
        cands = kw.pop("hook_candidates", [
            {"clip": "IMG_0001.MOV", "start_s": 2.0, "duration_s": 1.5, "reason": "best"},
            {"clip": "IMG_0002.MOV", "start_s": 0.0, "duration_s": 1.2, "reason": "alt"},
        ])
        return Draft.model_validate(make_draft(hook_candidates=cands, **kw))

    def test_candidates_roundtrip_and_are_invisible_to_beat_sync(self):
        d = self._draft_with_hooks()
        assert len(d.hook_candidates) == 2
        assert d.hook_candidates[0].clip == "IMG_0001.MOV"
        # the hook never leaks into the spoken script / beats
        assert d.voiceover_script == f"{NARRATION_A} {NARRATION_B}"
        assert len(d.beats("en")) == 2
        dump = d.model_dump()
        assert dump["hook_candidates"][0]["duration_s"] == 1.5

    def test_old_draft_without_field_still_validates(self):
        d = Draft.model_validate(make_draft())  # no hook_candidates key
        assert d.hook_candidates == []

    def test_valid_hook_candidates_drops_unhonorable(self):
        from app.schemas import valid_hook_candidates
        d = self._draft_with_hooks(hook_candidates=[
            {"clip": "GOOD.MOV", "start_s": 1.0, "duration_s": 1.5},     # ok
            {"clip": "MISSING.MOV", "start_s": 0.0, "duration_s": 1.5},  # unknown clip
            {"clip": "STILL.jpg", "start_s": 0.0, "duration_s": 1.5},    # photo
            {"clip": "SHORT.MOV", "start_s": 4.8, "duration_s": 1.5},    # <0.8s left
        ])
        kept = valid_hook_candidates(
            d, {"GOOD.MOV": 8.0, "STILL.jpg": 0.0, "SHORT.MOV": 5.0})
        assert [h.clip for h in kept] == ["GOOD.MOV"]

    def test_resolve_defaults_to_top_candidate(self):
        from datetime import date
        d = date(2026, 6, 14)
        draft = self._draft_with_hooks()
        chosen = self.hook.resolve(d, draft)  # no hook.json yet
        assert chosen is not None and chosen.clip == "IMG_0001.MOV"

    def test_resolve_none_when_feature_off(self):
        from datetime import date
        self.settings.first_frame_hook = False
        assert self.hook.resolve(date(2026, 6, 14), self._draft_with_hooks()) is None

    def test_select_index_then_resolve(self):
        from datetime import date
        d = date(2026, 6, 14)
        draft = self._draft_with_hooks()
        self.hook.select(d, draft, 1)
        chosen = self.hook.resolve(d, draft)
        assert chosen.clip == "IMG_0002.MOV"

    def test_select_none_turns_cold_open_off(self):
        from datetime import date
        d = date(2026, 6, 14)
        draft = self._draft_with_hooks()
        assert self.hook.select(d, draft, None) is None
        assert self.hook.resolve(d, draft) is None  # explicit off wins over default

    def test_clear_restores_default(self):
        from datetime import date
        d = date(2026, 6, 14)
        draft = self._draft_with_hooks()
        self.hook.select(d, draft, None)
        self.hook.clear(d)
        assert self.hook.resolve(d, draft).clip == "IMG_0001.MOV"  # back to top

    def test_duration_clamped_by_schema(self):
        from app.schemas import HookCandidate
        with pytest.raises(ValidationError):  # over the 2.5s ceiling
            HookCandidate(clip="A.MOV", start_s=0, duration_s=4.0)

    def test_mark_hook_kb_checks_only_the_picked_button(self):
        from datetime import date
        from app.bot import _hook_kb, _mark_hook_kb
        d = date(2026, 6, 14)
        kb = _hook_kb(d, shown=[0, 1, 2])  # Hook 1/2/3 → real idx 0/1/2
        marked = _mark_hook_kb(kb, f"hook|{d.isoformat()}|1")  # founder taps Hook 2
        texts = [b.text for row in marked.inline_keyboard for b in row]
        assert texts == ["Hook 1", "✅ Hook 2", "Hook 3", "🚫 No hook"]
        # re-picking moves the check, never stacks it
        again = _mark_hook_kb(marked, f"hook|{d.isoformat()}|2")
        texts2 = [b.text for row in again.inline_keyboard for b in row]
        assert texts2 == ["Hook 1", "Hook 2", "✅ Hook 3", "🚫 No hook"]


class TestForcedAlign:
    def test_interpolate_fills_missing(self):
        from app.services.align import _interpolate_missing
        words = [
            {"word": "Day", "start": 0.0, "end": 0.3},
            {"word": "2026", "start": None, "end": None},  # digits: wav2vec2 skips
            {"word": "rocks.", "start": 1.0, "end": 1.4},
        ]
        out = _interpolate_missing(words)
        assert out[1]["start"] == 0.3 and out[1]["end"] == 1.0  # bridges the gap
        assert all(w["start"] is not None and w["end"] is not None for w in out)

    def test_interpolate_leading_and_trailing(self):
        from app.services.align import _interpolate_missing
        out = _interpolate_missing([
            {"word": "$5", "start": None, "end": None},      # nothing before
            {"word": "flat.", "start": 0.5, "end": 0.9},
            {"word": "!!", "start": None, "end": None},      # nothing after
        ])
        # leading untimed word pins to the next onset (no caption before audio)
        assert out[0]["start"] == 0.5 and out[0]["end"] == 0.5
        # trailing untimed word pins to the previous end
        assert out[2]["start"] == 0.9 and out[2]["end"] == 0.9

    def test_interpolate_preserves_present(self):
        from app.services.align import _interpolate_missing
        words = [{"word": "a", "start": 0.1, "end": 0.2}]
        assert _interpolate_missing(words) == words

    def test_disabled_aligner_raises_so_caller_falls_back(self, monkeypatch):
        import asyncio
        from app.config import settings
        from app.services.align import align_script
        monkeypatch.setattr(settings, "subtitle_aligner", "whisper")
        with pytest.raises(RuntimeError):
            asyncio.run(align_script("hi", Path("x.mp3"), "en", 1.0))


class TestRetextToScript:
    """ASR gives TIMING but mis-hears TEXT; subtitle text must be the script."""

    def _timed(self, *pairs):  # (word, start) -> dicts with end = next start
        out = []
        for i, (word, start) in enumerate(pairs):
            end = pairs[i + 1][1] if i + 1 < len(pairs) else start + 0.3
            out.append({"word": word, "start": start, "end": end})
        return out

    def test_real_example_jetlag(self):
        # script "Jet lag woke me" heard as "JITLAG woke me"
        from app.services.align import retext_to_script
        timed = self._timed(("JITLAG", 0.0), ("woke", 0.5), ("me", 0.8))
        out = retext_to_script(timed, "Jet lag woke me")
        assert [w["word"] for w in out] == ["Jet", "lag", "woke", "me"]
        # one heard token split across two script tokens; both inherit its span
        assert out[0]["start"] == 0.0 and out[1]["start"] == 0.0

    def test_real_example_founders(self):
        from app.services.align import retext_to_script
        timed = self._timed(("Members", 0.0), ("on", 0.4),
                            ("rental", 0.7), ("bikes", 1.1))
        out = retext_to_script(timed, "founders on rental bikes")
        assert [w["word"] for w in out] == ["founders", "on", "rental", "bikes"]
        assert "Members" not in [w["word"] for w in out]

    def test_real_example_naval_ravikant(self):
        from app.services.align import retext_to_script
        # "Naval Ravikant's AngelList" heard as "Naval Ravacans Angelist"
        timed = self._timed(("Naval", 0.0), ("Ravacans", 0.5), ("Angelist", 1.0))
        out = retext_to_script(timed, "Naval Ravikant's AngelList")
        assert [w["word"] for w in out] == ["Naval", "Ravikant's", "AngelList"]

    def test_real_example_founders_cafe(self):
        from app.services.align import retext_to_script
        # "their Founders Cafe" heard as "their founder's café"
        timed = self._timed(("their", 0.0), ("founder's", 0.3), ("café", 0.7))
        out = retext_to_script(timed, "their Founders Cafe")
        assert [w["word"] for w in out] == ["their", "Founders", "Cafe"]

    def test_output_is_exactly_script_tokens(self):
        from app.services.align import retext_to_script
        script = "Jet lag woke me at five and the founders were already coding."
        timed = self._timed(
            ("JITLAG", 0.0), ("woke", 0.4), ("me", 0.7), ("at", 0.9),
            ("5", 1.1), ("and", 1.4), ("the", 1.6), ("Members", 1.8),
            ("were", 2.2), ("already", 2.5), ("coding", 3.0))
        out = retext_to_script(timed, script)
        assert [w["word"] for w in out] == script.split()  # verbatim, no leftovers

    def test_timings_monotonic_non_decreasing(self):
        from app.services.align import retext_to_script
        timed = self._timed(("a", 0.0), ("zzz", 5.0), ("c", 1.0))  # out-of-order
        out = retext_to_script(timed, "a b c")
        starts = [w["start"] for w in out]
        ends = [w["end"] for w in out]
        assert starts == sorted(starts)  # clamped monotonic
        assert all(e >= s for s, e in zip(starts, ends))

    def test_inserted_script_token_interpolated(self):
        from app.services.align import retext_to_script
        # script has a word ("very") the ASR dropped entirely
        timed = self._timed(("the", 0.0), ("good", 1.0))
        out = retext_to_script(timed, "the very good")
        assert [w["word"] for w in out] == ["the", "very", "good"]
        assert all(w["start"] is not None for w in out)
        assert out[0]["start"] <= out[1]["start"] <= out[2]["start"]

    def test_empty_timed_lays_out_script(self):
        from app.services.align import retext_to_script
        out = retext_to_script([], "hello world")
        assert [w["word"] for w in out] == ["hello", "world"]
        assert all(w["start"] is not None for w in out)

    def test_empty_script_returns_timed_unchanged(self):
        from app.services.align import retext_to_script
        timed = self._timed(("anything", 0.0))
        assert retext_to_script(timed, "") == timed

    def test_spanish_accents_and_punctuation(self):
        from app.services.align import retext_to_script
        # ES: accents differ between ASR and script; emit the script verbatim
        timed = self._timed(("desperte", 0.0), ("temprano", 0.5))
        out = retext_to_script(timed, "Desperté temprano.")
        assert [w["word"] for w in out] == ["Desperté", "temprano."]


class TestTimezoneNormalization:
    """Mixed-tz capture times must collapse to one comparable axis (trip-local).

    Video creation_time is UTC ('Z'); photo EXIF is naive *local* wall time. The
    bug: a 19:10 local photo sorted as 19:10 UTC = 12:10 local, scrambling order.
    Trip tz is America/Los_Angeles (settings default).
    """

    def test_utc_video_converted_to_local(self):
        from app.services.analyze import normalize_ts
        # 14:02 UTC in June (PDT = UTC-7) -> 07:02 local
        iso, epoch = normalize_ts("2026-06-12T14:02:04.000000Z")
        assert iso.startswith("2026-06-12T07:02:04")
        assert "-07:00" in iso
        assert epoch > 0

    def test_naive_photo_treated_as_already_local(self):
        from app.services.analyze import normalize_ts
        # naive EXIF 19:10 local must stay 19:10 local (NOT shifted)
        iso, epoch = normalize_ts("2026-06-12T19:10:11")
        assert iso.startswith("2026-06-12T19:10:11")
        assert "-07:00" in iso

    def test_mixed_tz_orders_correctly(self):
        """The actual scrambled-order bug: a 16:25 video, a 19:10 photo, and a
        12:48 video must sort 12:48 < 16:25 < 19:10 once normalized."""
        from app.services.analyze import normalize_ts
        early_video = normalize_ts("2026-06-12T19:48:00Z")   # 12:48 local
        mid_video = normalize_ts("2026-06-12T23:25:00Z")     # 16:25 local
        late_photo = normalize_ts("2026-06-12T19:10:11")     # 19:10 local (naive)
        order = sorted(["mid", "late", "early"],
                       key={"early": early_video[1], "mid": mid_video[1],
                            "late": late_photo[1]}.get)
        assert order == ["early", "mid", "late"]
        # naive-as-UTC would have ranked the 19:10 photo as 12:10 local = first
        assert late_photo[1] > early_video[1]

    def test_empty_and_garbage(self):
        from app.services.analyze import normalize_ts
        assert normalize_ts("") == ("", 0.0)
        assert normalize_ts("not-a-date") == ("", 0.0)

    def test_clipmeta_backfills_normalized_fields(self):
        from app.services.analyze import ClipMeta
        m = ClipMeta(filename="IMG.MOV", duration_s=5.0,
                     created_ts="2026-06-12T14:02:04Z", width=1080, height=1920,
                     hdr=False, has_audio=True)
        assert m.created_local.startswith("2026-06-12T07:02:04")
        assert m.created_epoch > 0

    def test_clipmeta_legacy_json_loads(self):
        # an old clips.json row (no normalized keys) must still construct and
        # backfill from the raw ts
        from app.services.analyze import ClipMeta
        old = {"filename": "IMG.MOV", "duration_s": 5.0,
               "created_ts": "2026-06-12T14:02:04Z", "width": 1080,
               "height": 1920, "hdr": False, "has_audio": True}
        m = ClipMeta(**old)
        assert m.created_epoch > 0


class TestScriptReviewGate:
    """The script-review gate: with SCRIPT_REVIEW on, the pipeline stops after
    drafting (no voiceover/render) until the script is approved; script feedback
    revises without rendering; with the gate off, behavior is unchanged."""

    def setup_method(self):
        import tempfile
        from app import db
        from app.config import settings
        self.db = db
        self.settings = settings
        self._old_dir = settings.data_dir
        self._old_gate = settings.script_review
        self._old_bilingual = settings.bilingual
        settings.data_dir = Path(tempfile.mkdtemp())
        settings.bilingual = False
        db._conn = None  # fresh in-memory-ish sqlite under the tmp data_dir
        db.connect()

    def teardown_method(self):
        self.db._conn = None
        self.settings.data_dir = self._old_dir
        self.settings.script_review = self._old_gate
        self.settings.bilingual = self._old_bilingual

    def _seed_drafted(self, d):
        """Put a day at the 'drafted' state with a saved draft, as if
        sync→analyze→draft already ran."""
        self.db.get_or_create_day(d)
        draft = Draft.model_validate(make_draft())
        self.db.save_draft(d, 1, draft.model_dump(), trigger="initial")
        self.db.set_status(d, "drafted")
        return draft

    def _patch_stages(self, monkeypatch, draft, *, rendered):
        """Stub the heavy stages so run_pipeline/render_phase run without
        ffmpeg/Claude/network. `rendered` is a list we append to whenever a
        voiceover+render would have happened — the test's render tripwire."""
        import app.pipeline as pl

        async def fake_sync(d):
            return []

        async def fake_analyze(d):
            return {"IMG_0001.MOV": type("M", (), {"duration_s": 5.0})()}

        async def fake_draft(d, feedback=None):
            version = (self.db.get_or_create_day(d)["current_version"] or 0) + 1
            self.db.save_draft(d, version, draft.model_dump(),
                               trigger="revision" if feedback else "initial",
                               feedback_text=feedback)
            self.db.set_status(d, "drafted")
            return draft, version

        async def fake_vo_render(d, dr, version, lang):
            rendered.append((d, version, lang))
            from app.services import render as render_svc
            return render_svc.RenderResult(video=Path("v.mp4"),
                                           preview=Path("p.mp4"), duration_s=12.0)

        async def fake_notify(*a, **k):
            return None

        monkeypatch.setattr(pl, "stage_sync", fake_sync)
        monkeypatch.setattr(pl, "stage_analyze", fake_analyze)
        monkeypatch.setattr(pl, "stage_draft", fake_draft)
        monkeypatch.setattr(pl, "_voiceover_and_render", fake_vo_render)
        monkeypatch.setattr(pl, "notify", fake_notify)

    def test_gate_on_stops_at_script_review_without_rendering(self, monkeypatch):
        import asyncio
        from datetime import date
        import app.pipeline as pl
        self.settings.script_review = True
        d = date(2026, 6, 13)
        draft = Draft.model_validate(make_draft())
        rendered = []
        self._patch_stages(monkeypatch, draft, rendered=rendered)

        dr, version, result = asyncio.run(pl.run_pipeline(d))

        assert result is None  # nothing rendered
        assert rendered == []  # the render tripwire never fired
        assert self.db.get_day(d)["status"] == "script_review"

    def test_approval_triggers_render(self, monkeypatch):
        import asyncio
        from datetime import date
        import app.pipeline as pl
        self.settings.script_review = True
        d = date(2026, 6, 13)
        draft = self._seed_drafted(d)
        self.db.set_status(d, "script_review")
        rendered = []
        self._patch_stages(monkeypatch, draft, rendered=rendered)

        dr, version, result = asyncio.run(pl.render_phase(d))

        assert result is not None  # a render happened
        assert ("en" in [lang for _, _, lang in rendered])
        assert self.db.get_day(d)["status"] == "awaiting_approval"

    def test_script_feedback_revises_without_rendering(self, monkeypatch):
        import asyncio
        from datetime import date
        import app.pipeline as pl
        self.settings.script_review = True
        d = date(2026, 6, 13)
        self._seed_drafted(d)
        self.db.set_status(d, "script_review")
        # a revised draft with a different script (so `changed` is non-empty)
        revised = Draft.model_validate(make_draft(
            edl=[{"clip": "IMG_0001.MOV", "start_s": 0,
                  "narration": "A completely rewritten opening beat for the day here.",
                  "narration_es": "Un comienzo completamente reescrito para el día."}]))
        rendered = []
        self._patch_stages(monkeypatch, revised, rendered=rendered)

        dr, version, changed = asyncio.run(pl.revise_script(d, "make it punchier"))

        assert rendered == []  # never rendered on script feedback
        assert "script" in changed
        assert version == 2
        assert self.db.get_day(d)["status"] == "script_review"  # stays in the gate

    def test_gate_off_preserves_render_path(self, monkeypatch):
        import asyncio
        from datetime import date
        import app.pipeline as pl
        self.settings.script_review = False
        d = date(2026, 6, 13)
        draft = Draft.model_validate(make_draft())
        rendered = []
        self._patch_stages(monkeypatch, draft, rendered=rendered)

        dr, version, result = asyncio.run(pl.run_pipeline(d))

        assert result is not None  # rendered straight through
        assert ("en" in [lang for _, _, lang in rendered])
        assert self.db.get_day(d)["status"] == "awaiting_approval"


class TestChronologicalSort:
    def _epochs(self, **kw):
        return kw

    def test_resorts_out_of_order_edl(self):
        # drafter put a later clip first; capture epochs say B is earlier than A
        draft = Draft.model_validate(make_draft(
            edl=[{"clip": "A.MOV", "start_s": 0, "narration": NARRATION_A,
                  "narration_es": "Beat A en español para tener suficiente largo."},
                 {"clip": "B.MOV", "start_s": 0, "narration": NARRATION_B,
                  "narration_es": "Beat B en español para tener suficiente largo."}]))
        epochs = {"A.MOV": 200.0, "B.MOV": 100.0}  # B earlier
        new, changed = sort_edl_chronologically(draft, epochs)
        assert changed
        assert [e.clip for e in new.edl] == ["B.MOV", "A.MOV"]

    def test_voiceover_script_rebuilds_to_match_new_order(self):
        """The core consistency requirement: after reordering beats, the spoken
        script (computed from edl[].narration) must follow the new visual order,
        for BOTH languages."""
        draft = Draft.model_validate(make_draft(
            edl=[{"clip": "A.MOV", "start_s": 0, "narration": NARRATION_A,
                  "narration_es": "Primero en español, suficientemente largo para validar."},
                 {"clip": "B.MOV", "start_s": 0, "narration": NARRATION_B,
                  "narration_es": "Segundo en español, suficientemente largo para validar."}]))
        new, _ = sort_edl_chronologically(draft, {"A.MOV": 200.0, "B.MOV": 100.0})
        # B's narration now leads the script in both languages
        assert new.voiceover_script == f"{NARRATION_B} {NARRATION_A}"
        assert new.voiceover_script_es.startswith("Segundo en español")
        # and beats() (what the renderer/aligner consume) matches the script order
        assert new.beats("en") == [NARRATION_B, NARRATION_A]

    def test_already_ordered_is_noop(self):
        draft = Draft.model_validate(make_draft())
        new, changed = sort_edl_chronologically(draft, {"IMG_0001.MOV": 1.0,
                                                        "IMG_0002.MOV": 2.0})
        assert not changed
        assert new is draft  # untouched object returned

    def test_stable_for_repeated_clip(self):
        # two beats reuse the same clip (same epoch) — author order preserved
        draft = Draft.model_validate(make_draft(
            edl=[{"clip": "A.MOV", "start_s": 0, "narration": "First beat over clip A here.",
                  "narration_es": "Primer beat sobre el clip A, largo suficiente."},
                 {"clip": "A.MOV", "start_s": 5, "narration": "Second beat over clip A here.",
                  "narration_es": "Segundo beat sobre el clip A, largo suficiente."}]))
        new, changed = sort_edl_chronologically(draft, {"A.MOV": 100.0})
        assert not changed
        assert [e.start_s for e in new.edl] == [0, 5]

    def test_final_edl_is_non_decreasing(self):
        draft = Draft.model_validate(make_draft(
            edl=[{"clip": "C.MOV", "start_s": 0, "narration": "Clip C narration goes here now.",
                  "narration_es": "Narración del clip C, largo suficiente para validar."},
                 {"clip": "A.MOV", "start_s": 0, "narration": NARRATION_A,
                  "narration_es": "Narración del clip A, largo suficiente para validar."},
                 {"clip": "B.MOV", "start_s": 0, "narration": NARRATION_B,
                  "narration_es": "Narración del clip B, largo suficiente para validar."}]))
        epochs = {"A.MOV": 300.0, "B.MOV": 200.0, "C.MOV": 100.0}
        new, _ = sort_edl_chronologically(draft, epochs)
        seq = [epochs[e.clip] for e in new.edl]
        assert seq == sorted(seq)


class TestSignature:
    def setup_method(self):
        from app import signature
        from app.config import settings
        self.signature = signature
        self.settings = settings
        self._en = settings.post_signature_en
        self._es = settings.post_signature_es
        settings.post_signature_en = "Edited by my agent."
        settings.post_signature_es = "Editado por mi agente."

    def teardown_method(self):
        self.settings.post_signature_en = self._en
        self.settings.post_signature_es = self._es

    def test_appends_after_blank_lines(self):
        assert self.signature.apply_signature("Day 2 in SF.", "en") == \
            "Day 2 in SF.\n\n\nEdited by my agent."

    def test_uses_spanish_signature(self):
        assert self.signature.apply_signature("Día 2.", "es").endswith(
            "Editado por mi agente.")

    def test_idempotent(self):
        once = self.signature.apply_signature("hi", "en")
        assert self.signature.apply_signature(once, "en") == once

    def test_empty_signature_is_noop(self):
        self.settings.post_signature_en = ""
        assert self.signature.apply_signature("hi", "en") == "hi"

    def test_empty_caption_returns_bare_signature(self):
        assert self.signature.apply_signature("", "en") == "Edited by my agent."


class TestPublishLangs:
    def setup_method(self):
        from app.config import settings
        self.settings = settings
        self._old = settings.publish_langs

    def teardown_method(self):
        self.settings.publish_langs = self._old

    def test_default_routes_en_to_x_es_to_linkedin(self):
        from app.publishing import _langs_for
        self.settings.publish_langs = "twitter:en,linkedin:es"
        assert _langs_for("twitter") == ["en"]
        assert _langs_for("linkedin") == ["es"]

    def test_unlisted_platform_defaults_to_en(self):
        from app.publishing import _langs_for
        self.settings.publish_langs = "linkedin:es"
        assert _langs_for("twitter") == ["en"]

    def test_multiple_langs_per_platform(self):
        from app.publishing import _langs_for
        self.settings.publish_langs = "twitter:en+es,linkedin:es"
        assert _langs_for("twitter") == ["en", "es"]


class TestClipAnalysisCache:
    """Eager per-clip processing + cached vision summary (Parts A & B)."""

    def setup_method(self):
        import tempfile
        from datetime import date
        from app.config import settings
        self.settings = settings
        self._old_dir = settings.data_dir
        settings.data_dir = Path(tempfile.mkdtemp())
        self.d = date(2026, 6, 20)

    def teardown_method(self):
        self.settings.data_dir = self._old_dir

    def _write_analysis(self, stem, *, version, summary="a quiet street", moments=None):
        import json
        from app import pipeline as pl
        dirs = pl.day_dirs(self.d)
        payload = {
            "analysis_version": version, "clip": f"{stem}.MOV",
            "summary": summary, "visual_details": "downtown SF",
            "usable": True, "quality_notes": "",
            "key_moments": moments or [{"timestamp_s": 2.0, "what": "a tram passes"}],
            "transcript_excerpt": "",
        }
        (dirs["clip_analysis"] / f"{stem}.json").write_text(json.dumps(payload))

    # --- schema -------------------------------------------------------------
    def test_schema_roundtrip(self):
        from app.schemas import (ClipAnalysis, KeyMoment, CLIP_ANALYSIS_VERSION)
        a = ClipAnalysis(analysis_version=CLIP_ANALYSIS_VERSION, clip="IMG.MOV",
                         summary="a beach", key_moments=[KeyMoment(timestamp_s=1.5,
                                                                    what="a wave")])
        b = ClipAnalysis.model_validate_json(a.model_dump_json())
        assert b.summary == "a beach"
        assert b.key_moments[0].timestamp_s == 1.5
        assert b.analysis_version == CLIP_ANALYSIS_VERSION

    # --- _clip_needs_processing --------------------------------------------
    def test_needs_processing_predicate(self):
        from app import pipeline as pl
        dirs = pl.day_dirs(self.d)
        clip = dirs["raw"] / "IMG_1.MOV"
        clip.write_bytes(b"x")
        assert pl._clip_needs_processing(self.d, clip) is True
        (dirs["frames"] / "IMG_1").mkdir(parents=True)
        (dirs["frames"] / "IMG_1" / "f_001.jpg").write_bytes(b"x")
        assert pl._clip_needs_processing(self.d, clip) is True  # transcript missing
        (dirs["transcripts"] / "IMG_1.json").write_text("{}")
        assert pl._clip_needs_processing(self.d, clip) is False

    # --- HEIC dedup in _media_files ----------------------------------------
    def test_media_files_drops_heic_with_jpg_sibling(self, monkeypatch):
        from app import pipeline as pl
        monkeypatch.setattr(self.settings, "include_photos", True)
        dirs = pl.day_dirs(self.d)
        for name in ("IMG_9.HEIC", "IMG_9.jpg", "CLIP.MOV"):
            (dirs["raw"] / name).write_bytes(b"x")
        names = {p.name for p in pl._media_files(self.d)}
        assert names == {"IMG_9.jpg", "CLIP.MOV"}  # .HEIC dropped (jpg sibling)

    # --- ensure_clip_analysis skip-if-valid --------------------------------
    def test_analysis_skips_when_valid(self, monkeypatch):
        import asyncio
        from app import pipeline as pl
        from app.schemas import CLIP_ANALYSIS_VERSION
        self._write_analysis("IMG_2", version=CLIP_ANALYSIS_VERSION)

        async def boom(*a, **k):
            raise AssertionError("run_structured must not be called for a valid cache")
        monkeypatch.setattr(pl, "run_structured", boom)
        asyncio.run(pl.ensure_clip_analysis(self.d, "IMG_2.MOV", is_photo=False))

    def test_analysis_recomputes_on_stale_version(self, monkeypatch):
        import asyncio, json
        from app import pipeline as pl
        from app.schemas import ClipAnalysisReply, KeyMoment, CLIP_ANALYSIS_VERSION
        self._write_analysis("IMG_3", version=0, summary="stale")
        calls = []

        async def fake_run(prompt, cwd, schema, extra_validate=None):
            calls.append(schema)
            return ClipAnalysisReply(summary="fresh look",
                                     key_moments=[KeyMoment(timestamp_s=0, what="x")])
        monkeypatch.setattr(pl, "run_structured", fake_run)
        asyncio.run(pl.ensure_clip_analysis(self.d, "IMG_3.MOV", is_photo=False))
        assert calls == [ClipAnalysisReply]
        dirs = pl.day_dirs(self.d)
        saved = json.loads((dirs["clip_analysis"] / "IMG_3.json").read_text())
        assert saved["summary"] == "fresh look"
        assert saved["analysis_version"] == CLIP_ANALYSIS_VERSION
        assert saved["clip"] == "IMG_3.MOV"

    def test_analysis_failure_is_swallowed(self, monkeypatch):
        import asyncio
        from app import pipeline as pl
        monkeypatch.setattr(pl.db, "log_event", lambda *a, **k: None)

        async def boom(*a, **k):
            raise RuntimeError("model down")
        monkeypatch.setattr(pl, "run_structured", boom)
        # must not raise; cache file simply not written
        asyncio.run(pl.ensure_clip_analysis(self.d, "IMG_4.MOV", is_photo=False))
        dirs = pl.day_dirs(self.d)
        assert not (dirs["clip_analysis"] / "IMG_4.json").exists()

    # --- eager_sweep contention --------------------------------------------
    def test_eager_sweep_yields_to_running_build(self, monkeypatch):
        import asyncio
        from app import pipeline as pl
        dirs = pl.day_dirs(self.d)
        (dirs["raw"] / "IMG_5.MOV").write_bytes(b"x")
        called = []

        async def fake_proc(d, clip):
            called.append(clip)
        monkeypatch.setattr(pl, "ensure_clip_processed", fake_proc)

        async def run():
            async with pl._pipeline_lock:          # a build owns the day
                return await pl.eager_sweep(self.d)
        assert asyncio.run(run()) == 0
        assert called == []  # never touched a clip while the build held the lock

    def test_eager_sweep_processes_when_free(self, monkeypatch):
        import asyncio
        from app import pipeline as pl
        from app.services.analyze import ClipMeta
        dirs = pl.day_dirs(self.d)
        (dirs["raw"] / "IMG_6.MOV").write_bytes(b"x")
        seen = []

        async def fake_proc(d, clip):
            seen.append(clip.name)
            return ClipMeta(filename=clip.name, duration_s=5.0, created_ts="",
                            width=0, height=0, hdr=False, has_audio=False)
        monkeypatch.setattr(pl, "ensure_clip_processed", fake_proc)
        n = asyncio.run(pl.eager_sweep(self.d))
        assert n == 1 and seen == ["IMG_6.MOV"]

    # --- stage_analyze refactor still writes clips.json + status -----------
    def test_stage_analyze_loops_and_persists(self, monkeypatch):
        import asyncio, json
        from app import pipeline as pl
        from app.services.analyze import ClipMeta
        dirs = pl.day_dirs(self.d)
        for name in ("A.MOV", "B.MOV"):
            (dirs["raw"] / name).write_bytes(b"x")

        async def fake_proc(d, clip):
            return ClipMeta(filename=clip.name, duration_s=3.0, created_ts="",
                            width=1920, height=1080, hdr=False, has_audio=True)
        monkeypatch.setattr(pl, "ensure_clip_processed", fake_proc)
        status = []
        monkeypatch.setattr(pl.db, "set_status", lambda d, s: status.append(s))
        metas = asyncio.run(pl.stage_analyze(self.d))
        assert set(metas) == {"A.MOV", "B.MOV"}
        saved = json.loads((dirs["work"] / "clips.json").read_text())
        assert set(saved) == {"A.MOV", "B.MOV"}
        assert status == ["analyzed"]

    # --- prompt integration -------------------------------------------------
    def test_clips_section_is_summary_led(self, monkeypatch):
        from app import prompts
        from app.schemas import CLIP_ANALYSIS_VERSION
        self._write_analysis("IMG_7", version=CLIP_ANALYSIS_VERSION,
                             summary="a foggy overlook",
                             moments=[{"timestamp_s": 4.0, "what": "skyline reveal"}])
        rows = [{"filename": "IMG_7.MOV", "duration_s": 8.0, "transcript_path": "",
                 "created_local": "", "created_ts": ""}]
        monkeypatch.setattr(prompts.db, "get_clips", lambda d: rows)
        out = prompts._clips_section(self.d)
        assert "a foggy overlook" in out
        assert "4s skyline reveal" in out
        assert "open these only to verify a detail" in out
        assert "(look at them)" not in out

    def test_clips_section_falls_back_without_cache(self, monkeypatch):
        from app import prompts
        rows = [{"filename": "IMG_8.MOV", "duration_s": 8.0, "transcript_path": "",
                 "created_local": "", "created_ts": ""}]
        monkeypatch.setattr(prompts.db, "get_clips", lambda d: rows)
        out = prompts._clips_section(self.d)
        assert "(look at them)" in out  # no cache -> old read-the-frames block
        assert "summary:" not in out


class TestHumanizer:
    d = date(2026, 6, 15)

    def test_prompt_embeds_skill_and_beat_count(self):
        from app.prompts import build_humanize_prompt
        p = build_humanize_prompt([NARRATION_A, NARRATION_B], "cap x", "cap li",
                                  first_line="day 3/8 in bali")
        # the whole vendored stack is inlined, house rules last (they override)
        assert "Humanizer: remove AI writing patterns" in p
        assert "Stop Slop" in p
        assert p.index("Stop Slop") < p.index("House rules: how this journal writes")
        assert "`day 3/8 in bali`" in p
        assert "A reviewer scored" not in p  # first pass carries no review notes
        # the hard contract that protects beat-sync
        assert "EXACTLY 2 narration beats" in p
        assert "±15%" in p
        # the actual copy to rewrite is present
        assert NARRATION_A in p and "cap x" in p and "cap li" in p

    def test_humanized_text_schema(self):
        from app.schemas import HumanizedText
        h = HumanizedText(narrations=["a", "b"], caption_x="x", caption_linkedin="y")
        assert len(h.narrations) == 2
        with pytest.raises(ValidationError):  # caption_x over 600
            HumanizedText(narrations=["a"], caption_x="x" * 601, caption_linkedin="y")
        with pytest.raises(ValidationError):  # narrations required
            HumanizedText(narrations=[], caption_x="x", caption_linkedin="y")

    def test_tweet_reply_schema(self):
        from app.schemas import TweetReply
        assert TweetReply(caption_x="Day 1/30. A punchier tweet.").caption_x
        with pytest.raises(ValidationError):  # caption_x over 600
            TweetReply(caption_x="x" * 601)

    def test_tweet_revision_prompt_includes_feedback(self):
        from app.prompts import build_tweet_revision_prompt
        p = build_tweet_revision_prompt("current tweet here", "make it punchier",
                                        theme="a theme", script="the script",
                                        first_line="day 2/8 in bali")
        assert "`day 2/8 in bali`" in p
        assert "current tweet here" in p and "make it punchier" in p
        assert "600 characters" in p and '"caption_x"' in p

    def test_stage_humanize_applies_in_place(self, monkeypatch):
        import asyncio

        from app import pipeline
        from app.schemas import HumanizedText
        draft = Draft.model_validate(make_draft())

        async def fake(prompt, cwd, schema, extra_validate=None):
            return HumanizedText(
                narrations=["Day three in SF and I finally get why builders stay.",
                            "Worked from a cafe all morning, every table a startup pitch."],
                caption_x="Day 3/30 in SF. One thing stuck with me.",
                caption_linkedin="Day 3/30, a shorter note.")

        monkeypatch.setattr(pipeline, "run_structured", fake)
        out = asyncio.run(pipeline.stage_humanize(self.d, draft))
        # same beat count/order, rewritten text, untouched mapping + ES
        assert len(out.edl) == 2
        assert out.edl[0].narration.startswith("Day three in SF and I finally")
        assert out.edl[1].clip == "IMG_0002.MOV" and out.edl[1].start_s == 5
        assert out.edl[0].narration_es.startswith("Día tres")  # ES left alone
        assert out.caption_x.startswith("Day 3/30 in SF")
        assert out.caption_linkedin == "Day 3/30, a shorter note."

    def test_stage_humanize_validator_rejects_bad_reply(self, monkeypatch):
        import asyncio

        from app import pipeline
        from app.schemas import HumanizedText
        captured = {}

        async def capture(prompt, cwd, schema, extra_validate=None):
            captured["v"] = extra_validate
            return HumanizedText(narrations=["x" * 20, "y" * 20],
                                 caption_x="c", caption_linkedin="d")

        monkeypatch.setattr(pipeline, "run_structured", capture)
        asyncio.run(pipeline.stage_humanize(self.d, Draft.model_validate(make_draft())))
        v = captured["v"]
        # wrong beat count is rejected
        one = HumanizedText(narrations=["a beat that is long enough"],
                            caption_x="c", caption_linkedin="d")
        assert any("EXACTLY 2" in e for e in v(one))
        # too few words total is rejected (beat-sync would have nothing to ride on)
        tiny = HumanizedText(narrations=["ten chars!!", "ten chars!!"],
                             caption_x="c", caption_linkedin="d")
        assert any("words" in e for e in v(tiny))

    def test_stage_humanize_best_effort_on_failure(self, monkeypatch):
        import asyncio

        from app import db, pipeline
        db.connect()  # log_event on the failure path writes an event row

        async def boom(prompt, cwd, schema, extra_validate=None):
            raise RuntimeError("CLI exploded")

        monkeypatch.setattr(pipeline, "run_structured", boom)
        original = Draft.model_validate(make_draft())
        out = asyncio.run(pipeline.stage_humanize(self.d, original))
        assert out.voiceover_script == original.voiceover_script  # unchanged


class TestClockfix:
    """Camera-clock corrections: the Ray-Ban glasses sometimes stamp a clip with
    a wrong DATE, scrambling capture order vs the iPhone clips. clockfix flags
    those (suspect) and pins a corrected, sticky time (override) in a sibling
    clockfix.json — re-applied on every build so the fix is idempotent."""

    def setup_method(self):
        import tempfile
        from app import clockfix
        from app.config import settings
        self.clockfix = clockfix
        self.settings = settings
        self._old_dir = settings.data_dir
        settings.data_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        self.settings.data_dir = self._old_dir

    def _meta(self, filename, created_local):
        from app.services.analyze import ClipMeta
        # build a ClipMeta whose normalized fields are already set to the given
        # (possibly wrong-day) local ISO, mirroring a fresh probe.
        from app.services.analyze import normalize_ts
        _, epoch = normalize_ts(created_local)
        return ClipMeta(filename=filename, duration_s=5.0,
                        created_ts=created_local, width=1080, height=1920,
                        hdr=False, has_audio=True, created_local=created_local,
                        created_epoch=epoch)

    def test_redate_to_day_keeps_time_of_day(self):
        d = date(2026, 6, 12)
        out = self.clockfix.redate_to_day(d, "2026-06-09T15:30:00-07:00")
        assert out.startswith("2026-06-12T15:30:00")
        assert "-07:00" in out

    def test_redate_to_day_garbage(self):
        d = date(2026, 6, 12)
        assert self.clockfix.redate_to_day(d, "") == ""
        assert self.clockfix.redate_to_day(d, "not-a-date") == ""

    def test_local_for_day_and_epoch_roundtrip(self):
        d = date(2026, 6, 12)
        iso = self.clockfix.local_for_day(d, 15, 30)
        assert iso.startswith("2026-06-12T15:30:00")
        assert "-07:00" in iso
        # epoch_of round-trips back to the same instant
        from datetime import datetime
        assert self.clockfix.epoch_of(iso) == datetime.fromisoformat(iso).timestamp()
        assert self.clockfix.epoch_of("") == 0.0

    def test_apply_redates_wrong_day_clip_and_flags_suspect(self):
        d = date(2026, 6, 12)
        m = self._meta("video-584.mov", "2026-06-09T15:30:00-07:00")
        out = self.clockfix.apply(d, m)
        assert out.created_local.startswith("2026-06-12T15:30:00")  # same time, day d
        assert out.created_epoch == self.clockfix.epoch_of(out.created_local)
        assert "video-584.mov" in self.clockfix.get_suspects(d)
        assert self.clockfix.get_overrides(d)["video-584.mov"] == out.created_local

    def test_apply_leaves_on_day_clip_untouched(self):
        d = date(2026, 6, 12)
        m = self._meta("IMG_0001.MOV", "2026-06-12T07:02:04-07:00")
        before = m.created_local
        out = self.clockfix.apply(d, m)
        assert out.created_local == before
        assert self.clockfix.get_suspects(d) == []
        assert self.clockfix.get_overrides(d) == {}

    def test_override_is_sticky_across_rebuilds(self):
        d = date(2026, 6, 12)
        # first build: wrong-day clip becomes an override
        first = self.clockfix.apply(d, self._meta("video-584.mov",
                                                  "2026-06-09T15:30:00-07:00"))
        pinned = first.created_local
        # second build: the raw probe STILL says the wrong day, but the override
        # wins and re-pins the corrected time (idempotent).
        second = self.clockfix.apply(d, self._meta("video-584.mov",
                                                   "2026-06-09T15:30:00-07:00"))
        assert second.created_local == pinned
        assert self.clockfix.get_suspects(d).count("video-584.mov") == 1

    def test_set_override_and_remove_suspect_persist(self):
        d = date(2026, 6, 12)
        self.clockfix.add_suspect(d, "video-584.mov")
        self.clockfix.set_override(d, "video-584.mov", "2026-06-12T16:00:00-07:00")
        assert self.clockfix.get_overrides(d)["video-584.mov"] == \
            "2026-06-12T16:00:00-07:00"
        self.clockfix.remove_suspect(d, "video-584.mov")
        assert "video-584.mov" not in self.clockfix.get_suspects(d)
        # override survives the suspect removal (it's authoritative)
        assert "video-584.mov" in self.clockfix.get_overrides(d)

    def test_override_wins_over_wrong_day_detection(self):
        d = date(2026, 6, 12)
        # founder pinned 16:00 via /retime; a later rebuild's raw probe is wrong-day
        self.clockfix.set_override(d, "video-584.mov", "2026-06-12T16:00:00-07:00")
        out = self.clockfix.apply(d, self._meta("video-584.mov",
                                                "2026-06-09T15:30:00-07:00"))
        assert out.created_local == "2026-06-12T16:00:00-07:00"
        # not re-flagged as suspect (the override path returns early)
        assert self.clockfix.get_suspects(d) == []


class TestTripConfig:
    def test_day_label_and_bible_use_trip_settings(self, monkeypatch):
        from app import prompts
        from app.config import settings
        monkeypatch.setattr(settings, "trip_place", "Bali")
        monkeypatch.setattr(settings, "trip_days", 8)
        monkeypatch.setattr(settings, "trip_start_date", date(2026, 11, 14))
        d = date(2026, 11, 16)
        assert settings.day_label(d) == "Day 3/8"
        assert prompts.tweet_first_line(d) == "day 3/8 in bali"
        bible = prompts.editorial()
        assert "8 days in Bali" in bible
        assert "{{" not in bible and "San Francisco" not in bible
        assert "House rules" in bible  # writing rules ride along with the bible

    def test_draft_prompt_carries_context_and_corrections(self, monkeypatch, tmp_path):
        from app import db, prompts
        from app.config import settings
        monkeypatch.setattr(settings, "trip_context", "a surf week with friends by the beach")
        d = date(2026, 6, 15)
        db.connect()
        db.add_correction(d, "tweet", "stop saying vibrant")
        p = prompts.build_draft_prompt(d)
        assert "a surf week with friends by the beach" in p
        assert "stop saying vibrant" in p
        assert "The tweet's first line today is exactly" in p


class TestSlopLint:
    def test_clean_tweet_passes(self):
        from app import slop
        t = "day 3/8 in bali\n\nshipped the demo at 2am. the AI part took 20 min\n\nthanks @SomeHost for the dinner"
        assert slop.lint_tweet(t, "day 3/8 in bali") == []

    def test_tweet_findings(self):
        from app import slop
        t = "Day 3/8 in Bali 🌴\n\nI loved it — Acme was great #bali\n\nwould you go?"
        issues = " | ".join(slop.lint_tweet(t, "day 3/8 in bali"))
        for needle in ("first line", "dash", "emoji", "closing question", "hashtags",
                       "Bali", "Acme", "I"):
            assert needle in issues, needle

    def test_lowercase_rule_is_optional(self):
        from app import slop
        t = "day 3/8 in bali\n\nShipped the demo at 2am."
        assert any("lowercase" in i for i in slop.lint_tweet(t, "day 3/8 in bali"))
        assert slop.lint_tweet(t, "day 3/8 in bali", lowercase=False) == []

    def test_writing_rules_skip_missing_reference(self, monkeypatch, tmp_path):
        from app import prompts
        monkeypatch.setattr(prompts, "REFERENCE_VOICES_PATH", tmp_path / "missing.md")
        assert "Reference voices" not in prompts.writing_rules()
        assert "HOUSE RULES" in prompts.writing_rules()

    def test_narration_findings(self):
        from app import slop
        assert slop.lint_narration("Normal spoken line, with Caps. Ends well.") == []
        issues = slop.lint_narration("It was great — truly. Would you come?")
        assert len(issues) == 2


class TestPolishLoop:
    d = date(2026, 6, 15)

    def _setup(self, monkeypatch, scores):
        import asyncio  # noqa: F401
        from app import db, pipeline
        from app.schemas import HumanizedText, SlopReview
        db.connect()
        calls = {"humanize": 0, "review": 0, "notes": []}

        async def fake(prompt, cwd, schema, extra_validate=None):
            if schema is SlopReview:
                n, t = scores[calls["review"]]
                calls["review"] += 1
                return SlopReview(narration_score=n, tweet_score=t, issues=["tweet: \"x\" -> y"])
            calls["humanize"] += 1
            calls["notes"].append("A reviewer scored" in prompt)
            return HumanizedText(
                narrations=[NARRATION_A + f" v{calls['humanize']}", NARRATION_B],
                caption_x="day 1/30 in san francisco\n\nshipped it", caption_linkedin="li")

        monkeypatch.setattr(pipeline, "run_structured", fake)
        return pipeline, calls

    def test_passes_first_review(self, monkeypatch):
        import asyncio
        pipeline, calls = self._setup(monkeypatch, [(47, 46)])
        out = asyncio.run(pipeline.stage_polish(self.d, Draft.model_validate(make_draft())))
        assert calls == {"humanize": 1, "review": 1, "notes": [False]}
        assert out.quality == "narration 47/50 · tweet 46/50"

    def test_rewrites_until_pass_with_review_notes(self, monkeypatch):
        import asyncio
        pipeline, calls = self._setup(monkeypatch, [(40, 38), (46, 45)])
        out = asyncio.run(pipeline.stage_polish(self.d, Draft.model_validate(make_draft())))
        assert calls["humanize"] == 2 and calls["review"] == 2
        assert calls["notes"] == [False, True]  # the rewrite got the reviewer's issues
        assert out.edl[0].narration.endswith("v2") and "45/50" in out.quality

    def test_keeps_best_when_rewrites_get_worse(self, monkeypatch):
        import asyncio
        pipeline, calls = self._setup(monkeypatch, [(44, 44), (30, 30), (35, 35)])
        out = asyncio.run(pipeline.stage_polish(self.d, Draft.model_validate(make_draft())))
        assert calls["review"] == 3  # initial + review_rounds (2) rewrites
        assert out.edl[0].narration.endswith("v1") and "44/50" in out.quality

    def test_lint_caps_score(self, monkeypatch):
        import asyncio
        from app import pipeline
        from app.schemas import SlopReview
        draft = Draft.model_validate(make_draft())  # SF-era caption: wrong first line

        async def fake(prompt, cwd, schema, extra_validate=None):
            return SlopReview(narration_score=50, tweet_score=50, issues=[])

        monkeypatch.setattr(pipeline, "run_structured", fake)
        review, lint = asyncio.run(pipeline.stage_review(self.d, draft))
        assert lint and review.tweet_score < 45
        assert review.issues[:len(lint)] == lint


class TestCorrectionsAndBackup:
    def test_corrections_roundtrip_and_backup(self, tmp_path):
        import sqlite3

        from app import db
        db.connect()
        db.add_correction(date(2026, 6, 15), "script", "less touristy")
        db.add_correction(date(2026, 6, 15), "script", "   ")  # blank ignored
        texts = [r["text"] for r in db.recent_corrections(50)]
        assert texts[-1] == "less touristy" and "" not in texts
        out = tmp_path / "b.sqlite"
        db.backup_to(out)
        n = sqlite3.connect(out).execute("SELECT count(*) FROM corrections").fetchone()[0]
        assert n >= 1


def test_in_trip_window(monkeypatch):
    """Pre-trip days used to ping as 'Day -2' (reported 2026-11-11). Scheduled
    jobs gate on in_trip, which is true only for days 1..trip_days."""
    from app.config import settings
    monkeypatch.setattr(settings, "trip_days", 8)
    monkeypatch.setattr(settings, "trip_start_date", date(2026, 11, 14))
    assert settings.day_number(date(2026, 11, 11)) == -2
    assert not settings.in_trip(date(2026, 11, 11))
    assert not settings.in_trip(date(2026, 11, 13))
    assert settings.in_trip(date(2026, 11, 14))
    assert settings.in_trip(date(2026, 11, 21))
    assert not settings.in_trip(date(2026, 11, 22))
