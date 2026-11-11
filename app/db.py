"""SQLite state. Low write volume (a few rows/day), so a plain sync
connection guarded by a lock is enough — no async driver needed."""
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date TEXT PRIMARY KEY,
    day_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'collecting',
    theme TEXT,
    current_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL REFERENCES days(date),
    filename TEXT NOT NULL,
    duration_s REAL,
    created_ts TEXT,
    created_local TEXT,
    created_epoch REAL DEFAULT 0,
    hdr INTEGER DEFAULT 0,
    transcript_path TEXT,
    UNIQUE(date, filename)
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL REFERENCES days(date),
    version INTEGER NOT NULL,
    draft_json TEXT NOT NULL,
    vo_path TEXT,
    video_path TEXT,
    preview_path TEXT,
    created_at TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'initial',
    feedback_text TEXT,
    UNIQUE(date, version)
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL REFERENCES days(date),
    platform TEXT NOT NULL,
    zernio_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_for TEXT,
    published_url TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""

DAY_STATUSES = [
    "collecting", "synced", "analyzed", "drafted", "script_review", "rendered",
    "awaiting_approval", "approved", "scheduled", "published",
    "discarded", "failed", "skipped",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = db_path or settings.data_dir / "journal.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        for migration in (
            "ALTER TABLE drafts ADD COLUMN es_video_path TEXT",
            "ALTER TABLE drafts ADD COLUMN es_preview_path TEXT",
            "ALTER TABLE clips ADD COLUMN created_local TEXT",
            "ALTER TABLE clips ADD COLUMN created_epoch REAL DEFAULT 0",
        ):
            try:
                _conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        _conn.commit()
    return _conn


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = connect().execute(sql, params)
        connect().commit()
        return cur


def get_or_create_day(d: date) -> sqlite3.Row:
    row = _exec("SELECT * FROM days WHERE date=?", (d.isoformat(),)).fetchone()
    if row:
        return row
    _exec(
        "INSERT INTO days(date, day_number, status, created_at) VALUES(?,?,?,?)",
        (d.isoformat(), settings.day_number(d), "collecting", _now()),
    )
    return _exec("SELECT * FROM days WHERE date=?", (d.isoformat(),)).fetchone()


def get_day(d: date) -> sqlite3.Row | None:
    return _exec("SELECT * FROM days WHERE date=?", (d.isoformat(),)).fetchone()


def all_days() -> list[sqlite3.Row]:
    return _exec("SELECT * FROM days ORDER BY date").fetchall()


def latest_day_with_status(statuses: tuple[str, ...]) -> sqlite3.Row | None:
    marks = ",".join("?" for _ in statuses)
    return _exec(
        f"SELECT * FROM days WHERE status IN ({marks}) ORDER BY date DESC LIMIT 1",
        statuses,
    ).fetchone()


def set_status(d: date, status: str, **fields: Any) -> None:
    assert status in DAY_STATUSES, status
    sets, params = ["status=?"], [status]
    for k, v in fields.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(d.isoformat())
    _exec(f"UPDATE days SET {', '.join(sets)} WHERE date=?", tuple(params))
    log_event(d, "status", status)


def upsert_clip(d: date, filename: str, duration_s: float, created_ts: str,
                hdr: bool, transcript_path: str | None,
                created_local: str = "", created_epoch: float = 0.0) -> None:
    _exec(
        """INSERT INTO clips(date, filename, duration_s, created_ts, created_local,
                             created_epoch, hdr, transcript_path)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(date, filename) DO UPDATE SET
             duration_s=excluded.duration_s, created_ts=excluded.created_ts,
             created_local=excluded.created_local, created_epoch=excluded.created_epoch,
             hdr=excluded.hdr, transcript_path=excluded.transcript_path""",
        (d.isoformat(), filename, duration_s, created_ts, created_local,
         created_epoch, int(hdr), transcript_path),
    )


def update_clip_time(d: date, filename: str, created_ts: str,
                     created_local: str, created_epoch: float) -> None:
    """Overwrite a clip's capture time (raw + normalized sort keys). Used by
    `/retime` to fix a wrong-camera-clock clip's place in the chronology."""
    _exec(
        """UPDATE clips SET created_ts=?, created_local=?, created_epoch=?
           WHERE date=? AND filename=?""",
        (created_ts, created_local, created_epoch, d.isoformat(), filename),
    )


