# Zernio (publishing to X / LinkedIn)

Zernio (ex-"Late") is the unified social publishing API. One API key
(`Authorization: Bearer sk_…`), base `https://zernio.com/api/v1`. Free tier
covers 2 connected accounts; X pass-through cost ≈ $0.015/post. Docs:
https://docs.zernio.com (full dump: `/llms-full.txt`). Client:
`app/services/zernio.py` — the only file that talks to it.

## Verified API shapes (live-tested 2026-06-10)

The live API and its docs disagree on the presign fields — trust this table:

| Operation | Endpoint | Notes |
|---|---|---|
| List accounts | `GET /accounts` | `{"accounts": [{platform, _id, username}]}` — empty until OAuth connect completes in the dashboard |
| Presign upload | `POST /media/presign` | **live API wants `filename` + `contentType`** (docs say `fileName`/`fileType` — client sends both spellings); returns `uploadUrl` (PUT the bytes, no auth) + `publicUrl` |
| Create post | `POST /posts` | `{content, mediaItems: [{type: "video", url}], platforms: [{platform, accountId}]}` + ONE of `publishNow: true` / `scheduledFor` (ISO) + `timezone` / neither (= draft) |
| Read post | `GET /posts/{id}` | wrapped: `{"post": {...}}`; overall `status` ∈ published/partial/failed; per-platform entries in `post.platforms[]` with `status`, `platformPostUrl`, `error` |
| Delete post | `DELETE /posts/{id}` | used by the smoke test to clean up drafts |

Platform names: `twitter`, `linkedin`. Post creation returns `post._id`.

## Publish flow (`app/publishing.py`)

1. Gate: day must be `approved`/`scheduled` — the only publish entry point.
2. **One language per platform** (`publishing._langs_for`, from `PUBLISH_LANGS`,
   default `twitter:en,linkedin:es`): English → X, Spanish → LinkedIn. Each
   needed language's video is uploaded once (presign → PUT, cached per lang),
   then one post per platform with its language's caption (`caption_x` for X,
   `caption_linkedin_es` for LinkedIn, etc.). So a default day = **2 posts**
   (was 4 — see [decisions.md](../product/decisions.md) "one language per
   platform"). Both EN+ES videos are still generated regardless.
   Then two footers are appended to the caption right before `create_post`:
   the optional per-day X shoutout (`shoutout.apply_to_caption`, X only, set via
   `/shoutout` → `<day>/shoutouts.json`), and the fixed sign-off
   (`signature.apply_signature`, all posts, `POST_SIGNATURE_EN/ES`). Both are
   text-only, never in the video/EDL. See
   [telegram.md](telegram.md#x-shoutouts-shoutout-appshoutoutpy).
3. `scheduledFor` = next 08:00 America/Los_Angeles, or `publishNow`.
   `only_platforms={...}` limits which platforms post — used to add a channel
   after the others already went out (e.g. LinkedIn after X, without
   re-posting X). Unconfigured platforms are skipped silently.
4. `post_poll` (scheduler, 10 min) reads pending posts, extracts
   `platformPostUrl` per platform, stores it, notifies Telegram, marks the
   day `published` when every post resolved.

## Testing without posting

`scripts/smoke_zernio.py`: presign-uploads a tiny synthetic mp4, creates a
**draft** (neither publishNow nor scheduledFor — X never sees it), reads it
back, deletes it. Run it after any client change.

## Account state

- X: once connected, find its account id via `GET /accounts` and set it in
  `ZERNIO_X_ACCOUNT_ID` (e.g. `<YOUR_X_ACCOUNT_ID>`).
- LinkedIn: once connected, find its account id via `GET /accounts` and set it
  in `ZERNIO_LINKEDIN_ACCOUNT_ID` (e.g. `<YOUR_LINKEDIN_ACCOUNT_ID>`).
- Gotcha: accounts live under a *profile*; an empty `accounts` list right
  after "connecting" means the OAuth flow didn't complete (or you logged into
  the wrong Zernio account) — it typically shows up on retry.

X rejects duplicate/near-duplicate text across posts — naturally avoided
because captions differ daily and the drafting prompt sees recent captions.

See also: [pipeline.md](../architecture/pipeline.md) ·
[telegram.md](telegram.md)
