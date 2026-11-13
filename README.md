# 记录仪本地时间 → UTC 校时服务

空气质量数仓场景：旧记录仪导出的本地时间不带 UTC 偏移，直接入库会污染暴露分析的时段对齐。本服务把本地时间还原为 UTC，并处理夏令时的两类陷阱：

- **跳时（春季拨快）**：本地时刻不存在 → 行级报错，禁止导出；
- **回拨（秋季拨慢）**：本地时刻对应两个 UTC → 列出全部候选，按"整份记录 UTC 严格递增"约束自动过滤，只保留能参与完整解释的候选；仍有歧义时人工选择并联动重算。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

浏览器中即可：上传 CSV → 选择 IANA 时区 → 消歧（如有回拨歧义）→ 下载含原值和 UTC 的 CSV。

页面内置两个样例按钮（也可直接在 `samples/` 找到）：

| 样例 | 场景 | 时区 |
|---|---|---|
| `samples/fallback.csv` | 2024-11-03 美国回拨，01:00–01:59 出现两次 | `America/New_York` |
| `samples/spring_forward.csv` | 2024-03-10 美国跳时，02:00–02:59 不存在 | `America/New_York` |

回拨样例中文件行序与采集序号故意不一致（seq 3、4 交换），演示"序号代表真实采集顺序、导出保留原行"。跳时样例上传后报错，切换时区到 `UTC` 可观察时刻变为合法并解锁导出。

## CSV 格式

```csv
采集序号,本地时间,PM2.5
1,2024-11-03 00:59:00,12.4
```

- 最多 **500** 行；表头支持别名：`seq/序号`、`local_time`、`pm25` 等；
- 本地时间秒级：`YYYY-MM-DD HH:MM:SS`；采集序号必须唯一（代表真实采集顺序）；
- 导出列：`采集序号,本地时间,PM2.5,UTC时间`（UTC 为 `YYYY-MM-DDTHH:MM:SSZ`，UTF-8 带 BOM，保持原行顺序）。

## 规则与并发模型

- **严格递增约束**：按采集序号排序后，整份记录的 UTC 必须严格递增；每个候选都经过前向/后向可达性过滤，只保留能参与完整解释的候选。
- **人工消歧**：选择某候选后立即联动重算其余行的可行候选；若选择会导致无解（矛盾选择），服务端拒绝并保留原选择。
- **乐观修订**：任务每次变更 `revision` 递增；所有修改与导出都必须携带所见修订，过期返回 `409`（共享任务链接即任务页 URL，`#任务ID`）。
- **换时区**：重新定位全部行并清空所有人工选择。
- **导出闸门**：存在跳时错误、无解或仍有歧义时一律禁止导出（`422`）；旧修订导出被服务端拒绝（`409`）。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/tasks` | multipart 上传 `file` + `timezone`，创建任务 |
| `GET` | `/api/tasks/{id}` | 任务当前状态（含 revision、每行候选） |
| `POST` | `/api/tasks/{id}/selections` | `{row_id, utc, revision}` 人工选择候选 |
| `POST` | `/api/tasks/{id}/clear-selection` | `{row_id, revision}` 清除选择 |
| `POST` | `/api/tasks/{id}/timezone` | `{timezone, revision}` 换时区并清空选择 |
| `GET` | `/api/tasks/{id}/export?revision=N` | 全部唯一确定后导出 CSV |
| `GET` | `/api/timezones` | IANA 时区列表 |

## 本地开发与测试

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # http://localhost:8000
python3 -m unittest discover -s tests  # 求解器 + 端到端 API 测试
```

技术栈：FastAPI + SQLite（任务/行持久化，修订号乐观并发）+ zoneinfo（PEP 495 fold 往返校验识别跳时/回拨）。
