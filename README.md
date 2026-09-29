# q002 水产养殖服务

本项目是水产养殖管理后端，维护塘口、养殖批次、投苗、投喂、水质、用药、成本、销售与周期分析数据。业务数据保存在 SQLite 文件中，HTTP 接口由 FastAPI 提供。

## 批次生命周期与记录校验

- 批次状态：`active`（养殖中）→ `harvested`（已收获）→ `closed`（已结算关闭，冻结）。
- 投苗、投喂、水质、用药、成本、销售六类记录的**新增/修改/删除**统一校验四个要素：批次状态、投苗日、实际收获日、业务发生日。
  - 业务发生日早于投苗日，或晚于实际收获日（含销售/成本落在收获日之外）：不直接落库，进入复核队列。
  - 批次未关闭时，超过补录窗口（默认 7 天，可用环境变量 `BACKFILL_WINDOW_DAYS` 调整）的历史补录同样进入复核队列；当天与未来的实时上报不受窗口限制。
  - 批次关闭后，一切写入都转入**关闭后更正流程**（复核队列），不允许直接改动已签署数据。
- 收容响应统一为 `202 Accepted`，返回 `review_item`；记录在裁定前不生效、不进入分析。

## 关闭、结算版本与复核

- `POST /api/batches/{id}/close/`：在**同一事务（BEGIN IMMEDIATE）**内确认未决写入并冻结结算版本 v1。存在未裁定复核项时返回 `409`，或显式 `?confirm_pending=true` 带未决项关闭。
- 重复关闭幂等：返回原冻结结果，绝不产生第二份版本；关闭失败回滚，重试仍只生成一份。
- `GET /api/batches/{id}/versions/` 列出版本；`GET /api/batches/{id}/versions/{n}/` 按版本重放完整记录快照与指标。
- `GET /api/review-items/` 复核队列（默认仅 pending）；`POST /api/review-items/{id}/resolve/` 裁定：
  - `approved`：恢复生效；批次已关闭时同事务冻结新结算版本（v2、v3……）。
  - `rejected`：仅留痕，不改变生效集、不产生新版本。
- 分析 `/api/analysis/cycle/{id}/` 与追溯默认排除未裁定记录；`?version=n` 按冻结版本重放，追溯可用 `?include_unresolved=true` 显式包含。
- 启动时自动为存量库补齐新列，并把历史越界记录迁入复核队列（标记 `flagged`，迁移幂等；可用 `LIFECYCLE_MIGRATE_ON_STARTUP=0` 关闭）。

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
