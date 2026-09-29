from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from ..database import get_db
from ..models import (
    Batch, Pond, StockingRecord, FeedingRecord, CostRecord,
    HarvestSale, WaterQualityRecord, MedicationRecord,
    LIFECYCLE_ACTIVE,
)
from ..schemas import CultureCycleAnalysis, BatchTraceability, BatchInfo, PondInfo
from ..services import lifecycle
from ..services.lifecycle import LifecycleError

router = APIRouter(
    prefix="/api/analysis",
    tags=["养殖周期分析"]
)


@router.get("/cycle/{batch_id}/", response_model=CultureCycleAnalysis)
def analyze_cycle(batch_id: int, version: Optional[int] = None, db: Session = Depends(get_db)):
    """周期分析。

    - 默认只统计 lifecycle_status=active 的记录（未裁定的复核记录被排除）；
    - version=N 时按第 N 个冻结结算版本重放，结果与关闭时刻完全一致。
    """
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    if version is not None or batch.current_version is not None:
        try:
            snapshot = lifecycle.load_version_snapshot(db, batch_id, version)
        except LifecycleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return CultureCycleAnalysis(**snapshot["metrics"])

    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    return CultureCycleAnalysis(**lifecycle.compute_metrics(db, batch, pond))


@router.get("/traceability/{batch_id}/", response_model=BatchTraceability)
def batch_traceability(batch_id: int, include_unresolved: bool = False,
                       db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()

    def rows(model):
        query = db.query(model).filter(model.batch_id == batch.id)
        if not include_unresolved:
            query = query.filter(model.lifecycle_status == LIFECYCLE_ACTIVE)
        return query.all()

    stocking_records = rows(StockingRecord)
    feeding_records = rows(FeedingRecord)
    water_quality_records = rows(WaterQualityRecord)
    medication_records = rows(MedicationRecord)
    cost_records = rows(CostRecord)
    harvest_sales = rows(HarvestSale)

    return BatchTraceability(
        batch=BatchInfo(
            batch_number=batch.batch_number,
            species=batch.species,
            stocking_date=batch.stocking_date,
            harvest_date=batch.actual_harvest_date,
            status=batch.status,
            pond_id=batch.pond_id
        ),
        pond_info=PondInfo(
            name=pond.name if pond else None,
            area=pond.area if pond else None,
            water_depth=pond.water_depth if pond else None
        ),
        stocking_records=[
            {
                "species": r.species,
                "quantity": r.quantity,
                "source": r.source,
                "batch_number": r.batch_number,
                "stocking_date": r.created_at.date() if hasattr(r, 'created_at') else None
            } for r in stocking_records
        ],
        feeding_records=[
            {
                "feeding_date": r.feeding_date,
                "feed_type": r.feed_type,
                "quantity": r.feed_quantity,
                "unit": "kg"
            } for r in feeding_records
        ],
        water_quality_records=[
            {
                "record_date": r.record_date,
                "water_temperature": r.water_temperature,
                "ph_value": r.ph_value,
                "dissolved_oxygen": r.dissolved_oxygen
            } for r in water_quality_records
        ],
        medication_records=[
            {
                "medication_date": r.medication_date,
                "medication_name": r.drug_name,
                "dosage": r.dosage,
                "unit": r.dosage_unit
            } for r in medication_records
        ],
        cost_records=[
            {
                "cost_date": r.cost_date,
                "cost_type": r.cost_type,
                "amount": r.amount,
                "description": r.description
            } for r in cost_records
        ],
        harvest_sales=[
            {
                "sale_date": r.sale_date,
                "weight": r.weight,
                "unit_price": r.unit_price,
                "total_amount": r.total_amount,
                "buyer": r.buyer
            } for r in harvest_sales
        ]
    )

@router.get("/trace-by-number/{batch_number}/", response_model=BatchTraceability)
def trace_by_batch_number(batch_number: str, include_unresolved: bool = False,
                          db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise HTTPException(status_code=404, detail=f"批次号 {batch_number} 不存在")
    return batch_traceability(batch.id, include_unresolved=include_unresolved, db=db)
