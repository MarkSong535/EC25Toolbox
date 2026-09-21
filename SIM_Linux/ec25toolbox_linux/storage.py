from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any


@dataclass(frozen=True)
class PendingEvent:
    event_id: str
    kind: str
    payload: dict[str, Any]
    attempts: int


class EventStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def enqueue(self, event_id: str, kind: str, payload: dict[str, Any], deliver: bool = True) -> bool:
        now = time.time()
        delivered_at = None if deliver else now
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events
                    (event_id, kind, payload_json, created_at, delivered_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, kind, encoded, now, delivered_at, now),
            )
            return cursor.rowcount == 1

    def pending(self, limit: int = 20) -> list[PendingEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, kind, payload_json, attempts
                FROM events
                WHERE delivered_at IS NULL AND next_attempt_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (time.time(), limit),
            ).fetchall()
        return [PendingEvent(row[0], row[1], json.loads(row[2]), row[3]) for row in rows]

    def mark_delivered(self, event_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE events SET delivered_at = ?, last_error = NULL WHERE event_id = ?",
                (time.time(), event_id),
            )

    def mark_failure(self, event_id: str, error: str, retry_after: float) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE events
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE event_id = ?
                """,
                (time.time() + retry_after, error[:2000], event_id),
            )

    def is_delivered(self, event_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT delivered_at FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return row is not None and row[0] is not None

    def metadata(self, key: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def count(self) -> int:
        with self._lock, self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()
