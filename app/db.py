"""SQLite 持久化：任务与记录行，跨进程乐观修订号。

并发模型（多个 Web 进程共用一个库文件）：

- 读：``read_state`` 在一个 SQLite 读事务（WAL 快照）内同时读取任务与全部行，
  因此 revision / timezone / pinned / 行数据天然来自同一个已提交状态。
- 写：``_transact`` 用 ``BEGIN IMMEDIATE`` 取得库级写锁，重新读取并核对
  "提交时仍是所见状态"（修订号、时区、每行 pin 与原始数据），核对通过后
  在同一事务内应用修改并递增修订号，最后 COMMIT。
- 孤儿事务：进程在 COMMIT 之前退出时，SQLite 会回滚未提交事务；
  提交前退出保留旧版，COMMIT 之后退出由 WAL 保证新版持久。
"""
from __future__ import annotations

import os
import sqlite3
import time
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

BUSY_TIMEOUT_MS = 10_000


class Conflict(Exception):
    """提交时发现状态已不是调用方所见的状态（并发修改）。"""


class TaskGone(Exception):
    """提交时任务已不存在。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(task: dict, rows: list[dict]) -> tuple:
    """完整状态指纹：修订、时区、以及每一行的原始值与人工选择。

    修订号足以发现本服务内的并发修改；同时比较整份行数据可防止外部写入
    或修订号未变的撕裂状态被覆盖。
    """
    return (
        task["revision"],
        task["timezone"],
        tuple(
            (r["id"], r["row_index"], r["seq"], r["local_time"], r["pm25"],
             r["pinned_utc"])
            for r in rows
        ),
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    # ---- 连接与初始化 ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,  # 事务由本模块显式控制
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # synchronous 是连接级设置；WAL + FULL 保证 COMMIT 返回即已持久化。
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        """建表并把库切到 WAL；两个进程同时首启时用退避重试。"""
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
        while True:
            conn = self._connect()
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            finally:
                conn.close()

    # ---- 行/任务读取辅助（在给定连接与事务内执行） ----

    @staticmethod
    def _fetch_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _fetch_rows(conn: sqlite3.Connection, task_id: str) -> list[dict]:
        cur = conn.execute(
            "SELECT * FROM rows WHERE task_id = ? ORDER BY row_index", (task_id,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ---- 读快照 ----

    def read_state(
        self, task_id: str, pause_after_task=None
    ) -> tuple[dict | None, list[dict]]:
        """在单个读事务内读取任务与行，返回同一已提交状态的快照。

        ``pause_after_task`` 仅用于演示：在读完任务、尚未读行时暂停，用来
        证明随后发生的换时区不会让响应拼接两个状态。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            task = self._fetch_task(conn, task_id)
            if task is not None and pause_after_task is not None:
                pause_after_task()
            rows = self._fetch_rows(conn, task_id) if task is not None else []
            return task, rows
        finally:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()

    # ---- 创建 ----
    def create_task(self, tz: str, rows: list[dict]) -> tuple[dict, list[dict]]:
        """创建任务并返回提交后的完整状态（单事务）。"""
        task_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO tasks (id, timezone, revision, created_at) VALUES (?,?,?,?)",
                (task_id, tz, 1, _now()),
            )
            conn.executemany(
                "INSERT INTO rows (task_id, row_index, seq, local_time, pm25, pinned_utc)"
                " VALUES (?,?,?,?,?,NULL)",
                [
                    (task_id, r["row_index"], r["seq"], r["local_time"], r["pm25"])
                    for r in rows
                ],
            )
            task = self._fetch_task(conn, task_id)
            rows_db = self._fetch_rows(conn, task_id)
            conn.execute("COMMIT")
            return task, rows_db
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    # ---- 提交修改（对比并交换） ----

    def _transact(
        self,
        task_id: str,
        observed_task: dict,
        observed_rows: list[dict],
        apply_fn,
        before_commit=None,
    ) -> tuple[dict, list[dict]]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = self._fetch_task(conn, task_id)
            rows = self._fetch_rows(conn, task_id)
            if task is None:
                raise TaskGone()
            if fingerprint(task, rows) != fingerprint(observed_task, observed_rows):
                raise Conflict()
            apply_fn(conn)
            conn.execute(
                "UPDATE tasks SET revision = revision + 1 WHERE id = ?", (task_id,)
            )
            new_task = self._fetch_task(conn, task_id)
            new_rows = self._fetch_rows(conn, task_id)
            if before_commit is not None:
                before_commit()
            conn.execute("COMMIT")
            return new_task, new_rows
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def set_selection(
        self,
        task_id: str,
        observed_task: dict,
        observed_rows: list[dict],
        row_id: int,
        utc_iso: str,
        before_commit=None,
    ) -> tuple[dict, list[dict]]:
        """写入人工选择并递增修订号（同一事务，提交时核对所见状态）。"""

        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE rows SET pinned_utc = ? WHERE task_id = ? AND id = ?",
                (utc_iso, task_id, row_id),
            )

        return self._transact(
            task_id, observed_task, observed_rows, apply, before_commit
        )

    def clear_selection(
        self,
        task_id: str,
        observed_task: dict,
        observed_rows: list[dict],
        row_id: int,
        before_commit=None,
    ) -> tuple[dict, list[dict]]:
        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE rows SET pinned_utc = NULL WHERE task_id = ? AND id = ?",
                (task_id, row_id),
            )

        return self._transact(
            task_id, observed_task, observed_rows, apply, before_commit
        )

    def change_timezone(
        self,
        task_id: str,
        observed_task: dict,
        observed_rows: list[dict],
        tz: str,
        before_commit=None,
    ) -> tuple[dict, list[dict]]:
        """换时区：清空全部人工选择，与修订号递增同一事务。"""

        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE tasks SET timezone = ? WHERE id = ?", (tz, task_id)
            )
            conn.execute(
                "UPDATE rows SET pinned_utc = NULL WHERE task_id = ?", (task_id,)
            )

        return self._transact(
            task_id, observed_task, observed_rows, apply, before_commit
        )
