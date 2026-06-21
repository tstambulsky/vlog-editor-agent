# Rendering (ffmpeg)

All in `app/services/render.py`. Commands run with `cwd =` the day directory
and relative paths (avoids `ass=` filter path-escaping), `nice -n 15` so
nightly encodes don't starve the 2-core server. Output: 1080×1920, H.264
High, CRF 23, AAC 128k, `+faststart`, hard-capped at 60s.

## Per-beat segments

Each EDL beat becomes `work/segments_v{N}/seg_{lang}_{idx}.mp4`. **Segments are
video-only (`-an`)** — clip audio is muted by design; the voiceover is the
final video's only sound (see Final mix).

A **video** beat:
- `-ss start_s -t in_dur` input trim, where `in_dur = min(available, beat_duration)`.
- If the clip runs out of footage: `tpad=stop_mode=clone` freezes the last
  frame to cover the remainder.

A **photo** beat (`ClipMeta.is_photo`, see [pipeline.md](pipeline.md)):
- still looped (`-loop 1 -t duration`) with a slow Ken Burns zoom
  (`zoompan` 1.0→1.12 over the beat, centered, supersampled 2160×3840→1080×1920)
  so runs of stills don't read as a frozen video.
- `start_s` is always 0; the footage-length validation is skipped for photos.

Segments are per-language because beat durations differ (ES ≈ 10-15% longer);
existing segment files are reused (resume-friendly).

Video filter chain per video segment:

```
[tone map if HDR:] zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,
                   tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p
scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1
```

HDR is detected by `ffprobe color_transfer ∈ {smpte2084, arib-std-b67}`
(`app/services/analyze.py`). Landscape footage (e.g. DJI) is center-cropped,
not pillarboxed. Requires ffmpeg ≥6 with libzimg — the image (trixie) ships 7.1.

## Final mix

