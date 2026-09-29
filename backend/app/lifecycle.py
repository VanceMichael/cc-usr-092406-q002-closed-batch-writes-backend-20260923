"""批次生命周期与业务记录写入的一致性约束。

集中实现：
- 六种业务记录的统一写入校验（批次状态 + 投苗日 + 实际收获日 + 业务发生日）；
- 关闭前的补录窗口（BACKFILL_WINDOW_DAYS，默认 7 天）与关闭后的更正流程的区分；
- 批次关闭事务：状态翻转、历史越界记录隔离、结算版本冻结在同一事务内完成，
  关闭幂等（重复关闭返回原结果，失败重试不会产生第二份结算版本）；
- 可恢复的复核队列：越界/关闭后写入的记录入队等待裁定，分析与追溯默认排除
  未裁定（pending）与已驳回（rejected）的记录，冻结版本支持按版本重放。
"""

import json
import os
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import schemas
from .models import (
    Batch,
    Pond,
    StockingRecord,
    FeedingRecord,
    WaterQualityRecord,
    MedicationRecord,
    CostRecord,
    HarvestSale,
    SettlementVersion,
    ReviewQueueItem,
    REVIEW_STATUS_CLEAR,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_REJECTED,
)

#: 关闭前允许的补录窗口（天）：业务发生日最晚可晚于实际收获日这么久
BACKFILL_WINDOW_DAYS = int(os.getenv("BACKFILL_WINDOW_DAYS", "7"))

#: 分析与追溯默认排除的复核状态（未裁定 + 已驳回）
REVIEW_EXCLUDED_STATUSES = (REVIEW_STATUS_PENDING, REVIEW_STATUS_REJECTED)

BATCH_STATUS_CLOSED = "closed"

#: 记录类型注册表：类型名 -> (模型, 业务日期字段, 创建schema, 更新schema)
#: stocking 没有独立业务日期字段，投苗行为即批次投苗日，仅做状态校验
RECORD_TYPES = {
    "stocking": (StockingRecord, None, schemas.StockingRecordCreate, schemas.StockingRecordUpdate),
    "feeding": (FeedingRecord, "feeding_date", schemas.FeedingRecordCreate, schemas.FeedingRecordUpdate),
    "water_quality": (WaterQualityRecord, "record_date", schemas.WaterQualityRecordCreate, schemas.WaterQualityRecordUpdate),
    "medication": (MedicationRecord, "medication_date", schemas.MedicationRecordCreate, schemas.MedicationRecordUpdate),
    "cost": (CostRecord, "cost_date", schemas.CostRecordCreate, schemas.CostRecordUpdate),
    "harvest_sale": (HarvestSale, "sale_date", schemas.HarvestSaleCreate, schemas.HarvestSaleUpdate),
}


# ---------------------------------------------------------------------------
# 写入守卫与日期校验
# ---------------------------------------------------------------------------

def backfill_deadline(batch: Batch):
    """补录窗口截止日：实际收获日 + BACKFILL_WINDOW_DAYS；未收获则为 None。"""
    if batch.actual_harvest_date is None:
        return None
    return batch.actual_harvest_date + timedelta(days=BACKFILL_WINDOW_DAYS)


def validate_business_date(batch: Batch, record_type: str, biz_date):
    """统一日期校验：业务发生日不得早于投苗日，不得晚于补录窗口截止日。"""
    if biz_date is None:
        return
    if biz_date < batch.stocking_date:
        raise HTTPException(
            status_code=400,
            detail=f"业务日期 {biz_date} 早于投苗日期 {batch.stocking_date}，超出养殖周期",
        )
    deadline = backfill_deadline(batch)
    if deadline is not None and biz_date > deadline:
        raise HTTPException(
            status_code=400,
            detail=(
                f"业务日期 {biz_date} 超出补录窗口：实际收获日 {batch.actual_harvest_date} "
                f"之后仅允许补录 {BACKFILL_WINDOW_DAYS} 天（截止 {deadline}）"
            ),
        )


def guard_batch_open(db: Session, batch_id: int) -> Batch:
    """在同一事务内抢占写锁并确认批次未关闭，返回最新批次行。

    守卫式 UPDATE 是事务的第一条写语句：与并发关闭互斥，要么本事务先拿到
    写锁（关闭方随后看到已写入的记录），要么关闭方先提交（此处 0 行命中，
    直接拒绝），不会出现部分成功。
    """
    now = datetime.utcnow()
    touched = (
        db.query(Batch)
        .filter(Batch.id == batch_id, Batch.status != BATCH_STATUS_CLOSED)
        .update({"updated_at": now}, synchronize_session=False)
    )
    if touched == 0:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        raise HTTPException(
            status_code=409,
            detail="批次已关闭，禁止直接写入，请通过更正流程提交复核队列",
        )
    return db.query(Batch).filter(Batch.id == batch_id).first()


