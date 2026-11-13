"""端到端多进程测试：两个真实 uvicorn 进程共用一个 SQLite 库。

覆盖需求里的并发选择、保存中退出、查询中换时区、导出闸门。演示用钩子
（APP_DEMO_HOOKS=1）让竞态可控；钩子关闭时不影响正常语义。

子进程通过 ``tests/_server.py`` 启动，父进程写哨兵文件让它优雅退出——
沙箱可能禁止向子进程发信号。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FALLBACK = """采集序号,本地时间,PM2.5
1,2024-11-03 00:59:00,12.4
2,2024-11-03 01:00:00,13.1
4,2024-11-03 01:59:59,14.0
3,2024-11-03 01:30:00,13.7
5,2024-11-03 02:00:00,15.2
6,2024-11-03 02:30:00,16.8
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(method, url, payload=None, files=None, timeout=20):
    data, headers = None, {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif files is not None:
        boundary = "----testboundary"
        data = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"timezone\"\r\n\r\n"
            f"{files['timezone']}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            "filename=\"data.csv\"\r\nContent-Type: text/csv\r\n\r\n"
            f"{files['csv']}\r\n--{boundary}--\r\n"
        ).encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, ConnectionError):
        return 0, b""  # 进程在演示钩子处退出，连接被切断


def jrequest(method, url, **kw):
    status, raw = request(method, url, **kw)
    return status, json.loads(raw) if raw else None


class Server:
    """一个真实 uvicorn 子进程；用哨兵文件控制退出。"""

    def __init__(self, db: str, flag_dir: str, name: str):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.flag = os.path.join(flag_dir, f"stop-{name}")
        env = dict(os.environ, APP_DB=db, APP_DEMO_HOOKS="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tests._server", str(self.port), self.flag],
            cwd=ROOT, env=env,
        )
        self.wait_ready()

    def wait_ready(self, seconds: float = 30.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if request("GET", self.url + "/api/timezones", timeout=2)[0] == 200:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"uvicorn 未就绪：{self.url}")

    def stop(self, seconds: float = 15.0) -> None:
        Path(self.flag).touch()
        try:
            self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=seconds)
        try:
            os.unlink(self.flag)
        except FileNotFoundError:
            pass


class TwoProcessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "app.db")
        cls.servers = [Server(cls.db, cls.tmp, "a"), Server(cls.db, cls.tmp, "b")]
        cls.urls = [s.url for s in cls.servers]

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def hook(self, url, mode):
        status, _ = jrequest("POST", url + "/api/demo/hooks", payload={"mode": mode})
        self.assertEqual(status, 200)

    def upload(self, tz="America/New_York"):
        status, st = jrequest(
            "POST", self.urls[0] + "/api/tasks",
            files={"csv": FALLBACK, "timezone": tz},
        )
        self.assertEqual(status, 201)
        return st

    @staticmethod
    def row(st, seq):
        return next(r for r in st["rows"] if r["seq"] == seq)

    def test_concurrent_selection_same_revision_one_wins(self):
        st = self.upload()
        tid, rev = st["id"], st["revision"]
        # 两个进程都在读完快照后暂停：保证两边都基于同一修订再提交
        self.hook(self.urls[0], "race")
        self.hook(self.urls[1], "race")
        results = {}

        def choose(name, base, rid, utc):
            results[name] = jrequest(
                "POST", f"{base}/api/tasks/{tid}/selections",
                payload={"row_id": rid, "utc": utc, "revision": rev},
            )

        threads = [
            threading.Thread(target=choose, args=(
                "a", self.urls[0], self.row(st, 2)["id"],
                "2024-11-03T05:00:00+00:00")),
            threading.Thread(target=choose, args=(
                "b", self.urls[1], self.row(st, 3)["id"],
                "2024-11-03T06:30:00+00:00")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.hook(self.urls[0], "")
        self.hook(self.urls[1], "")
        codes = sorted(status for status, _ in results.values())
        self.assertEqual(codes, [200, 409], results)
        _, latest = jrequest("GET", f"{self.urls[0]}/api/tasks/{tid}")
        self.assertEqual(latest["revision"], 2)
        # 只有胜出者的选择落库
        pinned = [r["seq"] for r in latest["rows"] if r["pinned"]]
        self.assertEqual(len(pinned), 1)

    def test_query_not_spliced_by_timezone_change(self):
        st = self.upload()
        tid, rev = st["id"], st["revision"]
        status, st = jrequest(
            "POST", f"{self.urls[0]}/api/tasks/{tid}/selections",
            payload={"row_id": self.row(st, 2)["id"],
                     "utc": "2024-11-03T05:00:00+00:00", "revision": rev},
        )
        self.assertEqual(status, 200)
        rev = st["revision"]
        self.hook(self.urls[0], "read")
        out = {}
        t = threading.Thread(
            target=lambda: out.update(
                r=jrequest("GET", f"{self.urls[0]}/api/tasks/{tid}"))
        )
        t.start()
        time.sleep(0.2)
        tz_status, tz_state = jrequest(
            "POST", f"{self.urls[1]}/api/tasks/{tid}/timezone",
            payload={"timezone": "Asia/Shanghai", "revision": rev},
        )
        t.join()
        self.hook(self.urls[0], "")
        self.assertEqual(tz_status, 200)
        self.assertEqual(tz_state["timezone"], "Asia/Shanghai")
        self.assertTrue(all(r["pinned"] is None for r in tz_state["rows"]))
        get_status, got = out["r"]
        self.assertEqual(get_status, 200)
        # 查询响应必须整体来自换时区之前那份提交
        self.assertEqual(got["revision"], rev)
        self.assertEqual(got["timezone"], "America/New_York")
        self.assertIsNotNone(self.row(got, 2)["pinned"])
        _, latest = jrequest("GET", f"{self.urls[0]}/api/tasks/{tid}")
        self.assertEqual(latest["revision"], rev + 1)

    def test_crash_before_commit_keeps_old_state_and_old_revision_not_exportable(self):
        st = self.upload()
        tid, rev = st["id"], st["revision"]
        self.hook(self.urls[1], "before-commit")
        status, _ = request(
            "POST", f"{self.urls[1]}/api/tasks/{tid}/selections",
            payload={"row_id": self.row(st, 2)["id"],
                     "utc": "2024-11-03T05:00:00+00:00", "revision": rev},
        )
        self.assertEqual(status, 0, "进程应在写出响应前退出")
        self._restart(1)
        self.hook(self.urls[1], "")
        _, after = jrequest("GET", f"{self.urls[0]}/api/tasks/{tid}")
        self.assertEqual(after["revision"], rev)
        self.assertIsNone(self.row(after, 2)["pinned"])
        # 旧修订仍是歧义 -> 不允许导出，绝不会导出"新 UTC"
        export_status, _ = request(
            "GET", f"{self.urls[0]}/api/tasks/{tid}/export?revision={rev}")
        self.assertEqual(export_status, 422)

    def test_crash_after_commit_persists_new_state(self):
        st = self.upload()
        tid, rev = st["id"], st["revision"]
        self.hook(self.urls[1], "after-commit")
        status, _ = request(
            "POST", f"{self.urls[1]}/api/tasks/{tid}/selections",
            payload={"row_id": self.row(st, 2)["id"],
                     "utc": "2024-11-03T05:00:00+00:00", "revision": rev},
        )
        self.assertEqual(status, 0)
        self._restart(1)
        self.hook(self.urls[1], "")
        _, after = jrequest("GET", f"{self.urls[0]}/api/tasks/{tid}")
        self.assertEqual(after["revision"], rev + 1)
        self.assertIsNotNone(self.row(after, 2)["pinned"])

    def test_export_uses_complete_approved_revision(self):
        st = self.upload()
        tid, rev = st["id"], st["revision"]
        for seq, utc in [(2, "2024-11-03T05:00:00+00:00"),
                         (3, "2024-11-03T05:30:00+00:00"),
                         (4, "2024-11-03T05:59:59+00:00")]:
            status, st = jrequest(
                "POST", f"{self.urls[0]}/api/tasks/{tid}/selections",
                payload={"row_id": self.row(st, seq)["id"], "utc": utc,
                         "revision": rev},
            )
            self.assertEqual(status, 200)
            rev = st["revision"]
        self.assertEqual(st["status"], "resolved")
        status, body = request(
            "GET", f"{self.urls[1]}/api/tasks/{tid}/export?revision={rev}")
        self.assertEqual(status, 200)
        lines = body.decode("utf-8-sig").strip().split("\r\n")
        self.assertEqual(lines[2], "2,2024-11-03 01:00:00,13.1,2024-11-03T05:00:00Z")
        # 换时区后旧修订导出被拒绝
        status, _ = jrequest(
            "POST", f"{self.urls[1]}/api/tasks/{tid}/timezone",
            payload={"timezone": "UTC", "revision": rev})
        self.assertEqual(status, 200)
        stale, _ = request(
            "GET", f"{self.urls[0]}/api/tasks/{tid}/export?revision={rev}")
        self.assertEqual(stale, 409)

    def _restart(self, idx):
        old = self.servers[idx]
        old.proc.wait(timeout=30)  # 演示钩子已让进程退出
        self.servers[idx] = Server(self.db, self.tmp, f"r{idx}-{time.time_ns()}")
        self.urls[idx] = self.servers[idx].url


if __name__ == "__main__":
    unittest.main()
