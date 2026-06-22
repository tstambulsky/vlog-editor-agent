"""Zernio publish-path round-trip WITHOUT publishing anything:
presign-upload a tiny test video, create a DRAFT post, read it back, delete it.

Run on the server:
  docker compose run --rm --no-deps -e PYTHONPATH=/srv/app auto-vlog \
      python scripts/smoke_zernio.py
"""
import asyncio
import subprocess
from pathlib import Path

from app.config import settings
from app.services import zernio


async def main() -> None:
    test_mp4 = Path("/tmp/zernio_test.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(test_mp4)],
        check=True)

    url = await zernio.upload_video(test_mp4)
    print("upload OK:", url[:90])

    post_id = await zernio.create_post(
        "twitter", settings.zernio_x_account_id,
        "SF journal pipeline test draft — never published", url, draft=True)
    print("draft created:", post_id)

    post = await zernio.get_post(post_id)
    print("status:", zernio.post_status(post),
          "| platform entry:", zernio.platform_entry(post, "twitter").get("platform"))
    assert zernio.post_status(post) in ("draft", "pending", "created"), post.get("status")

    await zernio.delete_post(post_id)
    print("draft deleted — round-trip OK, nothing was published")


if __name__ == "__main__":
    asyncio.run(main())