def guard_record_write(db: Session, batch_id: int, record_type: str, biz_date) -> Batch:
    """新增记录的统一入口守卫：批次未关闭 + 业务日期在周期/补录窗口内。"""
    batch = guard_batch_open(db, batch_id)
    validate_business_date(batch, record_type, biz_date)
    return batch


def guard_record_mutation(db: Session, record, record_type: str, update_data) -> Batch:
    """修改/删除记录的统一守卫。

    - 批次必须未关闭（关闭后只能走更正流程）；
    - 待裁定记录禁止直接改动；
    - 修改后的业务日期仍需落在周期/补录窗口内。
    """
    if getattr(record, "review_status", REVIEW_STATUS_CLEAR) == REVIEW_STATUS_PENDING:
        raise HTTPException(status_code=409, detail="记录正在复核队列中等待裁定，禁止直接修改或删除")
    target_batch_id = record.batch_id
    if update_data is not None:
        target_batch_id = update_data.get("batch_id", record.batch_id)
    batch = guard_batch_open(db, record.batch_id)
    if target_batch_id != record.batch_id:
        batch = guard_batch_open(db, target_batch_id)
    if update_data is not None:
        date_field = RECORD_TYPES[record_type][1]
        if date_field is not None:
            biz_date = update_data.get(date_field, getattr(record, date_field))
            validate_business_date(batch, record_type, biz_date)
    return batch


# ---------------------------------------------------------------------------
# 历史越界记录隔离
# ---------------------------------------------------------------------------

def record_snapshot(record) -> dict:
    """记录行的完整快照（用于复核队列留痕与恢复）。"""
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def quarantine_violations(db: Session, batch: Batch) -> int:
    """把批次内越界（早于投苗日或晚于补录窗口）的历史记录隔离进复核队列。

    在关闭事务内调用：被隔离记录置为 pending，分析与追溯随即默认排除。
    """
    deadline = backfill_deadline(batch)
    count = 0
    for record_type, (model, date_field, _create, _update) in RECORD_TYPES.items():
        if date_field is None:
            continue
        column = getattr(model, date_field)
        conditions = [column < batch.stocking_date]
        if deadline is not None:
            conditions.append(column > deadline)
        rows = (
            db.query(model)
            .filter(
                model.batch_id == batch.id,
                model.review_status == REVIEW_STATUS_CLEAR,
                or_(*conditions),
            )
            .all()
        )
        for record in rows:
            record.review_status = REVIEW_STATUS_PENDING
            db.add(
                ReviewQueueItem(
                    batch_id=batch.id,
                    record_type=record_type,
                    record_id=record.id,
                    operation="quarantine",
                    reason="out_of_cycle_bounds",
                    payload=json.dumps(record_snapshot(record), ensure_ascii=False, default=str),
                    status="pending",
                )
            )
            count += 1
    db.flush()
    return count


# ---------------------------------------------------------------------------
# 周期分析与追溯的统一计算（供实时接口与关闭冻结共用）
# ---------------------------------------------------------------------------

def _visible_only(query, model, include_review: bool):
    """默认排除未裁定与已驳回记录；include_review=True 时返回全量。"""
    if not include_review:
        query = query.filter(~model.review_status.in_(REVIEW_EXCLUDED_STATUSES))
    return query