Segments concat (`-f concat -c copy`, same encode params so it's safe), then:

- **The voiceover is the only audio** — `loudnorm I=-14:TP=-1.5:LRA=11`, no
  ambient/ducking (segments are silent). Earlier the clip audio was mixed in
  at 0.35 ducked under the VO; removed 2026-06-12 (user wanted clean VO).
- Length = narration end + 0.3s (`min(60, vo_dur + 0.3)`), where `vo_dur` is
  the last spoken word's end (from whisper, see [pipeline.md](pipeline.md)) —
  not the mp3 length, so trailing TTS silence is cut. Any shortfall vs the
  concat is covered by global `tpad` clone freeze; audio fades over the last
  0.3s. **A silent tail can't happen by construction.**
- Subtitles burned with `ass=` unless `BURN_SUBTITLES=false` (see below).

## Subtitles

`app/services/subtitles.py`: word timings → ASS, ≤3-word groups, ≤2.5s per
line, break at sentence/comma, overlap-clamped. Style: DejaVu Sans 76px
(fonts-dejavu-core in the image), white with heavy outline, bottom-center,
MarginV 280 (above X's UI overlay). `BURN_SUBTITLES=false` drops the `ass=`
filter entirely (used to publish Day 1 with no on-screen text — a one-off,
default stays true).

### Word timings — WhisperX forced alignment

`app/services/align.py` (`align_script`) times words in two stages: faster-whisper
transcribes the mp3 into **sentence-level segments** (reliable boundaries, drifty
per-word times), then WhisperX's wav2vec2 phoneme model force-aligns the words
inside **each segment's own audio slice**. Word boundaries land within tens of ms
instead of the 100-400ms drift of free-form whisper. Output
(`words_v{N}_{lang}.json`) feeds **both** subtitles (`write_ass`) and beat
durations (`beat_durations_from_words`), so tight word timing fixes beat-sync too.

The per-segment slicing is load-bearing: an earlier version aligned the whole
clip as one `[0,dur]` segment, and wav2vec2 CTC over a single ~44s span
compressed the **early** words so captions ran ~1-2s ahead of the voice for the
first ~10s before converging. Per-sentence segments fix it.

The transcription supplies **timing only** — its TEXT is mis-heard ASR
("Jet lag" → "JITLAG", "founders" → "Members", "Naval Ravikant's" → "Naval
Ravacans"). Because the TTS reads our exact script, `retext_to_script` re-maps
the timed words back onto the **known script** tokens (`difflib.SequenceMatcher`
over punctuation/case-stripped tokens), emitting each script token **verbatim**
with the matched word's start/end. So `words_v{N}_{lang}.json` — and therefore
the burned captions — can never diverge from the script. Script tokens the ASR
dropped are interpolated from neighbours; timings are clamped monotonic.
Words wav2vec2 can't time (digits, symbols) are interpolated too;
numpy floats are cast to native float so the JSON survives `json.dumps`.
faster-whisper is freed right after the segment pass so it and wav2vec2 aren't
both resident during the render (OOMs the 3.7GB box otherwise).

Fallbacks, in order (`pipeline.stage_voiceover`): WhisperX → free-form
`faster-whisper` transcription (the old, drifty path) → ElevenLabs alignment
(drifts ~1.5s). **Every** fallback path is also run through `retext_to_script`,
so even when alignment fails the captions still show the exact script — the only
thing that ever sets subtitle word TEXT is the script. `subtitle_aligner=whisper`
in `.env` forces the old timing path (still re-texted); a missing torch install
(e.g. the dev image) also falls back. wav2vec2 models download once into the
persisted `./data/hf-cache` volume (see [deploy.md](../operations/deploy.md)).

## Cold-open hook

`FIRST_FRAME_HOOK=true` (default) prepends a 1-2s attention-grabbing **cold-open**
before the narrated body — the agent proposes it, the founder picks it in the
script-review gate (see [telegram.md](../integrations/telegram.md#hook-picker)).
The hook is deliberately **not** an EDL beat: it carries no narration and is
invisible to beat-sync, `voiceover_script`, and the chronological sort. Why
outside the EDL: a beat shows for exactly its narration's length, and the hook
has none; making it a beat (or splicing it into the segment concat) would either
break beat timing or force the voiceover + burned subtitles to shift by the hook
duration. So instead the **body render is left byte-for-byte untouched** and the
hook is concatenated in front as a final step.

Mechanics (`render._make_hook_segment` + `_prepend_hook`):
- The chosen `HookCandidate` (`clip`, `start_s`, `duration_s`) is encoded like a
  normal video segment (same tone-map-if-HDR + vertical-crop chain, last-frame
  freeze if it runs short) but **keeps audio** — the clip's own natural sound,
  `loudnorm`'d to the voiceover's level. A clip with no audio track gets a
  matching strip of `anullsrc` silence instead.
- The hook's video/audio params are forced to **match the body's** (the body's
  final output is `ffprobe`d for its audio sample-rate/channels) so `[hook, body]`
  can be joined with the concat demuxer at `-c copy` — no re-encode of the ~60s
  body. Mismatched channels/rate is the classic copy-concat failure, hence the
  probe-and-match.
- The body's voiceover and burned subtitles are timed to *its* own `t=0`; because
  the body file is self-contained, concatenation naturally offsets them by the
  hook's length with no shifting needed.

Selection lives in `<day>/hook.json` (sibling to `notes/`, via `app/hook.py`,
the [shoutout](../integrations/telegram.md#x-shoutouts-shoutout-appshoutoutpy)
pattern) — never in the `Draft`. `hook.resolve()` returns the explicit pick, or
falls back to the agent's top candidate (so it works even gate-off / fully
automatic). Each fresh or revised draft clears the prior pick and drops
candidates the renderer can't honor (`schemas.valid_hook_candidates`: an existing
**video** clip — never a photo — with ≥0.8s of footage from `start_s`). The hook
re-encodes per language (EN/ES bodies differ), and EN+ES share the same hook.

**Cap exemption**: `MAX_VIDEO_SECONDS` is checked on the *body* only; the ≤2.5s
hook is a cold-open, not narrated time, so the final file may run that much
longer. `scripts/smoke_render.py` covers the prepend end-to-end (the silent
`anullsrc` branch + the copy-concat).

## Size limits

Telegram bots can send ≤50MB: if the final file exceeds 49MB, a CRF-28
720×1280 preview is encoded just for Telegram (`*_preview.mp4`); the
publish-quality file is untouched. Typical 30-50s output: 20-30MB, so one
file usually serves both. X accepts ≤512MB/140s — never a constraint here.

## Validation gates

After the final encode: duration ≤ 60+1s, audio stream present, else
`RenderError`. Synthetic end-to-end check: `scripts/smoke_render.py`
(landscape crop, silent clip, HLG tone-map — tag synthetic HDR with the
`setparams` filter, output flags alone don't stick — the freeze case, and a
HEIC photo → Ken Burns beat).

See also: [pipeline.md](pipeline.md) · [deploy.md](../operations/deploy.md)
