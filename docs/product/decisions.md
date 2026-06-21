# Design decisions (example)

This documents the design of the system using an **example series** — a 30-day
daily journal — to make the reasoning concrete. Adapt it to your own series:
swap in your own subject, audience, schedule, and platforms.

The example series: one ≤60s vertical video per day on X (+ LinkedIn), personal
and first-person, never a generic travel vlog. The editorial voice lives in
[prompts/editorial.md](../../prompts/editorial.md) (hook → story → personal
observation → reflection; "never just describe the footage"; privacy rules).

## Decisions and their why

- **Series start date** (`TRIP_START_DATE`); Day N = days since start + 1. The
  day before the start can serve as a test day ("Day 0") whose drafts are
  throwaway. Schedule it however your series runs (Day N / TOTAL).
- **Publish at 08:00 next morning** after evening approval (21:30 build).
  Approval after 08:00 → publish immediately. Standalone X posts, no threads.
- **Bilingual (EN + ES)** from day one in the example: same story, both
  languages, written independently (the prompt forbids literal translation;
  neutral Spanish). Both videos are always generated. Drop or change this if
  your series is single-language.
- **One language per platform — English on X, Spanish on LinkedIn**
  (`PUBLISH_LANGS=twitter:en,linkedin:es`). An earlier version auto-published
  EN+ES to *both* accounts, but the same video went out twice in one feed,
  minutes apart. Industry guidance is clear: don't double-post the same content
  in two languages on one account (duplicate-in-feed, split engagement,
  unfollows; LinkedIn especially penalizes back-to-back posting — space 8h+ or
  pick one language). So route by audience fit: each platform gets its own
  language + matching video. A staggered-time variant (EN now, ES +N hours) was
  the alternative; single-language-per-platform won for simplicity. Routing in
  `publishing._langs_for`.
- **Voice**: pick or clone a voice with your TTS provider and document your
  choice here. Premade voices get you started; a personal voice clone makes the
  series feel authored — the author's accent and delivery are part of the
  product. Voice settings (energy, speed) are configurable via `VOICE_*` in
  `.env`; in the example, energy is maxed and speed is 1.15×.
- **Script budget 150-175 words** ≈ 50-58s at 1.15× speed; scales with
  `VOICE_SPEED`. Schema hard cap `MAX_SCRIPT_WORDS=185` (validation headroom);
  the 60s render cap (`max_video_seconds`) is the real ceiling.
- **Approval is mandatory** — nothing publishes without a button tap. Telegram
  is the only control surface (no web UI, by design). This is the single
  publish gate; keep it.
- **Script review before render**: the author reviews and revises the *script*
  before any voiceover or render happens, so feedback is cheap (re-draft only,
  no re-render). The post-render approval gate is separate and still required.
- **Notes > automation guesses**: daily text/voice notes are the main lever
  for story quality; the model also sees every prior day's theme so the series
  builds as one continuous narrative without repeating itself.
- **No music bed, hard cuts only** (documentary feel; ambient audio ducked
  under the voice). A music-bed option could be added later.
- **Highlight-extractor tools rejected**: those extract highlights from
  long-form *spoken* content; this product authors the story and shoots
  b-roll — wrong tool, would also break the targeted-revision loop.
- **Zero-clip days**: reminder + the day can slide; a b-roll fallback folder
  exists as an idea, not implemented.

## Cost envelope (example)

Costs depend entirely on your own API usage and plans. As an example reference:
a publishing API on a free tier (2 accounts) + minimal X pass-through fees +
a TTS provider plan (~$22/mo) + a server you already pay for ≈ **~$22.50/mo**.
Your numbers will differ.

## Open items

- Possible later: staggered-time bilingual posting (EN now / ES +N hours) if you
  ever want both languages on one platform; weekly recap post; subtitle styling.

See also: [pipeline.md](../architecture/pipeline.md) ·
[elevenlabs.md](../integrations/elevenlabs.md)
