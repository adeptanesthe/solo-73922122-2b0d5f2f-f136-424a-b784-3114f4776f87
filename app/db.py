"""SQLite 持久化：任务与记录行，带乐观修订号（共享任务并发控制）。"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    local_time TEXT NOT NULL,
    pm25 TEXT NOT NULL,
    pinned_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_rows_task ON rows(task_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock, self._conn:
            self._conn.executescript(SCHEMA)

    # ---- 任务 ----
    def create_task(self, tz: str, rows: list[dict]) -> dict:
        task_id = uuid.uuid4().hex[:12]
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks (id, timezone, revision, created_at) VALUES (?,?,?,?)",
                (task_id, tz, 1, _now()),
            )
            self._conn.executemany(
                "INSERT INTO rows (task_id, row_index, seq, local_time, pm25, pinned_utc)"
                " VALUES (?,?,?,?,?,NULL)",
                [
                    (task_id, r["row_index"], r["seq"], r["local_time"], r["pm25"])
                    for r in rows
                ],
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict | None:
        with self.lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_timezone(self, task_id: str, tz: str) -> None:
        """换时区：清空全部人工选择并递增修订号。"""
        with self.lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET timezone = ?, revision = revision + 1 WHERE id = ?",
                (tz, task_id),
            )
            self._conn.execute(
                "UPDATE rows SET pinned_utc = NULL WHERE task_id = ?", (task_id,)
            )

    def bump_revision(self, task_id: str) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET revision = revision + 1 WHERE id = ?", (task_id,)
            )

    # ---- 行 ----
    def get_rows(self, task_id: str) -> list[dict]:
        with self.lock:
            cur = self._conn.execute(
                "SELECT * FROM rows WHERE task_id = ? ORDER BY row_index", (task_id,)
            )
            return [dict(r) for r in cur.fetchall()]

    def get_row(self, task_id: str, row_id: int) -> dict | None:
        with self.lock:
            row = self._conn.execute(
                "SELECT * FROM rows WHERE task_id = ? AND id = ?", (task_id, row_id)
            ).fetchone()
        return dict(row) if row else None

    def set_pin(self, task_id: str, row_id: int, utc_iso: str | None) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "UPDATE rows SET pinned_utc = ? WHERE task_id = ? AND id = ?",
                (utc_iso, task_id, row_id),
            )
