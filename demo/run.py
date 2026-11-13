"""可控并发演示：对两个共库 Web 进程发真实 HTTP 请求。

用法（容器内）：
    docker compose run --rm demo
    docker compose run --rm demo python -m demo.run --scenario race

每个场景都输出关键证据（状态码、修订号、最终库内状态），可用 ``--json``
拿到机器可读结果。
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

SAMPLE = (
    "采集序号,本地时间,PM2.5\n"
    "1,2024-11-03 00:59:00,12.4\n"
    "2,2024-11-03 01:00:00,13.1\n"
    "4,2024-11-03 01:59:59,14.0\n"
    "3,2024-11-03 01:30:00,13.7\n"
    "5,2024-11-03 02:00:00,15.2\n"
    "6,2024-11-03 02:30:00,16.8\n"
)


def call(method: str, url: str, body=None, form=None, timeout=30):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in form.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="data.csv"\r\nContent-Type: text/csv\r\n\r\n{SAMPLE}\r\n'
        )
        data = ("".join(parts) + f"--{boundary}--\r\n").encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, ConnectionError, http.client.RemoteDisconnected):
        # 进程在演示钩子处退出时连接会被切断，属预期行为。
        return 0, b""


def jcall(method, url, **kw):
    status, raw = call(method, url, **kw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw.decode("utf-8", "replace")
    return status, payload


def upload(base):
    status, st = jcall("POST", base + "/api/tasks", form={"timezone": "America/New_York"})
    if status != 201:
        raise SystemExit(f"上传失败 {status}: {st}")
    return st


def row(st, seq):
    return next(r for r in st["rows"] if r["seq"] == seq)


def hook(base, mode):
    status, payload = jcall("POST", base + "/api/demo/hooks", body={"mode": mode})
    if status != 200:
        raise SystemExit(f"演示钩子不可用（需 APP_DEMO_HOOKS=1）{status}: {payload}")


def summary(st):
    return {
        "revision": st["revision"],
        "timezone": st["timezone"],
        "status": st["status"],
        "pinned": {r["seq"]: r["pinned"] for r in st["rows"] if r["pinned"]},
    }


def wait_ready(base: str, seconds: float = 20.0) -> None:
    """等待被演示钩子退出的进程被重启（compose restart 策略）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status, _ = call("GET", base + "/api/timezones", timeout=2)
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit(f"进程未在 {seconds:.0f}s 内恢复：{base}")


# ---------- 场景 ----------

def scenario_race(a: str, b: str) -> dict:
    """两个进程基于同一修订各选一份修改：最多一份成功，其余 409。"""
    print("\n=== 场景 1：并发选择（同一修订，两个共库进程） ===")
    st = upload(a)
    tid, rev = st["id"], st["revision"]
    rid2, rid3 = row(st, 2)["id"], row(st, 3)["id"]
    hook(a, "race")
    hook(b, "race")  # 两个进程都在读完快照后等一拍，确保竞态可控
    results = {}

    def pick(name, base, rid, utc):
        status, payload = jcall(
            "POST", f"{base}/api/tasks/{tid}/selections",
            body={"row_id": rid, "utc": utc, "revision": rev},
        )
        results[name] = (status, payload if status != 200 else summary(payload))

    t1 = threading.Thread(target=pick, args=(
        "A(seq2 fold0)", a, rid2, "2024-11-03T05:00:00+00:00"))
    t2 = threading.Thread(target=pick, args=(
        "B(seq3 fold1)", b, rid3, "2024-11-03T06:30:00+00:00"))
    t1.start(); t2.start(); t1.join(); t2.join()
    hook(a, "")
    hook(b, "")

    ok = [k for k, (s, _) in results.items() if s == 200]
    conflict = [k for k, (s, _) in results.items() if s == 409]
    for k, v in results.items():
        print(f"  {k}: {v[0]} {v[1]}")
    _, final = jcall("GET", f"{a}/api/tasks/{tid}")
    print(f"  库内最终状态: {summary(final)}")
    return {
        "scenario": "race", "task": tid, "success": ok, "conflict": conflict,
        "final": summary(final), "passed": len(ok) <= 1,
    }


