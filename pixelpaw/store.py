"""SQLite state: which comments were seen/replied, drafts awaiting review, daily snapshots."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    id           TEXT PRIMARY KEY,
    video_id     TEXT,
    video_title  TEXT,
    author       TEXT,
    author_id    TEXT,
    text         TEXT,
    published_at TEXT,
    category     TEXT,
    sentiment    TEXT,
    draft        TEXT,
    status       TEXT NOT NULL,   -- seen | pending | replied | skipped | moderated | failed | own
    reply_id     TEXT,
    note         TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status);
CREATE INDEX IF NOT EXISTS idx_comments_published ON comments(published_at);

CREATE TABLE IF NOT EXISTS snapshots (
    day         TEXT PRIMARY KEY,
    subscribers INTEGER,
    views       INTEGER,
    videos      INTEGER,
    taken_at    TEXT NOT NULL
);
"""

COMMENT_FIELDS = (
    "id", "video_id", "video_title", "author", "author_id", "text", "published_at",
    "category", "sentiment", "draft", "status", "reply_id", "note",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---- comments -------------------------------------------------------
    def is_known(self, comment_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM comments WHERE id = ?", (comment_id,)).fetchone()
        return row is not None

    def upsert_comment(self, rec: dict) -> None:
        values = {k: rec.get(k) for k in COMMENT_FIELDS}
        values["updated_at"] = _now()
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        updates = ", ".join(f"{k}=excluded.{k}" for k in values if k != "id")
        with self._lock:
            self._db.execute(
                f"INSERT INTO comments ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(values.values()),
            )
            self._db.commit()

    def set_status(self, comment_id: str, status: str, **fields) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("draft", "reply_id", "note")}
        sets = ", ".join([f"{k} = ?" for k in allowed] + ["status = ?", "updated_at = ?"])
        with self._lock:
            self._db.execute(
                f"UPDATE comments SET {sets} WHERE id = ?",
                (*allowed.values(), status, _now(), comment_id),
            )
            self._db.commit()

    def pending(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM comments WHERE status = 'pending' ORDER BY published_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def comments_since(self, iso_ts: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM comments WHERE published_at >= ? AND status != 'own' "
                "ORDER BY published_at DESC",
                (iso_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_reply_texts(self, limit: int = 200) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT draft FROM comments WHERE status = 'replied' AND draft IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["draft"] for r in rows]

    def replies_since(self, iso_ts: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM comments WHERE status = 'replied' AND updated_at >= ?", (iso_ts,)
            ).fetchone()
        return int(row["n"])

    def recent_questions(self, limit: int = 30) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT text FROM comments WHERE category = 'question' "
                "ORDER BY published_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["text"] for r in rows]

    # ---- snapshots ------------------------------------------------------
    def save_snapshot(self, day: str, subscribers: int, views: int, videos: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO snapshots (day, subscribers, views, videos, taken_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET subscribers=excluded.subscribers, "
                "views=excluded.views, videos=excluded.videos, taken_at=excluded.taken_at",
                (day, subscribers, views, videos, _now()),
            )
            self._db.commit()

    def latest_snapshot(self) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM snapshots ORDER BY day DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def last_snapshot_before(self, day: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM snapshots WHERE day < ? ORDER BY day DESC LIMIT 1", (day,)
            ).fetchone()
        return dict(row) if row else None