def compute_cycle_analysis(db: Session, batch: Batch, pond: Pond, include_review: bool = False) -> dict:
    from sqlalchemy import func

    def sum_column(model, column):
        query = db.query(func.sum(column)).filter(model.batch_id == batch.id)
        query = _visible_only(query, model, include_review)
        return query.scalar() or 0

    initial_quantity = sum_column(StockingRecord, StockingRecord.quantity)
    harvest_weight = sum_column(HarvestSale, HarvestSale.weight)
    feed_total = sum_column(FeedingRecord, FeedingRecord.feed_quantity)
    total_cost = sum_column(CostRecord, CostRecord.amount)
    total_revenue = sum_column(HarvestSale, HarvestSale.total_amount)

    harvest_date = batch.actual_harvest_date
    days_cultured = None
    if harvest_date:
        days_cultured = (harvest_date - batch.stocking_date).days

    survival_rate = 0
    if initial_quantity > 0 and harvest_weight > 0:
        avg_weight_per_fish = 0.5
        estimated_survival = harvest_weight / avg_weight_per_fish
        survival_rate = (estimated_survival / initial_quantity) * 100

    feed_conversion_ratio = 0
    if harvest_weight > 0 and feed_total > 0:
        feed_conversion_ratio = feed_total / harvest_weight

    yield_per_mu = 0
    if pond and pond.area > 0:
        yield_per_mu = harvest_weight / pond.area

    profit = total_revenue - total_cost

    cost_query = db.query(
        CostRecord.cost_type,
        func.sum(CostRecord.amount).label("total"),
    ).filter(CostRecord.batch_id == batch.id)
    cost_query = _visible_only(cost_query, CostRecord, include_review)
    costs = cost_query.group_by(CostRecord.cost_type).all()

    cost_breakdown = {c.cost_type: c.total for c in costs}
    known_types = ["feed", "medicine", "labor", "electricity"]
    other_cost = sum(
        amount for cost_type, amount in cost_breakdown.items()
        if cost_type not in known_types
    )
    cost_summary_dict = {
        "feed_cost": cost_breakdown.get("feed", 0),
        "medicine_cost": cost_breakdown.get("medicine", 0),
        "labor_cost": cost_breakdown.get("labor", 0),
        "electricity_cost": cost_breakdown.get("electricity", 0),
        "other_cost": other_cost,
        "total_cost": total_cost,
    }

    feeding_query = db.query(
        FeedingRecord.feed_type,
        func.sum(FeedingRecord.feed_quantity).label("total_quantity"),
        func.count(FeedingRecord.id).label("feeding_count"),
    ).filter(FeedingRecord.batch_id == batch.id)
    feeding_query = _visible_only(feeding_query, FeedingRecord, include_review)
    feeding_summary_dict = feeding_query.group_by(FeedingRecord.feed_type).all()

    feeding_count = sum(f.feeding_count for f in feeding_summary_dict)
    avg_daily_feed = 0
    if days_cultured and days_cultured > 0:
        avg_daily_feed = feed_total / days_cultured

    feeding_summary_result = {
        "total_feed_weight": feed_total,
        "feeding_count": feeding_count,
        "avg_daily_feed": avg_daily_feed,
    }

    return {
        "batch_number": batch.batch_number,
        "pond_name": pond.name if pond else "未知",
        "species": batch.species,
        "stocking_date": batch.stocking_date,
        "harvest_date": harvest_date,
        "days_cultured": days_cultured,
        "initial_quantity": initial_quantity,
        "harvest_weight": harvest_weight,
        "survival_rate": round(survival_rate, 2),
        "feed_total": feed_total,
        "feed_conversion_ratio": round(feed_conversion_ratio, 2),
        "area": pond.area if pond else 0,
        "yield_per_mu": round(yield_per_mu, 2),
        "total_cost": total_cost,
        "total_revenue": total_revenue,
        "profit": profit,
        "cost_summary": cost_summary_dict,
        "feeding_summary": feeding_summary_result,
    }


def compute_traceability(db: Session, batch: Batch, pond: Pond, include_review: bool = False) -> dict:
    def visible_records(model):
        query = db.query(model).filter(model.batch_id == batch.id)
        return _visible_only(query, model, include_review).all()

    stocking_records = visible_records(StockingRecord)
    feeding_records = visible_records(FeedingRecord)
    water_quality_records = visible_records(WaterQualityRecord)
    medication_records = visible_records(MedicationRecord)
    cost_records = visible_records(CostRecord)
    harvest_sales = visible_records(HarvestSale)

    return {
        "batch": {
            "batch_number": batch.batch_number,
            "species": batch.species,
            "stocking_date": batch.stocking_date,
            "harvest_date": batch.actual_harvest_date,
            "status": batch.status,
            "pond_id": batch.pond_id,
        },
        "pond_info": {
            "name": pond.name if pond else None,
            "area": pond.area if pond else None,
            "water_depth": pond.water_depth if pond else None,
        },
        "stocking_records": [
            {
                "species": r.species,
                "quantity": r.quantity,
                "source": r.source,
                "batch_number": r.batch_number,
                "stocking_date": r.created_at.date() if hasattr(r, "created_at") else None,
            }
            for r in stocking_records
        ],
        "feeding_records": [
            {
                "feeding_date": r.feeding_date,
                "feed_type": r.feed_type,
                "quantity": r.feed_quantity,
                "unit": "kg",
            }
            for r in feeding_records
        ],
        "water_quality_records": [
            {
                "record_date": r.record_date,
                "water_temperature": r.water_temperature,
                "ph_value": r.ph_value,
                "dissolved_oxygen": r.dissolved_oxygen,
            }
            for r in water_quality_records
        ],
        "medication_records": [
            {
                "medication_date": r.medication_date,
                "medication_name": r.drug_name,
                "dosage": r.dosage,
                "unit": r.dosage_unit,
            }
            for r in medication_records
        ],
        "cost_records": [
            {
                "cost_date": r.cost_date,
                "cost_type": r.cost_type,
                "amount": r.amount,
                "description": r.description,
            }
            for r in cost_records
        ],
        "harvest_sales": [
            {
                "sale_date": r.sale_date,
                "weight": r.weight,
                "unit_price": r.unit_price,
                "total_amount": r.total_amount,
                "buyer": r.buyer,
            }
            for r in harvest_sales
        ],
    }