def scenario_crash_before(a: str, b: str) -> dict:
    """修改写入后、COMMIT 前退出：选择未变、修订未变、旧修订不导出新 UTC。"""
    print("\n=== 场景 2a：保存中退出（COMMIT 前） ===")
    st = upload(a)
    tid, rev = st["id"], st["revision"]
    rid2 = row(st, 2)["id"]
    hook(b, "before-commit")
    status, _ = call("POST", f"{b}/api/tasks/{tid}/selections", body={
        "row_id": rid2, "utc": "2024-11-03T05:00:00+00:00", "revision": rev})
    print(f"  进程 B 响应: {status}（连接被进程退出切断属预期）")
    wait_ready(b)
    hook(b, "")
    _, after = jcall("GET", f"{a}/api/tasks/{tid}")
    print(f"  重启后状态: {summary(after)}")
    exp_status, _ = call("GET", f"{a}/api/tasks/{tid}/export?revision={rev}")
    print(f"  旧修订导出: {exp_status}（应 422：仍是歧义，没有新 UTC）")
    passed = (after["revision"] == rev and row(after, 2)["pinned"] is None
              and exp_status == 422)
    return {"scenario": "crash-before-commit", "task": tid, "after": summary(after),
            "export_status": exp_status, "passed": passed}


def scenario_crash_after(a: str, b: str) -> dict:
    """COMMIT 后、响应前退出：重启后新版仍在。"""
    print("\n=== 场景 2b：保存中退出（COMMIT 后） ===")
    st = upload(a)
    tid, rev = st["id"], st["revision"]
    rid2 = row(st, 2)["id"]
    hook(b, "after-commit")
    status, _ = call("POST", f"{b}/api/tasks/{tid}/selections", body={
        "row_id": rid2, "utc": "2024-11-03T05:00:00+00:00", "revision": rev})
    print(f"  进程 B 响应: {status}（连接被进程退出切断属预期）")
    wait_ready(b)
    hook(b, "")
    _, after = jcall("GET", f"{a}/api/tasks/{tid}")
    print(f"  重启后状态: {summary(after)}")
    passed = after["revision"] == rev + 1 and row(after, 2)["pinned"] is not None
    return {"scenario": "crash-after-commit", "task": tid, "after": summary(after),
            "passed": passed}


def scenario_read_pause(a: str, b: str) -> dict:
    """查询读到任务后暂停，期间另一进程换时区：响应仍是同一份快照。"""
    print("\n=== 场景 3：查询中换时区（快照不拼接） ===")
    st = upload(a)
    tid, rev = st["id"], st["revision"]
    rid2 = row(st, 2)["id"]
    status, st = jcall("POST", f"{a}/api/tasks/{tid}/selections",
                       body={"row_id": rid2, "utc": "2024-11-03T05:00:00+00:00",
                             "revision": rev})
    rev = st["revision"]
    hook(a, "read")
    out = {}

    def read_task():
        out["task"] = jcall("GET", f"{a}/api/tasks/{tid}")

    t = threading.Thread(target=read_task)
    t.start()
    time.sleep(0.2)  # 让 GET 进入"已读任务、未读行"的窗口
    tz_status, tz_state = jcall("POST", f"{b}/api/tasks/{tid}/timezone",
                                body={"timezone": "Asia/Shanghai", "revision": rev})
    t.join()
    hook(a, "")
    get_status, got = out["task"]
    print(f"  换时区: {tz_status} {summary(tz_state)}")
    print(f"  并发查询: {get_status} {summary(got)}")
    consistent = (get_status == 200 and got["revision"] == rev
                  and got["timezone"] == "America/New_York"
                  and all(r["pinned"] is not None for r in got["rows"] if r["seq"] == 2))
    _, latest = jcall("GET", f"{a}/api/tasks/{tid}")
    print(f"  库内最新: {summary(latest)}")
    return {"scenario": "read-timezone-race", "task": tid,
            "concurrent_read": summary(got), "timezone_change": summary(tz_state),
            "latest": summary(latest), "passed": consistent}


SCENARIOS = {
    "race": scenario_race,
    "crash-before": scenario_crash_before,
    "crash-after": scenario_crash_after,
    "read-race": scenario_read_pause,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-a", default="http://localhost:8000")
    ap.add_argument("--base-b", default="http://localhost:8001")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    wait_ready(args.base_a)
    wait_ready(args.base_b)
    names = sorted(SCENARIOS) if args.all or not args.scenario else args.scenario
    results = [SCENARIOS[n](args.base_a, args.base_b) for n in names]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    passed = all(r["passed"] for r in results)
    print(f"\n结果：{'全部通过' if passed else '存在未通过场景'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
