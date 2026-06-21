"""Publishing: the ONLY entry point that can create posts, and it requires
the day to be in 'approved' state — enforced here, nowhere else to bypass."""
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app import db, shoutout, signature
from app.config import settings
from app.services import zernio


class PublishError(RuntimeError):
    pass


def _langs_for(platform: str) -> list[str]:
    """Languages this platform publishes, from `publish_langs`
    ("twitter:en,linkedin:es"). Unlisted platform → English."""
    for part in settings.publish_langs.split(","):
        name, _, langs = part.partition(":")
        if name.strip() == platform and langs.strip():
            return [lang.strip() for lang in langs.split("+") if lang.strip()]
    return ["en"]


def next_publish_time(now: datetime) -> datetime:
    """Next 08:00 (configurable) SF time strictly after now."""
    target = datetime.combine(now.date(),
                              time(settings.publish_hour, settings.publish_minute),
                              tzinfo=settings.tz)
    if target <= now:
        target += timedelta(days=1)
    return target


async def publish_day(d: date, when: datetime | None = None,
                      only_platforms: set[str] | None = None) -> list[dict]:
    """Upload each language's video, create one post per language per platform
    (when=None -> publish now). only_platforms limits which platforms post —
    used to add a channel after the others already went out."""
    day = db.get_day(d)
    if not day or day["status"] not in ("approved", "scheduled"):
        raise PublishError(f"Day {d} is '{day['status'] if day else 'missing'}', not approved")

    draft_row = db.get_draft(d)
    draft = json.loads(draft_row["draft_json"])

    accounts = []  # (platform, account_id, caption_field_en, caption_field_es)
    if settings.zernio_x_account_id:
        accounts.append(("twitter", settings.zernio_x_account_id,
                         "caption_x", "caption_x_es"))
    if settings.zernio_linkedin_account_id:
        accounts.append(("linkedin", settings.zernio_linkedin_account_id,
                         "caption_linkedin", "caption_linkedin_es"))
    if only_platforms:
        accounts = [a for a in accounts if a[0] in only_platforms]
    if not accounts:
        raise PublishError("No Zernio account ids configured")

    # One language per platform (audience fit): English on X, Spanish on
    # LinkedIn — avoids the same video twice in one account's feed.
    videos = {"en": draft_row["video_path"], "es": draft_row["es_video_path"]}
    media_cache: dict[str, str] = {}  # lang -> uploaded media url (upload once)

    async def media_for(lang: str) -> str | None:
        if lang not in media_cache:
            video = Path(videos.get(lang) or "")
            if not video.exists():
                return None
            media_cache[lang] = await zernio.upload_video(video)
        return media_cache[lang]

    results = []
    for platform, account_id, en_field, es_field in accounts:
        for lang in _langs_for(platform):
            if lang == "es" and not settings.bilingual:
                continue
            media_url = await media_for(lang)
            if media_url is None:
                results.append({"platform": platform, "lang": lang, "ok": False,
                                "error": f"missing {lang} video"})
                continue
            caption = draft.get(en_field if lang == "en" else es_field, "")
            # Optional per-day X shoutout — text-post only, never in the EDL.
            caption = shoutout.apply_to_caption(caption, d, platform, lang)
            # Fixed sign-off footer at the very end of every post.
            caption = signature.apply_signature(caption, lang)
            try:
                zid = await zernio.create_post(platform, account_id, caption,
                                               media_url, scheduled_for=when)
                status = "scheduled" if when else "publishing"
                db.save_post(d, platform, zid, status,
                             scheduled_for=when.isoformat() if when else None)
                results.append({"platform": platform, "lang": lang, "ok": True,
                                "zernio_id": zid})
            except zernio.ZernioError as exc:
                db.save_post(d, platform, None, "failed", error=str(exc))
                results.append({"platform": platform, "lang": lang, "ok": False,
                                "error": str(exc)})

    if any(r["ok"] for r in results):
        db.set_status(d, "scheduled" if when else "published",
                      **({} if when else {"published_at": datetime.now(settings.tz).isoformat()}))
    db.log_event(d, "publish_requested", json.dumps(results))
    return results


async def poll_pending_posts() -> list[dict]:
    """Refresh scheduled/publishing posts; returns newly-resolved ones."""
    resolved = []
    for row in db.pending_posts():
        if not row["zernio_id"]:
            continue
        try:
            post = await zernio.get_post(row["zernio_id"])
        except zernio.ZernioError:
            continue
        status = zernio.post_status(post, row["platform"])
        if status in ("published", "posted", "success"):
            url = zernio.extract_published_url(post, row["platform"])
            db.update_post(row["id"], status="published", published_url=url)
            d = date.fromisoformat(row["date"])
            posts = db.get_posts(d)
            if all(p["status"] in ("published", "failed") for p in posts):
                db.set_status(d, "published",
                              published_at=datetime.now(settings.tz).isoformat())
            resolved.append({"date": row["date"], "platform": row["platform"],
                             "ok": True, "url": url})
        elif status in ("failed", "error"):
            entry = zernio.platform_entry(post, row["platform"])
            err = str(entry.get("error") or post.get("error") or "unknown")
            db.update_post(row["id"], status="failed", error=err)
            resolved.append({"date": row["date"], "platform": row["platform"],
                             "ok": False, "error": err})
    return resolved
