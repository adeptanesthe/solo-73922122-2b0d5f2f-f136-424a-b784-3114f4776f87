# 记录仪本地时间 → UTC 校时服务

空气质量数仓场景：旧记录仪导出的本地时间不带 UTC 偏移，直接入库会污染暴露分析的时段对齐。本服务把本地时间还原为 UTC，并处理夏令时的两类陷阱：

- **跳时（春季拨快）**：本地时刻不存在 → 行级报错，禁止导出；
- **回拨（秋季拨慢）**：本地时刻对应两个 UTC → 列出全部候选，按"整份记录 UTC 严格递增"约束自动过滤，只保留能参与完整解释的候选；仍有歧义时人工选择并联动重算。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000（进程 A）或 http://localhost:8001（进程 B）
```

两个 Web 进程（`web-a` / `web-b`）**共用同一个 SQLite 库卷**（`/data/app.db`），用于验证跨进程并发语义。浏览器中可以完成全部业务操作：

上传 CSV → 选择 IANA 时区 → 消歧（如有回拨歧义）→ 下载含原值和 UTC 的 CSV。

页面内置两个样例按钮（也可直接在 `samples/` 找到）：

| 样例 | 场景 | 时区 |
|---|---|---|
| `samples/fallback.csv` | 2024-11-03 美国回拨，01:00–01:59 出现两次 | `America/New_York` |
| `samples/spring_forward.csv` | 2024-03-10 美国跳时，02:00–02:59 不存在 | `America/New_York` |

回拨样例中文件行序与采集序号故意不一致（seq 3、4 交换），演示"序号代表真实采集顺序、导出保留原行"。跳时样例上传后报错，切换时区到 `UTC` 可观察时刻变为合法并解锁导出。

## 并发与一致性模型

所有并发控制都落在共享的 SQLite 库上，不依赖任何进程内锁：

| 关注点 | 做法 |
|---|---|
| 读响应不拼接 | 任务与全部行在同一 SQLite **读事务快照**中读取；revision、时区、人工选择、候选都由这一份快照算出，查询途中别处换时区也不会混入 |
| 修改不互相覆盖 | 读快照 → **不持写锁**重算候选与可行性 → `BEGIN IMMEDIATE` 事务内**重新读取并核对**修订号、时区、每行 pin 与原始数据，一致才应用修改并递增修订号；不一致返回 `409`，不写入 |
| 同一修订最多一份成功 | 比对并交换（CAS）在库级写事务内完成；两个共库进程基于同一修订提交时，后提交者核对失败 → `409` |
| 选择 / 清空 / 换时区原子生效 | 三种修改各自与修订号递增在**同一事务**内提交，要么都生效要么都不生效 |
| 矛盾或写入失败保留原状态 | 矛盾在选择前用快照试算拦截（不写库）；事务内任何异常整体 `ROLLBACK` |
| 保存中退出 | 写操作与修订号同事务：COMMIT 前进程退出，SQLite 回滚 → 旧版保留；COMMIT 返回后退出，WAL + `synchronous=FULL` → 新版已持久 |
| 导出闸门 | 用请求携带的修订号比对同一快照；旧修订 `409`；存在跳时错误、无解或仍有歧义 `422`；只有完整核准的修订能导出 |

## 可控演示

正常运行（`docker compose up`）页面底部会显示「并发演示」面板，四个按钮全部通过真实 HTTP 请求驱动两个共库进程，并在日志区给出可核对的时序：

1. **并发选择（同一修订）**：A、B 同时提交不同选择 → 一份 `200`，另一份 `409`；
2. **保存中退出（COMMIT 前）**：向 B 提交后在 COMMIT 前退出进程 → 重启后修订未变、选择未变，旧修订仍不可导出；
3. **保存中退出（COMMIT 后）**：COMMIT 返回后、响应前退出 → 重启后新版仍在；
4. **查询中换时区**：A 读到任务后暂停，B 换时区 → A 的响应整体仍是旧快照，不出现拼接。

也提供命令行版本（同样打到两个共库进程）：

```bash
docker compose run --rm demo                    # 全部场景
docker compose run --rm demo python -m demo.run --scenario race
```

演示钩子仅在 `APP_DEMO_HOOKS=1` 时启用（compose 已配置），日常部署不设置该变量即可完全关闭，且 `/api/demo/*` 会返回 404。

关闭演示钩子后的等价启动方式：

```bash
docker compose up --build            # 已含演示面板；去掉 APP_DEMO_HOOKS 即为纯业务模式
```

## CSV 格式

```csv
采集序号,本地时间,PM2.5
1,2024-11-03 00:59:00,12.4
```

- 最多 **500** 行；表头支持别名：`seq/序号`、`local_time`、`pm25` 等；
- 本地时间秒级：`YYYY-MM-DD HH:MM:SS`；采集序号必须唯一（代表真实采集顺序）；
- 导出列：`采集序号,本地时间,PM2.5,UTC时间`（UTC 为 `YYYY-MM-DDTHH:MM:SSZ`，UTF-8 带 BOM，保持原行顺序）。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/tasks` | multipart 上传 `file` + `timezone`，创建任务 |
| `GET` | `/api/tasks/{id}` | 任务当前状态（含 revision、时区、每行候选） |
| `POST` | `/api/tasks/{id}/selections` | `{row_id, utc, revision}` 人工选择候选 |
| `POST` | `/api/tasks/{id}/clear-selection` | `{row_id, revision}` 清除选择 |
| `POST` | `/api/tasks/{id}/timezone` | `{timezone, revision}` 换时区并清空选择 |
| `GET` | `/api/tasks/{id}/export?revision=N` | 全部唯一确定后导出 CSV |
| `GET` | `/api/timezones` | IANA 时区列表 |

错误语义：`409` 修订过期/矛盾选择（原状态保留）；`422` 当前修订不可导出；`400` 上传或时区非法；`404` 任务或行不存在；`503` 写事务失败（本次修改未生效）。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload                       # http://localhost:8000
python3 -m unittest discover -s tests               # 单元 + API + 多进程端到端
```

测试构成：

- `tests/test_solver.py`：候选枚举与严格递增过滤；
- `tests/test_api.py`：上传、消歧、修订冲突、导出闸门（单进程 TestClient）；
- `tests/test_concurrency.py`：两个独立连接（模拟两个进程）验证快照一致性与提交回滚；
- `tests/test_multiprocess.py`：启动两个真实 uvicorn 进程共库，验证并发选择 200/409、COMMIT 前后退出、查询中换时区、导出只认对应修订。

`tests/_server.py` 是测试用的 uvicorn 启动器，用哨兵文件触发优雅退出（某些沙箱禁止向子进程发信号）。

技术栈：FastAPI + SQLite（WAL 跨进程事务，修订号乐观并发）+ zoneinfo（PEP 495 fold 往返校验识别跳时/回拨）。
