# ElevenLabs (voiceover)

Client: `app/services/voiceover.py`. Endpoint:
`POST /v1/text-to-speech/{voice_id}/with-timestamps?output_format=mp3_44100_128`,
model `eleven_multilingual_v2` (speaks both EN and ES with the same voice).
Header `xi-api-key`. 3 retries on 5xx/transport; 4xx raises immediately.

## Alignment — fallback only (it drifts)

The response carries two character-level alignments, both stored in
`alignment_v{N}_{lang}.json` (`alignment` = matches input text;
`normalized_alignment` = what was spoken, "6"→"six"). **These are no longer
the primary timing source**: the returned alignment can drift up to ~1.6s vs
the actual mp3 (more pronounced at higher speeds and with cloned voices),
which desynced cuts and subtitles.

Primary timing is now **faster-whisper word timestamps on the generated mp3**
(`words_v{N}_{lang}.json`) — ground truth on the real audio — for both subtitle
timings and beat durations (difflib-aligned to the script). ElevenLabs
alignment is the fallback, then word-count-proportional. See
[pipeline.md](../architecture/pipeline.md) for the beat-mapping detail.

## Voice configuration

All knobs live in `.env` and apply everywhere (drafts, revisions, the 🎙
regenerate button, both languages):

```
ELEVENLABS_VOICE_ID=<YOUR_VOICE_ID>   # the voice you select or clone
VOICE_STABILITY=0.3      # lower = more expressive
VOICE_STYLE=0.55         # higher = more energy
VOICE_SPEED=1.15         # also scales the prompt's words/second math
VOICE_SIMILARITY=0.75
```

Choose a premade voice or clone your own with your TTS provider, then set
`ELEVENLABS_VOICE_ID` to its id. Tune the `VOICE_*` settings to taste — the
defaults shipped in `.env.example` are a reasonable starting point. The
`scripts/voice_test.py` helper sends labeled audio samples of the current
day's script to Telegram, so you can A/B voices and speeds and pick what
sounds best.

## Account / plan notes

- A single API key may be **scoped**: depending on the key's permissions,
  TTS + list-voices can work while `GET /v1/user*` returns
  `missing_permissions` (so the plan tier can't be read via the API). The
  client only needs TTS, so a scoped key is fine.
- Your own cloned voices (instant or professional) generally TTS fine via the
  API once the account holds them; *library* "professional" voices may still
  return `402 paid_plan_required` depending on the plan.
- Bilingual usage is roughly 1.4–1.6k chars/day, so a small paid tier
  (~100k chars/mo) comfortably covers a bilingual month; the very smallest
  tiers (~30k) do not.
- Swapping voices is a config-only change: set `ELEVENLABS_VOICE_ID` to the
  new voice id and nothing else changes, exactly as designed.

## Cost math

A beat-synced day ≈ 140 EN words (~800 chars) + ES (~850 chars) ≈ 1.6k chars
≈ $0.16/day at pay-as-you-go-equivalent rates. Revisions that don't touch
narration cost nothing (voiceovers are copied across versions).

See also: [pipeline.md](../architecture/pipeline.md) ·
[rendering.md](../architecture/rendering.md)
