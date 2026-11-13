"""校时核心逻辑：本地时刻 -> UTC 候选、严格递增约束过滤。

规则：
- 本地时刻不存在（春季跳时空洞）-> 0 个候选，行报错；
- 回拨重叠小时 -> 2 个 UTC 候选；
- 普通时刻 -> 1 个候选；
- 整份记录按采集序号排序后，UTC 必须严格递增；
  只有能参与"完整解释"的候选才被保留（前向/后向可达性 DP）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def parse_local_time(raw: str) -> datetime:
    """解析秒级本地时间（naive），失败抛 ValueError。"""
    text = raw.strip()
    for fmt in LOCAL_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析本地时间 {raw!r}，需要格式 YYYY-MM-DD HH:MM:SS")


def utc_candidates(local_naive: datetime, tz: ZoneInfo) -> list[datetime]:
    """返回映射到该本地时刻的全部 UTC 时刻（升序、去重）。

    利用 PEP 495 fold 与往返校验：
    - 空洞时刻两个 fold 往返后都落不到原本地时刻 -> 0 个候选；
    - 重叠时刻两个 fold 都合法且 UTC 不同 -> 2 个候选。
    """
    found: set[datetime] = set()
    for fold in (0, 1):
        aware = local_naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == local_naive:
            found.add(utc)
    return sorted(found)


@dataclass
class Row:
    """一行记录。pinned_utc 为人工选定的候选（None 表示未选择）。"""

    id: int
    row_index: int  # 文件中的原始行序（保留原行）
    seq: int  # 采集序号，代表真实采集顺序
    local_raw: str
    pm25_raw: str
    pinned_utc: datetime | None = None
    local_naive: datetime = field(init=False)
    candidates: list[datetime] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.local_naive = parse_local_time(self.local_raw)

    def compute_candidates(self, tz: ZoneInfo) -> None:
        self.candidates = utc_candidates(self.local_naive, tz)


def filter_viable(rows_by_seq: list[Row]) -> tuple[list[list[datetime]], bool]:
    """按 UTC 严格递增约束过滤候选。

    入参必须已按采集序号升序排列。返回与入参对齐的每行可行候选列表，
    以及是否存在完整解释。人工选定（pin）的行只保留其选定值。
    """
    n = len(rows_by_seq)
    allowed: list[list[datetime]] = []
    for r in rows_by_seq:
        if r.pinned_utc is not None:
            allowed.append([r.pinned_utc] if r.pinned_utc in r.candidates else [])
        else:
            allowed.append(list(r.candidates))

    fwd = [set() for _ in range(n)]
    if n:
        fwd[0] = set(allowed[0])
        for i in range(1, n):
            prev = fwd[i - 1]
            fwd[i] = {c for c in allowed[i] if any(p < c for p in prev)}

    bwd = [set() for _ in range(n)]
    if n:
        bwd[-1] = set(allowed[-1])
        for i in range(n - 2, -1, -1):
            nxt = bwd[i + 1]
            bwd[i] = {c for c in allowed[i] if any(c < s for s in nxt)}

    viable = [sorted(fwd[i] & bwd[i]) for i in range(n)]
    solvable = n > 0 and all(viable)
    return viable, solvable
