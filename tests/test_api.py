"""端到端 API 测试：上传、消歧、并发修订、导出。"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp = tempfile.mkdtemp()
os.environ["APP_DB"] = str(Path(_tmp) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

FALLBACK = """采集序号,本地时间,PM2.5
1,2024-11-03 00:59:00,12.4
2,2024-11-03 01:00:00,13.1
4,2024-11-03 01:59:59,14.0
3,2024-11-03 01:30:00,13.7
5,2024-11-03 02:00:00,15.2
6,2024-11-03 02:30:00,16.8
"""

GAP = """采集序号,本地时间,PM2.5
1,2024-03-10 01:59:59,9.8
2,2024-03-10 02:30:00,11.0
3,2024-03-10 03:00:00,12.2
"""


def upload(csv_text, tz="America/New_York"):
    resp = client.post(
        "/api/tasks",
        files={"file": ("data.csv", csv_text.encode(), "text/csv")},
        data={"timezone": tz},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def row_by_seq(state, seq):
    return next(r for r in state["rows"] if r["seq"] == seq)


class FallbackFlowTest(unittest.TestCase):
    def test_full_disambiguation_flow(self):
        st = upload(FALLBACK)
        self.assertEqual(st["status"], "ambiguous")
        self.assertEqual(st["ambiguous_count"], 3)
        self.assertEqual(st["revision"], 1)
        # 保留原行顺序：文件中第 3 行是 seq=4
        self.assertEqual([r["seq"] for r in st["rows"]], [1, 2, 4, 3, 5, 6])
        # 回拨行给出全部两个 UTC 候选
        self.assertEqual(len(row_by_seq(st, 2)["viable"]), 2)

        tid = st["id"]
        rev = st["revision"]
        # 依次人工选择 fold0（EDT 侧），联动重算后全部唯一
        for seq, utc in [(2, "2024-11-03T05:00:00+00:00"),
                         (3, "2024-11-03T05:30:00+00:00"),
                         (4, "2024-11-03T05:59:59+00:00")]:
            rid = row_by_seq(st, seq)["id"]
            resp = client.post(f"/api/tasks/{tid}/selections",
                               json={"row_id": rid, "utc": utc, "revision": rev})
            self.assertEqual(resp.status_code, 200, resp.text)
            st = resp.json()
            rev = st["revision"]
        self.assertEqual(st["status"], "resolved")

        # 导出：含原值与 UTC，保持原行顺序
        resp = client.get(f"/api/tasks/{tid}/export", params={"revision": rev})
        self.assertEqual(resp.status_code, 200, resp.text)
        lines = resp.content.decode("utf-8-sig").strip().split("\r\n")
        self.assertEqual(lines[0], "采集序号,本地时间,PM2.5,UTC时间")
        self.assertEqual(lines[1], "1,2024-11-03 00:59:00,12.4,2024-11-03T04:59:00Z")
        self.assertEqual(lines[2], "2,2024-11-03 01:00:00,13.1,2024-11-03T05:00:00Z")
        self.assertEqual(lines[3], "4,2024-11-03 01:59:59,14.0,2024-11-03T05:59:59Z")
        self.assertEqual(lines[4], "3,2024-11-03 01:30:00,13.7,2024-11-03T05:30:00Z")

    def test_stale_revision_rejected(self):
        st = upload(FALLBACK)
        tid, rev = st["id"], st["revision"]
        rid = row_by_seq(st, 2)["id"]
        ok = client.post(f"/api/tasks/{tid}/selections",
                         json={"row_id": rid, "utc": "2024-11-03T05:00:00+00:00",
                               "revision": rev})
        self.assertEqual(ok.status_code, 200)
        # 用旧修订再次修改 -> 409
        stale = client.post(f"/api/tasks/{tid}/selections",
                            json={"row_id": rid, "utc": "2024-11-03T06:00:00+00:00",
                                  "revision": rev})
        self.assertEqual(stale.status_code, 409)
        # 旧修订导出 -> 409
        stale_export = client.get(f"/api/tasks/{tid}/export",
                                  params={"revision": rev})
        self.assertEqual(stale_export.status_code, 409)

    def test_contradictory_selection_rejected_and_preserved(self):
        st = upload(FALLBACK)
        tid = st["id"]
        # pin seq2 -> 06:00（fold1），则 seq3 只能 06:30、seq4 只能 06:59:59
        r = client.post(f"/api/tasks/{tid}/selections",
                        json={"row_id": row_by_seq(st, 2)["id"],
                              "utc": "2024-11-03T06:00:00+00:00",
                              "revision": st["revision"]})
        st = r.json()
        self.assertEqual(row_by_seq(st, 4)["viable"][0]["utc"],
                         "2024-11-03T06:59:59+00:00")
        # 矛盾选择：seq4 选 05:59:59（合法候选但无法参与完整解释）-> 拒绝并保留原选择
        bad = client.post(f"/api/tasks/{tid}/selections",
                          json={"row_id": row_by_seq(st, 4)["id"],
                                "utc": "2024-11-03T05:59:59+00:00",
                                "revision": st["revision"]})
        self.assertEqual(bad.status_code, 409)
        self.assertIn("矛盾", bad.json()["detail"])
        after = client.get(f"/api/tasks/{tid}").json()
        self.assertIsNone(row_by_seq(after, 4)["pinned"])
        self.assertEqual(after["revision"], st["revision"])  # 未递增

    def test_timezone_change_clears_selections(self):
        st = upload(FALLBACK)
        tid = st["id"]
        r = client.post(f"/api/tasks/{tid}/selections",
                        json={"row_id": row_by_seq(st, 2)["id"],
                              "utc": "2024-11-03T05:00:00+00:00",
                              "revision": st["revision"]})
        st = r.json()
        self.assertIsNotNone(row_by_seq(st, 2)["pinned"])
        # 换时区：清空旧选择，修订递增
        r = client.post(f"/api/tasks/{tid}/timezone",
                        json={"timezone": "UTC", "revision": st["revision"]})
        self.assertEqual(r.status_code, 200, r.text)
        st = r.json()
        self.assertTrue(all(row["pinned"] is None for row in st["rows"]))
        self.assertEqual(st["status"], "resolved")  # UTC 下无歧义
        # 换时区前的修订已过期
        stale = client.post(f"/api/tasks/{tid}/timezone",
                            json={"timezone": "Asia/Shanghai",
                                  "revision": st["revision"] - 1})
        self.assertEqual(stale.status_code, 409)


class GapAndValidationTest(unittest.TestCase):
    def test_gap_rows_error_and_export_forbidden(self):
        st = upload(GAP)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["error_count"], 1)
        self.assertEqual(row_by_seq(st, 2)["status"], "error")
        resp = client.get(f"/api/tasks/{st['id']}/export",
                          params={"revision": st["revision"]})
        self.assertEqual(resp.status_code, 422)
        # 换到 UTC 后 02:30 存在 -> 可导出
        r = client.post(f"/api/tasks/{st['id']}/timezone",
                        json={"timezone": "UTC", "revision": st["revision"]})
        st = r.json()
        self.assertEqual(st["status"], "resolved")
        resp = client.get(f"/api/tasks/{st['id']}/export",
                          params={"revision": st["revision"]})
        self.assertEqual(resp.status_code, 200)

    def test_unsolvable_forbidden(self):
        dup = "采集序号,本地时间,PM2.5\n1,2024-11-03 00:59:00,1\n2,2024-11-03 00:59:00,2\n"
        st = upload(dup)
        self.assertEqual(st["status"], "unsolvable")
        resp = client.get(f"/api/tasks/{st['id']}/export",
                          params={"revision": st["revision"]})
        self.assertEqual(resp.status_code, 422)

    def test_upload_validation(self):
        too_many = "采集序号,本地时间,PM2.5\n" + "".join(
            f"{i},2024-11-03 03:{i % 60:02d}:00,1\n" for i in range(1, 502))
        resp = client.post("/api/tasks",
                           files={"file": ("a.csv", too_many.encode(), "text/csv")},
                           data={"timezone": "UTC"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("500", resp.json()["detail"])

        dup_seq = "采集序号,本地时间,PM2.5\n1,2024-11-03 03:00:00,1\n1,2024-11-03 03:01:00,2\n"
        resp = client.post("/api/tasks",
                           files={"file": ("a.csv", dup_seq.encode(), "text/csv")},
                           data={"timezone": "UTC"})
        self.assertEqual(resp.status_code, 400)

        resp = client.post("/api/tasks",
                           files={"file": ("a.csv", FALLBACK.encode(), "text/csv")},
                           data={"timezone": "Mars/Olympus"})
        self.assertEqual(resp.status_code, 400)

        missing_col = "a,b,c\n1,2,3\n"
        resp = client.post("/api/tasks",
                           files={"file": ("a.csv", missing_col.encode(), "text/csv")},
                           data={"timezone": "UTC"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
