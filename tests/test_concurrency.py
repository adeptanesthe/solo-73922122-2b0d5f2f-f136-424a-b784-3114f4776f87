"""持久化/并发语义测试：独立连接模拟多个共库进程。

test_multiprocess.py 用真实 uvicorn 进程覆盖端到端场景；本文件直接在
Database 层验证快照一致性与提交回滚，便于快速定位问题。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Conflict, Database  # noqa: E402

ROWS = [
    {"row_index": 0, "seq": 1, "local_time": "2024-11-03 00:59:00", "pm25": "12.4"},
    {"row_index": 1, "seq": 2, "local_time": "2024-11-03 01:30:00", "pm25": "13.7"},
    {"row_index": 2, "seq": 3, "local_time": "2024-11-03 02:30:00", "pm25": "16.8"},
]


class MultiConnectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "shared.db")
        self.proc_a = Database(self.path)
        self.proc_b = Database(self.path)  # 模拟第二个共库进程
        self.task, self.rows = self.proc_a.create_task("America/New_York", ROWS)

    def pin_row2(self):
        return next(r for r in self.rows if r["seq"] == 2)

    def test_snapshot_not_spliced_by_concurrent_timezone_change(self):
        observed = {}

        def switch_tz():
            task, rows = self.proc_b.read_state(self.task["id"])
            self.proc_b.change_timezone(self.task["id"], task, rows, "Asia/Shanghai")
            observed["after"] = self.proc_b.read_state(self.task["id"])[0]

        task, rows = self.proc_a.read_state(
            self.task["id"], pause_after_task=switch_tz
        )
        # 任务与行必须都来自换时区之前的那份提交
        self.assertEqual(task["timezone"], "America/New_York")
        self.assertEqual(task["revision"], 1)
        self.assertEqual(len(rows), 3)
        self.assertEqual(observed["after"]["timezone"], "Asia/Shanghai")
        self.assertEqual(observed["after"]["revision"], 2)

    def test_commit_before_exit_rolls_back_pin_and_revision(self):
        task, rows = self.proc_a.read_state(self.task["id"])

        def crash():
            raise RuntimeError("进程在 COMMIT 前退出")

        with self.assertRaises(RuntimeError):
            self.proc_a.set_selection(
                self.task["id"], task, rows, self.pin_row2()["id"],
                "2024-11-03T05:30:00+00:00", before_commit=crash,
            )
        # 另一个"进程"看到的是旧版：选择未变、修订未变
        after_task, after_rows = self.proc_b.read_state(self.task["id"])
        self.assertEqual(after_task["revision"], 1)
        self.assertIsNone(
            next(r for r in after_rows if r["seq"] == 2)["pinned_utc"]
        )

    def test_same_revision_only_one_commit_succeeds(self):
        task_a, rows_a = self.proc_a.read_state(self.task["id"])
        task_b, rows_b = self.proc_b.read_state(self.task["id"])  # 同一修订
        self.proc_a.set_selection(
            self.task["id"], task_a, rows_a, self.pin_row2()["id"],
            "2024-11-03T05:30:00+00:00",
        )
        with self.assertRaises(Conflict):
            self.proc_b.set_selection(
                self.task["id"], task_b, rows_b, self.pin_row2()["id"],
                "2024-11-03T06:30:00+00:00",
            )
        task, rows = self.proc_b.read_state(self.task["id"])
        self.assertEqual(task["revision"], 2)
        self.assertEqual(
            next(r for r in rows if r["seq"] == 2)["pinned_utc"],
            "2024-11-03T05:30:00+00:00",
        )

    def test_write_failure_keeps_original_state(self):
        task, rows = self.proc_a.read_state(self.task["id"])

        def broken(conn):  # 写入后抛错：事务必须整体回滚
            conn.execute("UPDATE tasks SET timezone = 'UTC' WHERE id = ?",
                         (self.task["id"],))
            conn.execute("UPDATE rows SET pinned_utc = 'sentinel' WHERE task_id = ?",
                         (self.task["id"],))
            raise RuntimeError("模拟写入失败")

        with self.assertRaises(RuntimeError):
            self.proc_a._transact(self.task["id"], task, rows, broken)
        after_task, after_rows = self.proc_b.read_state(self.task["id"])
        self.assertEqual(after_task["revision"], task["revision"])
        self.assertEqual(after_task["timezone"], "America/New_York")
        self.assertTrue(all(r["pinned_utc"] is None for r in after_rows))


if __name__ == "__main__":
    unittest.main()
