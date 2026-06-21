"""Read-only web dashboard: trip calendar + per-day detail (clips, drafts,
feedback, voiceovers, renders, posts, events). No actions — approval and
publishing stay in the Telegram bot. Served by aiohttp (already a dep via
aiogram) inside the main event loop; Basic auth, password from .env.

Standalone run (local testing): DASHBOARD_PASSWORD=x python -m app.web
"""
import base64
import binascii
import hmac
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from html import escape as e

from app import db
from app.config import settings

log = logging.getLogger(__name__)

# extensions servable through /media/ — everything else is refused
MEDIA_EXTS = {".mp4", ".mp3", ".jpg", ".jpeg", ".png", ".json", ".md", ".mov",
              ".heic"}

STATUS_COLORS = {
    "collecting": "#8a8f98",
    "synced": "#7d9aa5",
    "analyzed": "#4fb3bf",
    "drafted": "#5b8def",
    "script_review": "#9a7bef",
    "rendered": "#7b6fe0",
    "awaiting_approval": "#e8a13c",
    "approved": "#3fb950",
    "scheduled": "#39c5cf",
    "published": "#2ea043",
    "discarded": "#6e7681",
    "failed": "#f85149",
    "skipped": "#6e7681",
}

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 16px; background: #0d1117;
       color: #e6edf3; font: 15px/1.5 -apple-system, system-ui, sans-serif; }
