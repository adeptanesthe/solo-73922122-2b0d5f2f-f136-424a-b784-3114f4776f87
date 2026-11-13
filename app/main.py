"""记录仪本地时间 -> UTC 校时服务（FastAPI + SQLite + zoneinfo）。"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Database
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


def build_state(task: dict, rows_db: list[dict]) -> dict:
    tz = ZoneInfo(task["timezone"])
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


def load_state(task_id: str) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return build_state(task, db.get_rows(task_id))


def check_revision(task: dict, revision: int) -> None:
    if revision != task["revision"]:
        raise HTTPException(
            409,
            f"修订过期：所见修订为 {revision}，当前修订为 {task['revision']}，请刷新后重试",
        )


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
    with db.lock:
        task = db.create_task(tz, rows)
    return load_state(task["id"])


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    return load_state(task_id)


@app.post("/api/tasks/{task_id}/timezone")
def change_timezone(task_id: str, body: TimezoneIn) -> dict:
    tz = parse_timezone(body.timezone)
    with db.lock:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        check_revision(task, body.revision)
        db.set_timezone(task_id, tz)  # 换时区清空旧选择并递增修订
    return load_state(task_id)


@app.post("/api/tasks/{task_id}/selections")
def select_candidate(task_id: str, body: SelectionIn) -> dict:
    with db.lock:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        check_revision(task, body.revision)
        row_db = db.get_row(task_id, body.row_id)
        if not row_db:
            raise HTTPException(404, "行不存在")
        try:
            chosen = datetime.fromisoformat(body.utc)
        except ValueError:
            raise HTTPException(422, "UTC 候选格式非法")

        tz = ZoneInfo(task["timezone"])
        row = Row(
            id=row_db["id"],
            row_index=row_db["row_index"],
            seq=row_db["seq"],
            local_raw=row_db["local_time"],
            pm25_raw=row_db["pm25"],
        )
        row.compute_candidates(tz)
        if chosen not in row.candidates:
            raise HTTPException(409, "该候选不属于此行的合法 UTC 候选，已保留原选择")

        # 先记下旧选择，矛盾时回滚
        old_pin = row_db["pinned_utc"]
        db.set_pin(task_id, body.row_id, chosen.isoformat())
        rows_now = db.get_rows(task_id)
        solver_rows = []
        for r in rows_now:
            sr = Row(
                id=r["id"],
                row_index=r["row_index"],
                seq=r["seq"],
                local_raw=r["local_time"],
                pm25_raw=r["pm25"],
                pinned_utc=(
                    datetime.fromisoformat(r["pinned_utc"]) if r["pinned_utc"] else None
                ),
            )
            sr.compute_candidates(tz)
            solver_rows.append(sr)
        _, solvable = filter_viable(sorted(solver_rows, key=lambda r: r.seq))
        if not solvable:
            db.set_pin(task_id, body.row_id, old_pin)  # 保留原选择
            raise HTTPException(
                409, "矛盾选择：与整份记录 UTC 严格递增约束冲突，已保留原选择"
            )
        db.bump_revision(task_id)
    return load_state(task_id)


@app.post("/api/tasks/{task_id}/clear-selection")
def clear_selection(task_id: str, body: ClearIn) -> dict:
    with db.lock:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        check_revision(task, body.revision)
        if not db.get_row(task_id, body.row_id):
            raise HTTPException(404, "行不存在")
        db.set_pin(task_id, body.row_id, None)
        db.bump_revision(task_id)
    return load_state(task_id)


@app.get("/api/tasks/{task_id}/export")
def export_csv(task_id: str, revision: int = Query(...)) -> Response:
    with db.lock:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        check_revision(task, revision)  # 拒绝旧修订导出
        state = build_state(task, db.get_rows(task_id))
        if state["status"] != "resolved":
            raise HTTPException(
                422, "存在无解或歧义行，全部唯一确定前禁止导出"
            )

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["采集序号", "本地时间", "PM2.5", "UTC时间"])
    for r in state["rows"]:  # 保留原行顺序
        utc = datetime.fromisoformat(r["resolved_utc"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        writer.writerow([r["seq"], r["local_time"], r["pm25"], utc])
    payload = "\ufeff" + buf.getvalue()  # BOM，便于 Excel 正确识别 UTF-8
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="task_{task_id}_utc.csv"'
        },
    )


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.mount("/samples", StaticFiles(directory=ROOT_DIR / "samples"), name="samples")
