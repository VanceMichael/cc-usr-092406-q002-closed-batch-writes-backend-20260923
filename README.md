# q002 水产养殖服务

本项目是水产养殖管理后端，维护塘口、养殖批次、投苗、投喂、水质、用药、成本、销售与周期分析数据。业务数据保存在 SQLite 文件中，HTTP 接口由 FastAPI 提供。

## 批次生命周期与写入一致性

批次状态机：`active` → `harvested` → `closed`。所有业务记录（投苗、投喂、水质、用药、成本、销售）的新增、修改、删除都按同一套规则校验：

- **批次状态**：已关闭（`closed`）批次禁止直接写入，返回 409，必须走更正流程；
- **投苗日**：业务发生日不得早于批次投苗日；
- **实际收获日 + 补录窗口**：已登记实际收获日的批次，业务发生日最晚不超过收获日 + `BACKFILL_WINDOW_DAYS` 天（默认 7，可用环境变量调整）——这是**关闭前允许的补录窗口**；
- **关闭后的更正流程**：通过 `POST /api/batches/{id}/corrections` 提交更正（create/update/delete），进入复核队列，裁定通过后才生效。

### 批次关闭（事务 + 幂等）

`POST /api/batches/{id}/close`（请求体可含 `request_id`、`closed_by`、`note`）在**同一事务**内完成：翻转批次状态、确认未决写入（把历史越界记录隔离进复核队列）、冻结结算版本。与并发的新增/修改/删除互斥，不会出现部分成功。

- 重复关闭（无论是否携带相同 `request_id`）返回原结算结果，`already_closed=true`；
- 事务失败整体回滚，重试不会产生第二份结算版本（`settlement_versions.batch_id` 与 `request_id` 均唯一）；
- `GET /api/batches/{id}/settlement` 查询冻结的结算版本。

### 复核队列（可恢复）

历史越界或关闭后写入的记录进入 `review_queue_items` 表持久化的复核队列，重启后不丢失：

- `GET /api/review-queue/?batch_id=&status=` 列出复核项；
- `POST /api/review-queue/{id}/decision`（`decision`: `approved` / `rejected`）裁定：`approved` 恢复或应用记录，`rejected` 持续排除；已裁定项目拒绝重复裁定。

记录的 `review_status`：`clear`（正常）、`pending`（待裁定）、`approved`（裁定通过）、`rejected`（裁定驳回）。

### 分析与追溯（默认排除未裁定，按版本重放）

- `GET /api/analysis/cycle/{batch_id}/` 与 `GET /api/analysis/traceability/{batch_id}/` 默认排除 `pending`/`rejected` 记录；加 `?include_review=true` 可见全量；
- 加 `?version=1` 按关闭时冻结的结算版本重放，不受后续更正裁定影响。

### 并发与存储

SQLite 连接以 `BEGIN IMMEDIATE` 开启事务并设置 30 秒 busy timeout：事务首条语句即取得写锁，"校验批次状态 → 写入记录" 与 "关闭批次" 在数据库层面串行化，任一时刻只有一个写事务，失败方整体回滚。

## 测试命令

```bash
python3 -m unittest discover -s tests -v
```

## 编译与构建命令

```bash
python3 -m compileall -q backend/app
```

## 启动命令

```bash
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后可访问 `/health` 检查服务状态。开发环境不得提交真实账号、连接凭据或生产数据。
