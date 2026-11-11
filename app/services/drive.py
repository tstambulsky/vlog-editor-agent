"""Google Drive via rclone: list/copy the day folder, create tomorrow's."""
import asyncio
import json
from datetime import date
from pathlib import Path

from app.config import settings

from app.schemas import PHOTO_EXTS

VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
IMAGE_EXTS = PHOTO_EXTS | {".heic", ".heif"}  # iPhone uploads HEIC by default
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS


class RcloneError(RuntimeError):
    pass


async def _rclone(*args: str, timeout: int = 1800) -> str:
    proc = await asyncio.create_subprocess_exec(
        "rclone", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RcloneError(f"rclone {' '.join(args[:2])} failed: {stderr.decode()[:500]}")
    return stdout.decode()


def _remote_day(d: date) -> str:
    return f"{settings.drive_remote}/{d.isoformat()}"


async def list_remote_clips(d: date) -> list[dict]:
    try:
        out = await _rclone("lsjson", _remote_day(d), timeout=120)
    except RcloneError as exc:
        if "directory not found" in str(exc).lower():
            return []
        raise
    entries = json.loads(out or "[]")
    return [e for e in entries
            if not e.get("IsDir") and Path(e["Name"]).suffix.lower() in MEDIA_EXTS]


async def sync_day(d: date) -> list[str]:
    """Copy the day's Drive folder into raw/. min-age avoids files still uploading."""
    raw = settings.day_dir(d) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in raw.iterdir()}
    await _rclone("copy", _remote_day(d), str(raw),
                  "--min-age", "2m", "--ignore-existing", "--transfers", "2")
    return sorted({p.name for p in raw.iterdir()
                   if p.suffix.lower() in MEDIA_EXTS} - before)


async def upload_backup(path: Path) -> None:
    """Copy a DB backup to <drive_remote>/_backups/ (off-box copy of the state:
    a server wipe or disk cleanup otherwise loses every day's state, since the
    DB only lives in ./data)."""
    await _rclone("copyto", str(path), f"{settings.drive_remote}/_backups/{path.name}",
                  timeout=600)


async def create_day_folder(d: date) -> None:
    await _rclone("mkdir", _remote_day(d), timeout=120)


async def day_folder_link(d: date) -> str:
    """Shareable URL for the day's Drive folder."""
    return (await _rclone("link", _remote_day(d), timeout=120)).strip()
