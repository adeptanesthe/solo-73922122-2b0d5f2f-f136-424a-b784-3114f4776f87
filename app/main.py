"""记录仪本地时间 -> UTC 校时服务（FastAPI + SQLite + zoneinfo）。

多进程共享同一 SQLite 库时的关键约束：

- 所有读响应（查询/修改/导出）都基于 ``read_state`` 返回的同一份快照，
  revision / timezone / 人工选择 / 候选不会拼接两个已提交状态。
- 所有写操作先在不持写锁的情况下读快照、算候选与可行性，再在
  ``BEGIN IMMEDIATE`` 事务里核对所见状态；不一致返回 409，不覆盖后来修改。
  重算全程不持 SQLite 写锁。
- 选择、清空选择、换时区三种修改各自原子生效（与 revision 递增同事务），
  失败或矛盾时整事务回滚，保留原状态。
"""
from __future__ import annotations

import csv
import io
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Conflict, Database, TaskGone
from .solver import Row, filter_viable, parse_local_time

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
MAX_ROWS = 500
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

SEQ_HEADERS = {"采集序号", "seq", "序号"}
LOCAL_HEADERS = {"本地时间", "local_time", "本地秒级时间", "采集时间"}
PM_HEADERS = {"pm2.5", "pm25", "PM2.5", "PM2.5值"}

app = FastAPI(title="记录仪本地时间校时服务")
db = Database(os.environ.get("APP_DB", str(ROOT_DIR / "data" / "app.db")))

# 演示环境里浏览器需要直接请求另一个共库进程（不同端口），因此仅在
# APP_DEMO_HOOKS=1 时开放 CORS；正常部署不启用。
if os.environ.get("APP_DEMO_HOOKS", "").strip().lower() in {"1", "true", "yes"}:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------- 演示钩子（仅演示容器启用，日常运行完全不触发） ----------

HOOK_LOCK = threading.Lock()
HOOKS: dict[str, str] = {}


def demo_enabled() -> bool:
    return os.environ.get("APP_DEMO_HOOKS", "").strip().lower() in {"1", "true", "yes"}


def _crash_before_commit() -> None:
    """在写操作已落库、COMMIT 之前退出进程：SQLite 回滚整个事务。"""
    if demo_enabled() and HOOKS.get("crash") == "before-commit":
        os._exit(70)


def _crash_after_commit() -> None:
    """在 COMMIT 返回之后、响应写出之前退出进程：新版已持久化。"""
    if demo_enabled() and HOOKS.get("crash") == "after-commit":
        os._exit(71)


def _pause_after_task_read(hold_seconds: float):
    """返回读快照钩子：读完任务后暂停，期间别处换时区也不会污染本响应。"""

    def hook() -> None:
        if not demo_enabled():
            return
        with HOOK_LOCK:
            mode = HOOKS.get("pause", "")
        if mode == "read":
            threading.Event().wait(hold_seconds)

    return hook


def _race_pause(hold_seconds: float) -> None:
    """修改请求读完快照后、提交前暂停，让两个请求都基于同一修订。

    暂停期间不持有写锁；随后各自提交，只有一份能通过提交时的状态核对。
    """
    if not demo_enabled():
        return
    with HOOK_LOCK:
        mode = HOOKS.get("pause", "")
    if mode == "race":
        threading.Event().wait(hold_seconds)


# ---------- 状态构建 ----------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _candidate_payload(utc: datetime, tz: ZoneInfo) -> dict:
    local = utc.astimezone(tz)
    off = local.utcoffset()
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total) // 60, 60)
    return {
        "utc": _iso(utc),
        "label": f"{local.tzname()} (UTC{sign}{hh:02d}:{mm:02d})",
    }


def _row_objects(rows_db: list[dict], tz: ZoneInfo) -> list[Row]:
    rows: list[Row] = []
    for r in rows_db:
        row = Row(
            id=r["id"],
            row_index=r["row_index"],
            seq=r["seq"],
            local_raw=r["local_time"],
            pm25_raw=r["pm25"],
            pinned_utc=(
                datetime.fromisoformat(r["pinned_utc"]) if r["pinned_utc"] else None
            ),
        )
        row.compute_candidates(tz)
        rows.append(row)
    return rows


