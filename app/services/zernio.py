"""Zernio client: media presign upload + post create/status.

Payload shapes verified against https://docs.zernio.com/llms-full.txt
(2026-06-10): presign takes fileName/fileType and returns uploadUrl/publicUrl;
posts are wrapped in {"post": {...}} and carry per-platform entries with
status and platformPostUrl.
"""
from datetime import datetime
from pathlib import Path

import httpx

from app.config import settings


class ZernioError(RuntimeError):
    pass


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.zernio_api_key}"}


async def upload_video(path: Path) -> str:
    """Presign -> PUT -> public URL usable in posts for any platform."""
    async with httpx.AsyncClient(timeout=300) as client:
        # The docs show fileName/fileType but the live API rejects those and
        # asks for filename/contentType — send both spellings to be robust.
        r = await client.post(
            f"{settings.zernio_base_url}/media/presign",
            json={"filename": path.name, "contentType": "video/mp4",
                  "fileName": path.name, "fileType": "video/mp4"},
            headers=_headers(),
        )
        if r.status_code >= 400:
            raise ZernioError(f"presign failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        upload_url = data.get("uploadUrl")
        public_url = data.get("publicUrl")
        if not upload_url or not public_url:
            raise ZernioError(f"unexpected presign response: {data}")

        with path.open("rb") as f:
            put = await client.put(upload_url, content=f.read(),
                                   headers={"Content-Type": "video/mp4"})
        if put.status_code >= 400:
            raise ZernioError(f"upload PUT failed ({put.status_code}): {put.text[:300]}")
    return public_url


async def create_post(platform: str, account_id: str, caption: str,
                      media_url: str, scheduled_for: datetime | None = None,
                      draft: bool = False) -> str:
    """Create an immediate, scheduled, or draft video post. Returns Zernio post id."""
    body: dict = {
        "platforms": [{"platform": platform, "accountId": account_id}],
        "content": caption,
        "mediaItems": [{"type": "video", "url": media_url}],
    }
    if draft:
        pass  # neither scheduledFor nor publishNow -> saved as draft
    elif scheduled_for:
        body["scheduledFor"] = scheduled_for.isoformat()
        body["timezone"] = settings.timezone
    else:
        body["publishNow"] = True

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{settings.zernio_base_url}/posts",
                              json=body, headers=_headers())
    if r.status_code >= 400:
        raise ZernioError(f"create_post failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    post_id = (data.get("post") or {}).get("_id") or data.get("_id")
    if not post_id:
        raise ZernioError(f"unexpected create_post response: {data}")
    return str(post_id)


async def delete_post(post_id: str) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.delete(f"{settings.zernio_base_url}/posts/{post_id}",
                                headers=_headers())
    if r.status_code >= 400:
        raise ZernioError(f"delete_post failed ({r.status_code}): {r.text[:300]}")


async def get_post(post_id: str) -> dict:
    """Returns the unwrapped post object."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{settings.zernio_base_url}/posts/{post_id}",
                             headers=_headers())
    if r.status_code >= 400:
        raise ZernioError(f"get_post failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    return data.get("post") or data


async def list_accounts() -> list[dict]:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{settings.zernio_base_url}/accounts", headers=_headers())
    if r.status_code >= 400:
        raise ZernioError(f"list_accounts failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    return data if isinstance(data, list) else data.get("accounts", [])


def platform_entry(post: dict, platform: str) -> dict:
    for entry in post.get("platforms") or []:
        if entry.get("platform") == platform:
            return entry
    return {}


def extract_published_url(post: dict, platform: str) -> str | None:
    return platform_entry(post, platform).get("platformPostUrl")


def post_status(post: dict, platform: str | None = None) -> str:
    """Overall post status, or the per-platform one when platform is given."""
    if platform:
        entry = platform_entry(post, platform)
        if entry.get("status"):
            return str(entry["status"]).lower()
    return str(post.get("status", "unknown")).lower()
