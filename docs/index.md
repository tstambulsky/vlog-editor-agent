# video-journal-agent — docs index

Daily video journal automation: phone clips in Google Drive → analyzed on the
server → Claude writes a beat-synced story → script-review gate → ElevenLabs
voiceover → ffmpeg render → Telegram approval → published to X and LinkedIn.
Optionally bilingual (one language per platform). Runs on a Linux server with Docker.

## Architecture

- [pipeline.md](architecture/pipeline.md) — end-to-end flow, stages, idempotency, state machine, the beat-sync contract, revision diffing. *(pipeline, EDL, beats, states, feedback)*
- [rendering.md](architecture/rendering.md) — ffmpeg specifics: HDR tone mapping, vertical crop, beat-timed segments, photo Ken Burns, VO-only audio, subtitles + toggle, cold-open hook prepend, size limits. *(ffmpeg, HDR, ASS, photos, freeze, hook, cold-open)*

## Integrations

- [zernio.md](integrations/zernio.md) — verified API shapes, presign upload, post lifecycle, drafts, polling, accounts. *(publish, X, LinkedIn, presign)*
- [elevenlabs.md](integrations/elevenlabs.md) — TTS, why whisper (not alignment) times beats & subtitles, voice selection, plans/limits. *(voiceover, whisper, drift, voice clone)*
- [telegram.md](integrations/telegram.md) — bot commands (/menu, /context, /folder, /preview), native command menu, approval keyboard, script-review gate + hook picker, same-day routing (voice = context), notifications. *(bot, menu, approval, feedback, hook, notify)*

## Operations

- [deploy.md](operations/deploy.md) — server requirements, Docker deploy, runner scripts, testing, disk management. *(server, deploy, runners, pruning)*
- [dashboard.md](operations/dashboard.md) — read-only web dashboard: pages, auth, media serving, ports, local testing. *(aiohttp, calendar, Basic auth)*

## Product

- [decisions.md](product/decisions.md) — editorial line, bilingual mode, publish timing, day numbering, scope choices and their why. *(bilingual, captions, publish time, day numbering)*
