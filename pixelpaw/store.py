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

CREATE TABLE IF NOT EXISTS quota (          -- YouTube API units used, per Pacific day
    day    TEXT NOT NULL,
    bucket TEXT NOT NULL,                   -- live | normal
    units  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, bucket)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    day      TEXT PRIMARY KEY,
    calls    INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    cached   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS answer_cache (   -- reviewed !ask answers, reused for repeat questions
    key        TEXT PRIMARY KEY,
    question   TEXT,
    answer     TEXT NOT NULL,
    created_at REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
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

    # ---- quota / AI usage / cache / kv -----------------------------------
    def quota_add(self, day: str, bucket: str, units: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO quota (day, bucket, units) VALUES (?,?,?) "
                "ON CONFLICT(day, bucket) DO UPDATE SET units = units + excluded.units",
                (day, bucket, units),
            )
            self._db.commit()

    def quota_used(self, day: str) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT bucket, units FROM quota WHERE day = ?", (day,)).fetchall()
        return {r["bucket"]: int(r["units"]) for r in rows}

    def ai_add(self, day: str, field: str) -> None:
        assert field in ("calls", "failures", "cached")
        with self._lock:
            self._db.execute(
                f"INSERT INTO ai_usage (day, {field}) VALUES (?, 1) "
                f"ON CONFLICT(day) DO UPDATE SET {field} = {field} + 1",
                (day,),
            )
            self._db.commit()

    def ai_used(self, day: str) -> dict[str, int]:
        with self._lock:
            row = self._db.execute("SELECT calls, failures, cached FROM ai_usage WHERE day = ?", (day,)).fetchone()
        return dict(row) if row else {"calls": 0, "failures": 0, "cached": 0}

    def cache_get(self, key: str, max_age_s: float) -> str | None:
        import time

        with self._lock:
            row = self._db.execute("SELECT answer, created_at FROM answer_cache WHERE key = ?", (key,)).fetchone()
            if not row or time.time() - row["created_at"] > max_age_s:
                return None
            self._db.execute("UPDATE answer_cache SET hits = hits + 1 WHERE key = ?", (key,))
            self._db.commit()
        return row["answer"]

    def cache_put(self, key: str, question: str, answer: str) -> None:
        import time

        with self._lock:
            self._db.execute(
                "INSERT INTO answer_cache (key, question, answer, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET answer=excluded.answer, created_at=excluded.created_at",
                (key, question, answer, time.time()),
            )
            self._db.commit()

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))
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