def get_clips(d: date) -> list[sqlite3.Row]:
    # Order by the tz-normalized epoch (true capture order); fall back to the raw
    # ts for any legacy row that predates normalization (epoch defaults to 0).
    return _exec(
        "SELECT * FROM clips WHERE date=? ORDER BY created_epoch, created_ts",
        (d.isoformat(),),
    ).fetchall()


def save_draft(d: date, version: int, draft: dict, trigger: str = "initial",
               feedback_text: str | None = None) -> None:
    _exec(
        """INSERT INTO drafts(date, version, draft_json, created_at, trigger, feedback_text)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(date, version) DO UPDATE SET draft_json=excluded.draft_json""",
        (d.isoformat(), version, json.dumps(draft, ensure_ascii=False), _now(),
         trigger, feedback_text),
    )
    _exec("UPDATE days SET current_version=?, theme=? WHERE date=?",
          (version, draft.get("theme"), d.isoformat()))


def update_draft_paths(d: date, version: int, **paths: str) -> None:
    sets = ", ".join(f"{k}=?" for k in paths)
    _exec(f"UPDATE drafts SET {sets} WHERE date=? AND version=?",
          (*paths.values(), d.isoformat(), version))


def get_draft(d: date, version: int | None = None) -> sqlite3.Row | None:
    if version is None:
        return _exec(
            "SELECT * FROM drafts WHERE date=? ORDER BY version DESC LIMIT 1",
            (d.isoformat(),),
        ).fetchone()
    return _exec("SELECT * FROM drafts WHERE date=? AND version=?",
                 (d.isoformat(), version)).fetchone()


def get_drafts(d: date) -> list[sqlite3.Row]:
    return _exec(
        "SELECT * FROM drafts WHERE date=? ORDER BY version DESC", (d.isoformat(),)
    ).fetchall()


def all_day_themes() -> list[tuple[int, str, str]]:
    """(day_number, theme, status) for every day that got a theme — the
    agent's trip-wide memory."""
    rows = _exec(
        "SELECT day_number, theme, status FROM days "
        "WHERE theme IS NOT NULL ORDER BY date"
    ).fetchall()
    return [(r["day_number"], r["theme"], r["status"]) for r in rows]


def recent_published_drafts(limit: int = 3) -> list[dict]:
    rows = _exec(
        """SELECT dr.draft_json FROM days d
           JOIN drafts dr ON dr.date = d.date AND dr.version = d.current_version
           WHERE d.status = 'published' ORDER BY d.date DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [json.loads(r["draft_json"]) for r in rows]


def save_post(d: date, platform: str, zernio_id: str | None, status: str,
              scheduled_for: str | None = None, error: str | None = None) -> int:
    cur = _exec(
        """INSERT INTO posts(date, platform, zernio_id, status, scheduled_for, error)
           VALUES(?,?,?,?,?,?)""",
        (d.isoformat(), platform, zernio_id, status, scheduled_for, error),
    )
    return cur.lastrowid


def update_post(post_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), post_id))


def pending_posts() -> list[sqlite3.Row]:
    return _exec(
        "SELECT * FROM posts WHERE status IN ('pending','scheduled','publishing')"
    ).fetchall()


def get_posts(d: date) -> list[sqlite3.Row]:
    return _exec("SELECT * FROM posts WHERE date=?", (d.isoformat(),)).fetchall()


def get_events(d: date) -> list[sqlite3.Row]:
    return _exec("SELECT * FROM events WHERE date=? ORDER BY ts",
                 (d.isoformat(),)).fetchall()


def add_correction(d: date, kind: str, text: str) -> None:
    """The founder's own feedback ('script' | 'tweet' | 'video'), kept in its own
    table so it survives draft churn and feeds every future drafting prompt."""
    text = (text or "").strip()
    if text:
        _exec("INSERT INTO corrections(date, ts, kind, text) VALUES (?, ?, ?, ?)",
              (d.isoformat(), _now(), kind, text))


def recent_corrections(limit: int = 15) -> list[sqlite3.Row]:
    rows = _exec("SELECT date, kind, text FROM corrections ORDER BY id DESC LIMIT ?",
                 (limit,)).fetchall()
    return list(reversed(rows))


def backup_to(path: Path) -> None:
    """Consistent online copy of the SQLite DB (safe while the bot is writing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(path)
    try:
        connect().backup(dst)
    finally:
        dst.close()


def log_event(d: date | None, kind: str, detail: str = "") -> None:
    _exec("INSERT INTO events(date, ts, kind, detail) VALUES(?,?,?,?)",
          (d.isoformat() if d else None, _now(), kind, detail[:2000]))
