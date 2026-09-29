"""批次生命周期与业务记录写入的统一守卫。

约定：
- 所有写操作使用 writer 会话（SQLite 下为 BEGIN IMMEDIATE），关闭与记录写入在同一把写锁上串行。
- 校验四要素：批次状态、投苗日、实际收获日、业务发生日；active/harvested 另受补录窗口约束。
- 越界或关闭后写入不报错丢数，而是进入 review_items 复核队列（pending），裁定后可恢复生效。
- 关闭在同一事务内确认未决写入并冻结 settlement_versions v1；重复关闭返回原结果。
"""
import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from ..config import BACKFILL_WINDOW_DAYS
from ..models import (
    Base, Batch, Pond,
    StockingRecord, FeedingRecord, WaterQualityRecord, MedicationRecord,
    CostRecord, HarvestSale, ReviewItem, SettlementVersion,
    LIFECYCLE_ACTIVE, LIFECYCLE_FLAGGED,
)

BATCH_ACTIVE = "active"
BATCH_HARVESTED = "harvested"
BATCH_CLOSED = "closed"

OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"

REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"

# 收容原因
REASON_BEFORE_STOCKING = "before_stocking"   # 业务发生日早于投苗日
REASON_AFTER_HARVEST = "after_harvest"       # 业务发生日晚于实际收获日
REASON_OUTSIDE_BACKFILL = "outside_backfill" # 未关闭但超出补录窗口
REASON_BATCH_CLOSED = "batch_closed"         # 批次已关闭，须走更正流程


class LifecycleError(Exception):
    """业务语义错误，路由层映射为 4xx。"""

    def __init__(self, status_code: int, detail: str, extra: dict = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra or {}


# 记录类型 -> (模型, 业务发生日字段, [业务字段])
REGISTRY = {
    "stocking": (
        StockingRecord, None,
        ["species", "quantity", "source", "batch_number", "weight_per_unit",
         "total_weight", "notes"],
    ),
    "feeding": (
        FeedingRecord, "feeding_date",
        ["feeding_date", "feed_type", "feed_quantity", "feeding_time", "weather",
         "water_temperature", "notes"],
    ),
    "water_quality": (
        WaterQualityRecord, "record_date",
        ["record_date", "record_time", "water_temperature", "ph_value",
         "dissolved_oxygen", "ammonia_nitrogen", "nitrite", "transparency", "notes"],
    ),
    "medication": (
        MedicationRecord, "medication_date",
        ["medication_date", "drug_name", "drug_type", "dosage", "dosage_unit",
         "administration_method", "purpose", "manufacturer", "batch_number", "notes"],
    ),
    "cost": (
        CostRecord, "cost_date",
        ["cost_date", "cost_type", "amount", "description", "quantity", "unit",
         "unit_price", "notes"],
    ),
    "harvest_sale": (
        HarvestSale, "sale_date",
        ["sale_date", "weight", "unit_price", "total_amount", "buyer",
         "batch_number", "quality_grade", "notes"],
    ),
}

# 投苗记录的业务日就是投苗锚点本身，只做状态与区间校验，不受补录窗口限制。
WINDOW_APPLICABLE_TYPES = {"feeding", "water_quality", "medication", "cost", "harvest_sale"}


# --------------------------------------------------------------------------- 序列化

def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"不可序列化的类型: {type(value)!r}")


def dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def loads(text: str):
    return json.loads(text)