def build_state(task: dict, rows_db: list[dict]) -> dict:
    """由一份快照构建完整状态；耗时重算全部在事务之外完成。"""
    tz = ZoneInfo(task["timezone"])
    rows = _row_objects(rows_db, tz)

    by_seq = sorted(rows, key=lambda r: r.seq)
    viable_lists, solvable = filter_viable(by_seq)
    viable_by_id = {r.id: v for r, v in zip(by_seq, viable_lists)}

    has_gap = any(len(r.candidates) == 0 for r in rows)
    ambiguous_count = 0
    row_payloads = []
    for r in rows:  # 保留原行顺序输出
        viable = viable_by_id[r.id]
        if len(r.candidates) == 0:
            status = "error"
        elif len(viable) == 1:
            status = "resolved"
        else:
            status = "ambiguous"
            ambiguous_count += 1
        row_payloads.append(
            {
                "id": r.id,
                "row_index": r.row_index,
                "seq": r.seq,
                "local_time": r.local_raw,
                "pm25": r.pm25_raw,
                "status": status,
                "candidates": [_candidate_payload(c, tz) for c in r.candidates],
                "viable": [_candidate_payload(c, tz) for c in viable],
                "pinned": _iso(r.pinned_utc) if r.pinned_utc else None,
                "resolved_utc": _iso(viable[0]) if len(viable) == 1 else None,
            }
        )

    if has_gap:
        status = "error"
    elif not solvable:
        status = "unsolvable"
    elif ambiguous_count:
        status = "ambiguous"
    else:
        status = "resolved"

    return {
        "id": task["id"],
        "timezone": task["timezone"],
        "revision": task["revision"],
        "created_at": task["created_at"],
        "status": status,
        "row_count": len(rows),
        "ambiguous_count": ambiguous_count,
        "error_count": sum(1 for r in rows if len(r.candidates) == 0),
        "rows": row_payloads,
    }


def snapshot(task_id: str, pause_before_rows=None) -> tuple[dict, list[dict]]:
    """读取同一已提交状态的 (task, rows)；任务不存在抛 404。"""
    task, rows = db.read_state(task_id, pause_before_rows)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task, rows


def state_of(task_id: str, pause_before_rows=None) -> dict:
    task, rows = snapshot(task_id, pause_before_rows)
    return build_state(task, rows)


def _conflict(observed: int, current: int) -> HTTPException:
    return HTTPException(
        409,
        f"修订过期：所见修订为 {observed}，当前修订为 {current}，未应用任何修改，请刷新后重试",
    )


def check_revision(task: dict, revision: int) -> None:
    """快照上的快速检查；真正的门禁在提交事务内再次核对。"""
    if revision != task["revision"]:
        raise _conflict(revision, task["revision"])


def _commit_error(exc: Exception, revision: int) -> HTTPException:
    if isinstance(exc, Conflict):
        return HTTPException(
            409,
            f"修订过期：所见修订为 {revision}，提交时任务已被其他进程修改，"
            "未应用任何修改，请刷新后重试",
        )
    if isinstance(exc, TaskGone):
        return HTTPException(404, "任务不存在")
    return HTTPException(503, "写入失败，本次修改未生效，任务保持原状态，请稍后重试")


# ---------- CSV 解析 ----------