a { color: #58a6ff; text-decoration: none; }
h1 { font-size: 22px; } h2 { font-size: 17px; margin: 28px 0 10px;
     border-bottom: 1px solid #21262d; padding-bottom: 6px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.card { display: block; background: #161b22; border: 1px solid #21262d; border-radius: 8px;
        padding: 10px 12px; color: inherit; }
.card:hover { border-color: #58a6ff; }
.card .num { font-size: 18px; font-weight: 600; }
.card .date { color: #8b949e; font-size: 12px; }
.card .theme { font-size: 12px; color: #8b949e; margin-top: 4px;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
         font-weight: 600; color: #0d1117; margin-top: 6px; }
.muted { color: #8b949e; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #21262d;
         vertical-align: top; }
th { color: #8b949e; font-weight: 600; }
video { max-width: 320px; max-height: 480px; border-radius: 8px; background: #000; }
audio { width: 100%; max-width: 420px; }
.thumbs img { height: 90px; border-radius: 6px; margin: 2px; }
.clip { background: #161b22; border: 1px solid #21262d; border-radius: 8px;
        padding: 10px 12px; margin-bottom: 8px; }
.draft { background: #161b22; border: 1px solid #21262d; border-radius: 8px;
         padding: 12px 14px; margin-bottom: 14px; }
.feedback { background: #1f2a1f; border-left: 3px solid #3fb950; padding: 6px 10px;
            border-radius: 4px; margin: 8px 0; }
pre { background: #161b22; border: 1px solid #21262d; border-radius: 8px;
      padding: 10px; white-space: pre-wrap; font-size: 13px; }
.row { display: flex; gap: 16px; flex-wrap: wrap; }
"""


# ---------------------------------------------------------------- helpers

def _page(title: str, body: str) -> web.Response:
    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{e(title)}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )
    return web.Response(text=html_doc, content_type="text/html")


def _badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#8a8f98")
    return f"<span class='badge' style='background:{color}'>{e(status)}</span>"


def _media_url(p: str | Path | None) -> str | None:
    """Absolute path (as stored in the DB) -> /media/ URL, or None if the
    file is gone (pruned) or lives outside DATA_DIR."""
    if not p:
        return None
    path = Path(p)
    try:
        rel = path.resolve().relative_to(settings.data_dir.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return "/media/" + quote(str(rel))


def _video(p: str | Path | None) -> str:
    url = _media_url(p)
    if not url:
        return ""
    return f"<video controls preload='none' src='{url}'></video>"


def _audio(p: Path) -> str:
    url = _media_url(p)
    if not url:
        return ""
    return (f"<div><span class='muted'>{e(p.name)}</span><br>"
            f"<audio controls preload='none' src='{url}'></audio></div>")


def _fmt_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------- handlers

async def handle_index(request: web.Request) -> web.Response:
    rows = {r["date"]: r for r in db.all_days()}
    today = datetime.now(settings.tz).date()
    dates = {date.fromisoformat(k) for k in rows}
    d = settings.trip_start_date
    while d <= today:
        dates.add(d)
        d += timedelta(days=1)

    cards = []
    for d in sorted(dates):
        row = rows.get(d.isoformat())
        status = row["status"] if row else "no data"
        theme = (row["theme"] or "") if row else ""
        n_clips = len(db.get_clips(d))
        cards.append(
            f"<a class='card' href='/day/{d.isoformat()}'>"
            f"<div class='num'>Day {settings.day_number(d)}</div>"
            f"<div class='date'>{d.strftime('%a %b %d')} · {n_clips} clips</div>"
            f"{_badge(status)}"
            f"<div class='theme'>{e(theme)}</div></a>"
        )
    body = ("<h1>🎬 Video Journal</h1>"
            + (f"<div class='grid'>{''.join(cards)}</div>" if cards
               else "<p class='muted'>No days yet — the trip hasn't started.</p>"))
    return _page("Video Journal", body)


def _render_clips(d: date, day_dir: Path) -> str:
    clips = db.get_clips(d)
    if not clips:
        return "<p class='muted'>No clips synced yet.</p>"
    out = []
    for c in clips:
        stem = Path(c["filename"]).stem
        frames_dir = day_dir / "work" / "frames" / stem
        thumbs = ""
        if frames_dir.is_dir():
            imgs = sorted(frames_dir.glob("*.jpg"))[:4]
            thumbs = "".join(f"<img src='{_media_url(p)}' loading='lazy'>"
                             for p in imgs if _media_url(p))
        raw = day_dir / "raw" / c["filename"]
        if raw.is_file():
            raw_info = (f"<a href='{_media_url(raw)}' download>download</a>"
                        f" · {_fmt_size(raw.stat().st_size)}")
        else:
            raw_info = "<span class='muted'>raw file pruned</span>"
        if Path(c["filename"]).suffix.lower() in (".jpg", ".jpeg", ".png"):
            dur = "photo"
        else:
            dur = f"{c['duration_s']:.1f}s" if c["duration_s"] else "?"
        hdr = " · HDR" if c["hdr"] else ""
        out.append(
            f"<div class='clip'><b>{e(c['filename'])}</b>"
            f" <span class='muted'>· {dur}{hdr} · {e(c['created_ts'] or '')}</span>"
            f" · {raw_info}<div class='thumbs'>{thumbs}</div></div>"
        )
    return "".join(out)


def _render_draft(row, day_dir: Path) -> str:
    try:
        dr = json.loads(row["draft_json"])
    except (json.JSONDecodeError, TypeError):
        dr = {}
    parts = [f"<h3>v{row['version']} <span class='muted'>· {e(row['trigger'])}"
             f" · {e(row['created_at'] or '')}</span></h3>"]
    if row["feedback_text"]:
        parts.append(f"<div class='feedback'>💬 {e(row['feedback_text'])}</div>")
    if dr.get("theme"):
        parts.append(f"<p><b>{e(dr['theme'])}</b> — {e(dr.get('story_summary', ''))}</p>")

    edl = dr.get("edl") or []
    if edl:
        rows_html = "".join(
            f"<tr><td>{e(str(b.get('clip', '')))}</td>"
            f"<td>{b.get('start_s', '')}</td>"
            f"<td>{e(b.get('narration') or '')}</td>"
            f"<td>{e(b.get('narration_es') or '')}</td></tr>"
            for b in edl if isinstance(b, dict)
        )
        parts.append("<table><tr><th>clip</th><th>start</th><th>narration</th>"
                     f"<th>narración</th></tr>{rows_html}</table>")

    caps = [(k, dr.get(k)) for k in
            ("caption_x", "caption_x_es", "caption_linkedin", "caption_linkedin_es")]
    cap_rows = "".join(f"<tr><th>{k}</th><td>{e(v)}</td>" for k, v in caps if v)
    if cap_rows:
        parts.append(f"<table>{cap_rows}</table>")
    if dr.get("confidence_notes"):
        parts.append(f"<p class='muted'>🤔 {e(dr['confidence_notes'])}</p>")

    vo_dir = day_dir / "work" / "voiceover"
    audios = ""
    if vo_dir.is_dir():
        audios = "".join(_audio(p) for p in
                         sorted(vo_dir.glob(f"vo_v{row['version']}*.mp3")))
    if audios:
        parts.append(f"<div class='row'>{audios}</div>")

    videos = (_video(row["preview_path"] or row["video_path"])
              + _video(row["es_preview_path"] or row["es_video_path"]))
    if videos:
        parts.append(f"<div class='row'>{videos}</div>")
    return f"<div class='draft'>{''.join(parts)}</div>"


async def handle_day(request: web.Request) -> web.Response:
    try:
        d = date.fromisoformat(request.match_info["date"])
    except ValueError:
        raise web.HTTPNotFound()
    day = db.get_day(d)
    day_dir = settings.day_dir(d)

    status = day["status"] if day else "no data"
    theme = (day["theme"] or "") if day else ""
    head = (f"<p><a href='/'>← calendar</a></p>"
            f"<h1>Day {settings.day_number(d)} — {d.isoformat()} {_badge(status)}</h1>"
            + (f"<p><b>{e(theme)}</b></p>" if theme else ""))

    sections = [head, "<h2>📹 Clips</h2>", _render_clips(d, day_dir)]

    drafts = db.get_drafts(d)
    sections.append("<h2>📝 Drafts</h2>")
    sections.append("".join(_render_draft(r, day_dir) for r in drafts)
                    or "<p class='muted'>No drafts yet.</p>")

    posts = db.get_posts(d)
    sections.append("<h2>🚀 Posts</h2>")
    if posts:
        def _post_row(p) -> str:
            url = p["published_url"]
            link = f"<a href='{e(url, quote=True)}'>link</a>" if url else ""
            return (f"<tr><td>{e(p['platform'])}</td><td>{e(p['status'])}</td>"
                    f"<td>{e(p['scheduled_for'] or '')}</td><td>{link}</td>"
                    f"<td>{e(p['error'] or '')}</td></tr>")
        rows_html = "".join(_post_row(p) for p in posts)
        sections.append("<table><tr><th>platform</th><th>status</th><th>scheduled</th>"
                        f"<th>url</th><th>error</th></tr>{rows_html}</table>")
    else:
        sections.append("<p class='muted'>No posts.</p>")

    notes_dir = day_dir / "notes"
    notes = sorted(notes_dir.glob("*.md")) if notes_dir.is_dir() else []
    if notes:
        sections.append("<h2>🗒 Notes</h2>")
        for n in notes:
            try:
                sections.append(f"<pre>{e(n.read_text(errors='replace'))}</pre>")
            except OSError:
                pass

    events = db.get_events(d)
    sections.append("<h2>📋 Events</h2>")
    if events:
        rows_html = "".join(
            f"<tr><td class='muted'>{e(ev['ts'][:19])}</td><td>{e(ev['kind'])}</td>"
            f"<td>{e(ev['detail'] or '')}</td></tr>" for ev in events)
        sections.append(f"<table>{rows_html}</table>")
    else:
        sections.append("<p class='muted'>No events.</p>")

    return _page(f"Day {settings.day_number(d)}", "".join(sections))


async def handle_media(request: web.Request) -> web.StreamResponse:
    rel = request.match_info["path"]
    root = settings.data_dir.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or target.suffix.lower() not in MEDIA_EXTS:
        raise web.HTTPForbidden()
    if not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)  # handles Range requests for video seeking


# ---------------------------------------------------------------- app

@web.middleware
async def auth_middleware(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    ok = False
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            _, _, password = decoded.partition(":")
            ok = hmac.compare_digest(password, settings.dashboard_password)
        except (binascii.Error, UnicodeDecodeError):
            ok = False
    if not ok:
        return web.Response(
            status=401, text="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="video-journal"'})
    return await handler(request)


def create_app() -> web.Application:
    if not settings.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD is empty — dashboard disabled")
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/day/{date}", handle_day)
    app.router.add_get("/media/{path:.*}", handle_media)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), port=settings.dashboard_port)