# ---------------------------------------------------------------------------
# 批次关闭（幂等）与结算版本冻结
# ---------------------------------------------------------------------------

def _insert_settlement(
    db: Session,
    batch: Batch,
    request_id: str,
    closed_by,
    note,
    closed_at: datetime,
    quarantined_count: int,
) -> SettlementVersion:
    """在关闭事务内冻结结算版本；唯一约束冲突会令整个关闭事务回滚。"""
    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    snapshot = {
        "cycle": compute_cycle_analysis(db, batch, pond),
        "traceability": compute_traceability(db, batch, pond),
    }
    settlement = SettlementVersion(
        batch_id=batch.id,
        version_no=1,
        request_id=request_id,
        closed_by=closed_by,
        note=note,
        closed_at=closed_at,
        quarantined_count=quarantined_count,
        snapshot=json.dumps(snapshot, ensure_ascii=False, default=str),
    )
    db.add(settlement)
    db.flush()
    return settlement


def close_batch(db: Session, batch_id: int, request_id=None, closed_by=None, note=None):
    """关闭批次并冻结结算版本。

    返回 (settlement, batch, already_closed)。整个操作是单个事务：
    状态翻转、未决写入确认（历史越界隔离）、结算版本冻结一起提交或一起回滚。
    幂等：相同 request_id 或批次已关闭时返回原结算版本，不产生第二份。
    """
    if request_id:
        existing = (
            db.query(SettlementVersion)
            .filter(SettlementVersion.request_id == request_id)
            .first()
        )
        if existing is not None:
            batch = db.query(Batch).filter(Batch.id == existing.batch_id).first()
            return existing, batch, True

    now = datetime.utcnow()
    touched = (
        db.query(Batch)
        .filter(Batch.id == batch_id, Batch.status != BATCH_STATUS_CLOSED)
        .update(
            {"status": BATCH_STATUS_CLOSED, "closed_at": now, "updated_at": now},
            synchronize_session=False,
        )
    )
    if touched == 0:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        settlement = (
            db.query(SettlementVersion)
            .filter(SettlementVersion.batch_id == batch_id)
            .first()
        )
        if settlement is None:
            raise HTTPException(status_code=500, detail="批次已关闭但缺少结算版本，数据异常")
        return settlement, batch, True

    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    quarantined = quarantine_violations(db, batch)
    settlement = _insert_settlement(
        db,
        batch,
        request_id or f"close-{batch_id}-{uuid.uuid4().hex[:12]}",
        closed_by,
        note,
        now,
        quarantined,
    )
    return settlement, batch, False


def get_settlement(db: Session, batch_id: int, version_no=None) -> SettlementVersion:
    query = db.query(SettlementVersion).filter(SettlementVersion.batch_id == batch_id)
    if version_no is not None:
        query = query.filter(SettlementVersion.version_no == version_no)
    return query.first()


# ---------------------------------------------------------------------------
# 关闭后的更正流程与复核裁定
# ---------------------------------------------------------------------------