def _find_column(fieldnames: list[str], aliases: set[str]) -> str | None:
    lowered = {f.strip().lower(): f for f in fieldnames}
    for alias in aliases:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    return None


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV 缺少表头")
    seq_col = _find_column(reader.fieldnames, SEQ_HEADERS)
    local_col = _find_column(reader.fieldnames, LOCAL_HEADERS)
    pm_col = _find_column(reader.fieldnames, PM_HEADERS)
    missing = []
    if not seq_col:
        missing.append("采集序号")
    if not local_col:
        missing.append("本地时间")
    if not pm_col:
        missing.append("PM2.5")
    if missing:
        raise HTTPException(400, f"CSV 缺少必需列：{'、'.join(missing)}")

    records = list(reader)
    if not records:
        raise HTTPException(400, "CSV 没有数据行")
    if len(records) > MAX_ROWS:
        raise HTTPException(400, f"CSV 超过 {MAX_ROWS} 行上限（{len(records)} 行）")

    rows, seen_seq = [], set()
    for i, rec in enumerate(records):
        line = i + 2  # 含表头的行号
        seq_raw = (rec.get(seq_col) or "").strip()
        local_raw = (rec.get(local_col) or "").strip()
        pm_raw = (rec.get(pm_col) or "").strip()
        try:
            seq = int(seq_raw)
        except ValueError:
            raise HTTPException(400, f"第 {line} 行：采集序号 {seq_raw!r} 不是整数")
        if seq in seen_seq:
            raise HTTPException(400, f"第 {line} 行：采集序号 {seq} 重复")
        seen_seq.add(seq)
        try:
            parse_local_time(local_raw)
        except ValueError as exc:
            raise HTTPException(400, f"第 {line} 行：{exc}")
        try:
            float(pm_raw)
        except ValueError:
            raise HTTPException(400, f"第 {line} 行：PM2.5 值 {pm_raw!r} 不是数值")
        rows.append(
            {"row_index": i, "seq": seq, "local_time": local_raw, "pm25": pm_raw}
        )
    return rows


def parse_timezone(name: str) -> str:
    name = (name or "").strip()
    try:
        ZoneInfo(name)
    except Exception:
        raise HTTPException(400, f"未知 IANA 时区：{name!r}")
    return name


# ---------- 请求模型 ----------

class SelectionIn(BaseModel):
    row_id: int
    utc: str
    revision: int


class ClearIn(BaseModel):
    row_id: int
    revision: int


class TimezoneIn(BaseModel):
    timezone: str
    revision: int


# ---------- 路由 ----------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/api/timezones")
def timezones() -> list[str]:
    return sorted(available_timezones())


@app.post("/api/tasks", status_code=201)
async def create_task(file: UploadFile = File(...), timezone: str = Form(...)) -> dict:
    tz = parse_timezone(timezone)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "文件过大")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是有效的 UTF-8 文本")
    rows = parse_csv(text)
    task, rows_db = db.create_task(tz, rows)  # 建任务 + 建行在同一事务
    return build_state(task, rows_db)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    return state_of(task_id, pause_before_rows=_pause_after_task_read(0.6))


@app.post("/api/tasks/{task_id}/timezone")
def change_timezone(task_id: str, body: TimezoneIn) -> dict:
    tz = parse_timezone(body.timezone)
    task, rows_db = snapshot(task_id)
    check_revision(task, body.revision)
    _race_pause(0.6)
    try:
        new_task, new_rows = db.change_timezone(
            task_id, task, rows_db, tz, before_commit=_crash_before_commit
        )
    except Exception as exc:
        raise _commit_error(exc, body.revision)
    _crash_after_commit()  # 演示：COMMIT 之后、响应之前退出
    return build_state(new_task, new_rows)  # 只反映本次提交的状态