def serialize_row(row, record_type: str) -> dict:
    _, _, fields = REGISTRY[record_type]
    data = {
        "id": row.id,
        "batch_id": row.batch_id,
        "lifecycle_status": row.lifecycle_status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    for name in fields:
        value = getattr(row, name)
        data[name] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return data


# --------------------------------------------------------------------------- 校验

def _stocking_effective_date(batch, record_type, data):
    """投苗记录没有业务发生日字段，以批次投苗日为发生日参与窗口判断。"""
    if record_type == "stocking":
        return batch.stocking_date
    return data[REGISTRY[record_type][1]]


def _row_business_date(batch, record_type, row):
    date_attr = REGISTRY[record_type][1]
    return getattr(row, date_attr) if date_attr else batch.stocking_date


def evaluate_violation(batch, record_type, business_date, *, today: Optional[date] = None,
                       window_days: Optional[int] = None):
    """返回 None 表示允许直接写入，否则返回收容原因码。所有记录类型共用同一套规则。"""
    today = today or date.today()
    if window_days is None:
        window_days = BACKFILL_WINDOW_DAYS

    if business_date < batch.stocking_date:
        return REASON_BEFORE_STOCKING

    if batch.actual_harvest_date and business_date > batch.actual_harvest_date:
        return REASON_AFTER_HARVEST

    if batch.status == BATCH_CLOSED:
        return REASON_BATCH_CLOSED

    # active / harvested：补录窗口只约束过去日期（现场实时上报不受影响），投苗记录豁免。
    if record_type in WINDOW_APPLICABLE_TYPES and (today - business_date).days > window_days:
        return REASON_OUTSIDE_BACKFILL

    return None


REASON_DETAIL = {
    REASON_BEFORE_STOCKING: "业务发生日早于投苗日",
    REASON_AFTER_HARVEST: "业务发生日晚于实际收获日",
    REASON_OUTSIDE_BACKFILL: f"超出 {BACKFILL_WINDOW_DAYS} 天补录窗口",
    REASON_BATCH_CLOSED: "批次已关闭，须走关闭后更正流程",
}


# --------------------------------------------------------------------------- 写入入口

def _get_batch(db: Session, batch_id: int) -> Batch:
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise LifecycleError(404, "批次不存在")
    return batch


def _get_record(record_type: str, record_id: int, db: Session):
    model, _, _ = REGISTRY[record_type]
    row = db.query(model).filter(model.id == record_id).first()
    if not row:
        raise LifecycleError(404, "记录不存在")
    return row


def _normalize_amounts(record_type: str, data: dict):
    """销售记录的总金额派生逻辑，集中在一处保证创建/更新一致。"""
    if record_type == "harvest_sale":
        weight = data.get("weight")
        unit_price = data.get("unit_price")
        if data.get("total_amount") is None and weight is not None and unit_price is not None:
            data["total_amount"] = weight * unit_price


def _quarantine(db, batch, record_type, operation, reason, business_date,
                payload, record_id=None) -> ReviewItem:
    item = ReviewItem(
        batch_id=batch.id,
        record_type=record_type,
        operation=operation,
        record_id=record_id,
        reason=reason,
        business_date=business_date,
        payload_json=dumps(payload),
        status=REVIEW_PENDING,
    )
    db.add(item)
    db.flush()
    return item


def submit_record(writer_session_factory, record_type: str, operation: str,
                  data: Optional[dict] = None, record_id: Optional[int] = None,
                  *, today: Optional[date] = None):
    """统一的新增/修改/删除入口。全程在一个 BEGIN IMMEDIATE 事务内。

    返回 (outcome, payload)：
      ("applied", orm_row_or_None)    —— 直接生效（delete 时为 None）
      ("quarantined", ReviewItem)     —— 进入复核队列
    """
    if record_type not in REGISTRY:
        raise LifecycleError(400, f"未知记录类型: {record_type}")
    if operation not in (OP_CREATE, OP_UPDATE, OP_DELETE):
        raise LifecycleError(400, f"未知操作: {operation}")

    data = dict(data or {})
    db = writer_session_factory()
    try:
        if operation == OP_CREATE:
            batch = _get_batch(db, data["batch_id"])
            business_date = _stocking_effective_date(batch, record_type, data)
            reason = evaluate_violation(batch, record_type, business_date, today=today)
            if reason:
                _normalize_amounts(record_type, data)
                item = _quarantine(db, batch, record_type, OP_CREATE, reason,
                                   business_date, data)
                db.commit()
                db.refresh(item)
                return "quarantined", item
            _normalize_amounts(record_type, data)
            row = REGISTRY[record_type][0](**data)
            db.add(row)
            db.commit()
            db.refresh(row)
            return "applied", row

        # update / delete：先取现有记录与所属批次
        existing = _get_record(record_type, record_id, db)
        target_batch_id = data.get("batch_id", existing.batch_id)
        batch = _get_batch(db, target_batch_id)

        if operation == OP_DELETE:
            business_date = _row_business_date(batch, record_type, existing)
            reason = evaluate_violation(batch, record_type, business_date, today=today)
            if reason:
                item = _quarantine(db, batch, record_type, OP_DELETE, reason,
                                   business_date, serialize_row(existing, record_type),
                                   record_id=existing.id)
                db.commit()
                db.refresh(item)
                return "quarantined", item
            db.delete(existing)
            db.commit()
            return "applied", None

        # update：以合并后的业务发生日校验
        business_date = _row_business_date(batch, record_type, existing)
        date_attr = REGISTRY[record_type][1]
        if date_attr is not None and date_attr in data:
            business_date = data[date_attr]
        reason = evaluate_violation(batch, record_type, business_date, today=today)
        if reason:
            item = _quarantine(db, batch, record_type, OP_UPDATE, reason,
                               business_date, data, record_id=existing.id)
            db.commit()
            db.refresh(item)
            return "quarantined", item
        _normalize_amounts(record_type, data)
        for key, value in data.items():
            setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return "applied", existing
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------- 结算快照与指标

def effective_query(db: Session, model, batch_id: int):
    return db.query(model).filter(
        model.batch_id == batch_id,
        model.lifecycle_status == LIFECYCLE_ACTIVE,
    )


def compute_metrics(db: Session, batch: Batch, pond: Optional[Pond]) -> dict:
    """周期分析指标，只统计 lifecycle_status=active（已裁定生效）的记录。"""
    initial_quantity = effective_query(db, StockingRecord, batch.id).with_entities(
        func.sum(StockingRecord.quantity)).scalar() or 0
    harvest_weight = effective_query(db, HarvestSale, batch.id).with_entities(
        func.sum(HarvestSale.weight)).scalar() or 0
    feed_total = effective_query(db, FeedingRecord, batch.id).with_entities(
        func.sum(FeedingRecord.feed_quantity)).scalar() or 0
    total_cost = effective_query(db, CostRecord, batch.id).with_entities(
        func.sum(CostRecord.amount)).scalar() or 0
    total_revenue = effective_query(db, HarvestSale, batch.id).with_entities(
        func.sum(HarvestSale.total_amount)).scalar() or 0

    days_cultured = None
    if batch.actual_harvest_date:
        days_cultured = (batch.actual_harvest_date - batch.stocking_date).days

    survival_rate = 0.0
    if initial_quantity > 0 and harvest_weight > 0:
        avg_weight_per_fish = 0.5
        estimated_survival = harvest_weight / avg_weight_per_fish
        survival_rate = (estimated_survival / initial_quantity) * 100

    feed_conversion_ratio = feed_total / harvest_weight if harvest_weight > 0 and feed_total > 0 else 0.0
    yield_per_mu = harvest_weight / pond.area if pond and pond.area > 0 else 0.0

    costs = db.query(
        CostRecord.cost_type, func.sum(CostRecord.amount).label("total")
    ).filter(
        CostRecord.batch_id == batch.id,
        CostRecord.lifecycle_status == LIFECYCLE_ACTIVE,
    ).group_by(CostRecord.cost_type).all()
    breakdown = {c.cost_type: c.total for c in costs}
    known = ["feed", "medicine", "labor", "electricity"]
    cost_summary = {
        "feed_cost": breakdown.get("feed", 0),
        "medicine_cost": breakdown.get("medicine", 0),
        "labor_cost": breakdown.get("labor", 0),
        "electricity_cost": breakdown.get("electricity", 0),
        "other_cost": sum(v for k, v in breakdown.items() if k not in known),
        "total_cost": total_cost,
    }

    feeding_rows = db.query(
        func.count(FeedingRecord.id).label("feeding_count")
    ).filter(
        FeedingRecord.batch_id == batch.id,
        FeedingRecord.lifecycle_status == LIFECYCLE_ACTIVE,
    ).one()
    feeding_count = feeding_rows.feeding_count or 0
    avg_daily_feed = (feed_total / days_cultured) if days_cultured and days_cultured > 0 else 0.0

    return {
        "batch_number": batch.batch_number,
        "pond_name": pond.name if pond else "未知",
        "species": batch.species,
        "stocking_date": batch.stocking_date.isoformat(),
        "harvest_date": batch.actual_harvest_date.isoformat() if batch.actual_harvest_date else None,
        "days_cultured": days_cultured,
        "initial_quantity": int(initial_quantity),
        "harvest_weight": harvest_weight,
        "survival_rate": round(survival_rate, 2),
        "feed_total": feed_total,
        "feed_conversion_ratio": round(feed_conversion_ratio, 2),
        "area": pond.area if pond else 0,
        "yield_per_mu": round(yield_per_mu, 2),
        "total_cost": total_cost,
        "total_revenue": total_revenue,
        "profit": total_revenue - total_cost,
        "cost_summary": cost_summary,
        "feeding_summary": {
            "total_feed_weight": feed_total,
            "feeding_count": feeding_count,
            "avg_daily_feed": avg_daily_feed,
        },
    }


def build_snapshot(db: Session, batch: Batch) -> dict:
    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    records = {}
    for record_type, (model, _, _) in REGISTRY.items():
        records[record_type] = [
            serialize_row(r, record_type)
            for r in effective_query(db, model, batch.id).all()
        ]
    return {
        "batch": {
            "id": batch.id,
            "batch_number": batch.batch_number,
            "status": batch.status,
            "stocking_date": batch.stocking_date.isoformat(),
            "actual_harvest_date": batch.actual_harvest_date.isoformat()
                if batch.actual_harvest_date else None,
            "closed_at": batch.closed_at.isoformat() if batch.closed_at else None,
        },
        "frozen_at": datetime.utcnow().isoformat(),
        "records": records,
        "metrics": compute_metrics(db, batch, pond),
    }


# --------------------------------------------------------------------------- 批次关闭

def close_batch(writer_session_factory, batch_id: int, *, confirm_pending: bool = False,
                actor: Optional[str] = None):
    """关闭批次：同事务确认未决写入 + 冻结结算版本。重复关闭返回原版本（幂等）。"""
    db = writer_session_factory()
    try:
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if not batch:
            raise LifecycleError(404, "批次不存在")

        if batch.status == BATCH_CLOSED:
            version = db.query(SettlementVersion).filter(
                SettlementVersion.batch_id == batch.id,
                SettlementVersion.version == batch.current_version,
            ).first()
            if version is None:  # 极端不一致时回退到最新版本，而非 500
                version = db.query(SettlementVersion).filter(
                    SettlementVersion.batch_id == batch.id
                ).order_by(SettlementVersion.version.desc()).first()
            return {
                "idempotent": True,
                "batch_id": batch.id,
                "status": batch.status,
                "version": _version_payload(batch, version),
            }

        if batch.status not in (BATCH_ACTIVE, BATCH_HARVESTED):
            raise LifecycleError(409, f"批次状态 {batch.status} 不允许关闭")

        pending = db.query(ReviewItem).filter(
            ReviewItem.batch_id == batch.id,
            ReviewItem.status == REVIEW_PENDING,
        ).all()
        if pending and not confirm_pending:
            raise LifecycleError(
                409,
                f"存在 {len(pending)} 条未裁定的复核记录，关闭前必须先裁定或显式确认带未决项关闭",
                extra={"pending_review_item_ids": [p.id for p in pending],
                       "pending_count": len(pending)},
            )

        batch.status = BATCH_CLOSED
        batch.closed_at = datetime.utcnow()
        batch.current_version = 1
        db.flush()
        snapshot = build_snapshot(db, batch)
        version = SettlementVersion(
            batch_id=batch.id,
            version=1,
            trigger="close",
            snapshot_json=dumps(snapshot),
            created_by=actor,
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        return {
            "idempotent": False,
            "batch_id": batch.id,
            "status": BATCH_CLOSED,
            "pending_count": len(pending),
            "version": _version_payload(batch, version),
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _version_payload(batch: Batch, version: SettlementVersion) -> dict:
    snapshot = loads(version.snapshot_json)
    return {
        "version_id": version.id,
        "version": version.version,
        "trigger": version.trigger,
        "batch_id": batch.id,
        "batch_current_version": batch.current_version,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "metrics": snapshot["metrics"],
    }


# --------------------------------------------------------------------------- 复核裁定（关闭后的更正流程）

def resolve_review_item(writer_session_factory, item_id: int, decision: str,
                        *, note: Optional[str] = None, actor: Optional[str] = None):
    """裁定复核项。批准即恢复生效；批次已关闭时在同事务冻结新结算版本。"""
    if decision not in (REVIEW_APPROVED, REVIEW_REJECTED):
        raise LifecycleError(400, "decision 必须是 approved 或 rejected")

    db = writer_session_factory()
    try:
        item = db.query(ReviewItem).filter(ReviewItem.id == item_id).first()
        if not item:
            raise LifecycleError(404, "复核记录不存在")
        if item.status != REVIEW_PENDING:
            raise LifecycleError(409, f"复核记录已裁定: {item.status}",
                                extra={"current_status": item.status})

        batch = _get_batch(db, item.batch_id)
        payload = loads(item.payload_json)
        mutated = False

        if decision == REVIEW_APPROVED:
            mutated = _apply_approved(db, item, batch, payload)
        # rejected：create/update 不改变生效集；历史 flagged 行保持 flagged（继续默认排除）。

        item.status = decision
        item.resolution_note = note
        item.resolved_by = actor
        item.resolved_at = datetime.utcnow()

        new_version = None
        if batch.status == BATCH_CLOSED and mutated:
            db.flush()  # 确保本事务恢复/撤销的行对快照查询可见（autoflush=False）
            next_no = (batch.current_version or 0) + 1
            snapshot = build_snapshot(db, batch)
            version = SettlementVersion(
                batch_id=batch.id,
                version=next_no,
                trigger="correction",
                snapshot_json=dumps(snapshot),
                created_by=actor,
            )
            db.add(version)
            db.flush()
            batch.current_version = next_no
            new_version = version

        db.commit()
        db.refresh(item)
        if new_version is not None:
            db.refresh(new_version)
        return {
            "review_item_id": item.id,
            "decision": decision,
            "batch_id": batch.id,
            "batch_status": batch.status,
            "effective_data_changed": mutated,
            "new_version": _version_payload(batch, new_version) if new_version else None,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _coerce_dates(record_type: str, data: dict) -> dict:
    """JSON 往返后业务发生日为 ISO 字符串，构造 ORM 对象前转回 date。"""
    date_attr = REGISTRY[record_type][1]
    if date_attr and isinstance(data.get(date_attr), str):
        data[date_attr] = date.fromisoformat(data[date_attr])
    return data


def _apply_approved(db: Session, item: ReviewItem, batch: Batch, payload: dict) -> bool:
    model, _, fields = REGISTRY[item.record_type]

    existing = None
    if item.record_id is not None:
        existing = db.query(model).filter(model.id == item.record_id).first()

    if item.operation == OP_DELETE:
        if existing is not None:
            db.delete(existing)
            return True
        return False

    if item.operation == OP_CREATE:
        if existing is not None:
            # 历史迁移：被 flag 的原行就地恢复，并保留复核项回链作为人工裁定凭证，
            # 这样重启时迁移不会把已批准的越界更正再次打成未裁定。
            if existing.lifecycle_status != LIFECYCLE_ACTIVE:
                existing.lifecycle_status = LIFECYCLE_ACTIVE
                existing.review_item_id = item.id
                return True
            return False
        data = {k: v for k, v in payload.items() if k in fields}
        data["batch_id"] = batch.id
        _coerce_dates(item.record_type, data)
        row = model(**data)
        row.review_item_id = item.id  # 裁定凭证：重启迁移据此识别"人工已批准"的越界数据
        db.add(row)
        return True

    # update
    if existing is None:
        # 原记录已不存在，无法补改；按拒绝处理由调用方报错更安全。
        raise LifecycleError(409, "原记录已不存在，无法批准修改")
    _coerce_dates(item.record_type, payload)
    # 销售金额随重量/单价联动，保证更正后的总额与普通更新口径一致。
    if item.record_type == "harvest_sale" and (
            "weight" in payload or "unit_price" in payload):
        weight = payload.get("weight", existing.weight)
        unit_price = payload.get("unit_price", existing.unit_price)
        if weight is not None and unit_price is not None:
            payload["total_amount"] = weight * unit_price
    changed = False
    for key, value in payload.items():
        if key in fields and getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True
    if existing.lifecycle_status != LIFECYCLE_ACTIVE:
        existing.lifecycle_status = LIFECYCLE_ACTIVE
        changed = True
    existing.review_item_id = item.id
    return changed


# --------------------------------------------------------------------------- 版本查询

def list_versions(session_factory, batch_id: int):
    db = session_factory()
    try:
        versions = db.query(SettlementVersion).filter(
            SettlementVersion.batch_id == batch_id
        ).order_by(SettlementVersion.version).all()
        batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if not batch:
            raise LifecycleError(404, "批次不存在")
        return [_version_payload(batch, v) for v in versions]
    finally:
        db.close()


def load_version_snapshot(db: Session, batch_id: int, version_no: Optional[int] = None):
    """在给定会话内读取版本快照。version_no 为空时取批次当前版本。"""
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise LifecycleError(404, "批次不存在")
    target = version_no if version_no is not None else batch.current_version
    if target is None:
        raise LifecycleError(404, "批次尚无冻结结算版本")
    version = db.query(SettlementVersion).filter(
        SettlementVersion.batch_id == batch_id,
        SettlementVersion.version == target,
    ).first()
    if not version:
        raise LifecycleError(404, f"结算版本 {target} 不存在")
    return loads(version.snapshot_json)


def get_version_snapshot(session_factory, batch_id: int, version_no: Optional[int] = None):
    db = session_factory()
    try:
        return load_version_snapshot(db, batch_id, version_no)
    finally:
        db.close()


# --------------------------------------------------------------------------- 历史数据迁移

def ensure_schema(engine) -> None:
    """create_all 之外，为存量 SQLite 补齐后加的列（SQLAlchemy 不会自动 ALTER 既有表）。"""
    Base.metadata.create_all(bind=engine)
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    added_columns = {
        "batches": {"closed_at": "DATETIME", "current_version": "INTEGER"},
    }
    for record_type, (model, _, _) in REGISTRY.items():
        added_columns[model.__tablename__] = {
            "lifecycle_status": f"VARCHAR(20) NOT NULL DEFAULT '{LIFECYCLE_ACTIVE}'",
            "review_item_id": "INTEGER",
        }

    # 反射必须在事务外完成：内存库使用单连接池，事务内再取连接反射会嵌套 BEGIN。
    columns_present = {
        table: {col["name"] for col in inspector.get_columns(table)}
        for table in existing_tables
    }

    with engine.begin() as conn:
        for table, columns in added_columns.items():
            if table not in existing_tables:
                continue
            present = columns_present[table]
            for column, ddl_type in columns.items():
                if column not in present:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def migrate_legacy_records(writer_session_factory, *, today: Optional[date] = None) -> int:
    """把历史上越界（早于投苗日 / 晚于实际收获日）的现存记录移入复核队列。

    幂等：已 flag 或已存在对应 pending 复核项的记录跳过。返回本次新收容条数。
    """
    db = writer_session_factory()
    queued = 0
    try:
        batches = db.query(Batch).all()
        for batch in batches:
            for record_type, (model, date_attr, _) in REGISTRY.items():
                rows = db.query(model).filter(model.batch_id == batch.id).all()
                for row in rows:
                    business_date = getattr(row, date_attr) if date_attr else batch.stocking_date
                    reason = evaluate_violation(
                        batch, record_type, business_date, today=today,
                        # 历史迁移只处理硬性越界，不把"超补录窗口"的正常旧账打成越界。
                        window_days=10 ** 9,
                    )
                    if reason in (REASON_BATCH_CLOSED, REASON_OUTSIDE_BACKFILL, None):
                        continue
                    if row.lifecycle_status == LIFECYCLE_FLAGGED:
                        continue
                    # 已带裁定回链（人工批准的越界更正）：重启迁移不得再次收容。
                    if getattr(row, "review_item_id", None) is not None:
                        continue
                    exists = db.query(ReviewItem).filter(
                        ReviewItem.record_type == record_type,
                        ReviewItem.record_id == row.id,
                        ReviewItem.status == REVIEW_PENDING,
                    ).first()
                    if exists:
                        continue
                    item = _quarantine(db, batch, record_type, OP_CREATE, reason,
                                       business_date, serialize_row(row, record_type),
                                       record_id=row.id)
                    row.lifecycle_status = LIFECYCLE_FLAGGED
                    row.review_item_id = item.id
                    queued += 1
        db.commit()
        return queued
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