def submit_correction(db: Session, batch_id: int, req: schemas.CorrectionRequest) -> ReviewQueueItem:
    """关闭后的更正入口：不直接改数据，登记进复核队列等待裁定。"""
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status != BATCH_STATUS_CLOSED:
        raise HTTPException(status_code=400, detail="批次未关闭，可直接写入记录，无需更正流程")
    if req.record_type not in RECORD_TYPES:
        raise HTTPException(status_code=400, detail=f"未知记录类型: {req.record_type}")
    model, _date_field, create_schema, update_schema = RECORD_TYPES[req.record_type]

    if req.operation == "create":
        if not req.payload:
            raise HTTPException(status_code=400, detail="create 更正必须提供 payload")
        data = dict(req.payload)
        data["batch_id"] = batch.id
        try:
            validated = create_schema(**data)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"payload 校验失败: {exc.errors()}")
        record = model(**validated.model_dump())
        record.review_status = REVIEW_STATUS_PENDING
        db.add(record)
        db.flush()
        payload = validated.model_dump()
        record_id = record.id
    elif req.operation == "update":
        if req.record_id is None or not req.payload:
            raise HTTPException(status_code=400, detail="update 更正必须提供 record_id 与 payload")
        record = _find_batch_record(db, model, req.record_id, batch.id)
        try:
            validated = update_schema(**req.payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"payload 校验失败: {exc.errors()}")
        payload = validated.model_dump(exclude_unset=True)
        record_id = record.id
    elif req.operation == "delete":
        if req.record_id is None:
            raise HTTPException(status_code=400, detail="delete 更正必须提供 record_id")
        record = _find_batch_record(db, model, req.record_id, batch.id)
        payload = record_snapshot(record)
        record_id = record.id
    else:
        raise HTTPException(status_code=400, detail=f"未知更正操作: {req.operation}")

    item = ReviewQueueItem(
        batch_id=batch.id,
        record_type=req.record_type,
        record_id=record_id,
        operation=req.operation,
        reason="post_close_correction",
        payload=json.dumps(payload, ensure_ascii=False, default=str),
        status="pending",
        submitted_by=req.submitted_by,
    )
    db.add(item)
    db.flush()
    return item


def _find_batch_record(db: Session, model, record_id: int, batch_id: int):
    record = db.get(model, record_id)
    if record is None or record.batch_id != batch_id:
        raise HTTPException(status_code=404, detail="记录不存在")
    if record.review_status == REVIEW_STATUS_PENDING:
        raise HTTPException(status_code=409, detail="记录已在复核队列中等待裁定")
    return record


def _coerce_value(model, key, value):
    """把 JSON 载荷中的字符串按列类型还原（日期/日期时间）。"""
    column = model.__table__.columns.get(key)
    if column is None or value is None or not isinstance(value, str):
        return value
    column_type = column.type.__class__.__name__
    try:
        if column_type == "Date":
            return date.fromisoformat(value)
        if column_type == "DateTime":
            return datetime.fromisoformat(value)
    except ValueError:
        return value
    return value


def apply_decision(db: Session, item: ReviewQueueItem, decision: str, decided_by=None, note=None) -> ReviewQueueItem:
    """裁定复核项：approved 恢复/应用，rejected 持续排除。已裁定的项目拒绝重复裁定。"""
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision 必须是 approved 或 rejected")
    if item.status != "pending":
        raise HTTPException(status_code=409, detail="该复核项已裁定，不能重复裁定")
    model = RECORD_TYPES[item.record_type][0]
    record = db.get(model, item.record_id) if item.record_id is not None else None

    if item.operation in ("quarantine", "create"):
        if record is None:
            raise HTTPException(status_code=404, detail="关联记录不存在")
        record.review_status = (
            REVIEW_STATUS_APPROVED if decision == "approved" else REVIEW_STATUS_REJECTED
        )
    elif item.operation == "update":
        if decision == "approved":
            if record is None:
                raise HTTPException(status_code=404, detail="关联记录不存在")
            for key, value in json.loads(item.payload or "{}").items():
                if key in model.__table__.columns:
                    setattr(record, key, _coerce_value(model, key, value))
    elif item.operation == "delete":
        if decision == "approved":
            if record is None:
                raise HTTPException(status_code=404, detail="关联记录不存在")
            db.delete(record)

    item.status = decision
    item.decided_at = datetime.utcnow()
    item.decided_by = decided_by
    item.decision_note = note
    db.flush()
    return item


def review_item_to_dict(item: ReviewQueueItem) -> dict:
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "record_type": item.record_type,
        "record_id": item.record_id,
        "operation": item.operation,
        "reason": item.reason,
        "payload": json.loads(item.payload) if item.payload else None,
        "status": item.status,
        "submitted_by": item.submitted_by,
        "created_at": item.created_at,
        "decided_at": item.decided_at,
        "decided_by": item.decided_by,
        "decision_note": item.decision_note,
    }


def settlement_to_dict(settlement: SettlementVersion) -> dict:
    return {
        "id": settlement.id,
        "batch_id": settlement.batch_id,
        "version_no": settlement.version_no,
        "request_id": settlement.request_id,
        "closed_by": settlement.closed_by,
        "note": settlement.note,
        "closed_at": settlement.closed_at,
        "quarantined_count": settlement.quarantined_count,
        "snapshot": json.loads(settlement.snapshot),
    }