@app.post("/api/tasks/{task_id}/selections")
def select_candidate(task_id: str, body: SelectionIn) -> dict:
    task, rows_db = snapshot(task_id)
    check_revision(task, body.revision)
    _race_pause(0.6)

    try:
        chosen = datetime.fromisoformat(body.utc)
    except ValueError:
        raise HTTPException(422, "UTC 候选格式非法")
    if chosen.tzinfo is None:
        raise HTTPException(422, "UTC 候选必须带时区偏移")

    # 在快照上（不持写锁）校验候选并试算矛盾。
    tz = ZoneInfo(task["timezone"])
    rows = _row_objects(rows_db, tz)
    target = next((r for r in rows if r.id == body.row_id), None)
    if target is None:
        raise HTTPException(404, "行不存在")
    if chosen not in target.candidates:
        raise HTTPException(409, "该候选不属于此行的合法 UTC 候选，已保留原选择")

    target.pinned_utc = chosen
    _, solvable = filter_viable(sorted(rows, key=lambda r: r.seq))
    if not solvable:
        raise HTTPException(
            409, "矛盾选择：与整份记录 UTC 严格递增约束冲突，已保留原选择"
        )

    try:
        new_task, new_rows = db.set_selection(
            task_id, task, rows_db, body.row_id, chosen.isoformat(),
            before_commit=_crash_before_commit,
        )
    except Exception as exc:
        raise _commit_error(exc, body.revision)
    _crash_after_commit()  # 演示：COMMIT 之后、响应之前退出
    return build_state(new_task, new_rows)  # 只反映本次提交的状态


@app.post("/api/tasks/{task_id}/clear-selection")
def clear_selection(task_id: str, body: ClearIn) -> dict:
    task, rows_db = snapshot(task_id)
    check_revision(task, body.revision)
    _race_pause(0.6)
    if not any(r["id"] == body.row_id for r in rows_db):
        raise HTTPException(404, "行不存在")
    try:
        new_task, new_rows = db.clear_selection(
            task_id, task, rows_db, body.row_id, before_commit=_crash_before_commit
        )
    except Exception as exc:
        raise _commit_error(exc, body.revision)
    _crash_after_commit()  # 演示：COMMIT 之后、响应之前退出
    return build_state(new_task, new_rows)  # 只反映本次提交的状态


@app.get("/api/tasks/{task_id}/export")
def export_csv(task_id: str, revision: int = Query(...)) -> Response:
    # 查询与导出共用一份快照：revision、时区、选择、候选都来自同一次提交。
    task, rows_db = snapshot(task_id, pause_before_rows=_pause_after_task_read(0.6))
    check_revision(task, revision)  # 拒绝旧修订导出
    state = build_state(task, rows_db)
    if state["status"] != "resolved":
        raise HTTPException(
            422, "该修订未全部核准：存在跳时错误、无解或歧义行，禁止导出"
        )

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["采集序号", "本地时间", "PM2.5", "UTC时间"])
    for r in state["rows"]:  # 保留原行顺序
        utc = datetime.fromisoformat(r["resolved_utc"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        writer.writerow([r["seq"], r["local_time"], r["pm25"], utc])
    payload = "﻿" + buf.getvalue()  # BOM，便于 Excel 正确识别 UTF-8
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="task_{task_id}_utc.csv"'
        },
    )


# ---------- 演示控制（仅演示容器可调用） ----------

class HookIn(BaseModel):
    mode: str = ""


@app.get("/api/demo/info")
def demo_info() -> dict:
    """浏览器演示面板用：返回另一个共库进程的地址（需 APP_DEMO_HOOKS=1）。"""
    if not demo_enabled():
        raise HTTPException(404, "未找到")
    return {
        "enabled": True,
        "self": os.environ.get("APP_SELF_URL", ""),
        "peer": os.environ.get("APP_PEER_URL", ""),
        "hooks": dict(HOOKS),
    }


@app.post("/api/demo/hooks")
def set_demo_hook(body: HookIn) -> dict:
    if not demo_enabled():
        raise HTTPException(404, "未找到")
    if body.mode not in {"", "read", "race", "before-commit", "after-commit"}:
        raise HTTPException(400, "未知演示模式")
    with HOOK_LOCK:
        if body.mode == "":
            HOOKS.pop("pause", None)
            HOOKS.pop("crash", None)
        elif body.mode in {"read", "race"}:
            HOOKS["pause"] = body.mode
        else:
            HOOKS["crash"] = body.mode
    return {"hooks": dict(HOOKS), "enabled": True}


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.mount("/samples", StaticFiles(directory=ROOT_DIR / "samples"), name="samples")
