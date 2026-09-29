import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from ..database import get_db
from ..models import Batch, Pond
from ..schemas import CultureCycleAnalysis, BatchTraceability
from .. import lifecycle

router = APIRouter(
    prefix="/api/analysis",
    tags=["养殖周期分析"]
)

def _load_batch_and_pond(db: Session, batch_id: int):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    return batch, pond

def _frozen_snapshot(db: Session, batch_id: int, version: int, section: str):
    settlement = lifecycle.get_settlement(db, batch_id, version_no=version)
    if not settlement:
        raise HTTPException(status_code=404, detail=f"结算版本 {version} 不存在")
    snapshot = json.loads(settlement.snapshot)
    data = snapshot[section]
    data["version_no"] = settlement.version_no
    data["frozen_at"] = settlement.closed_at.isoformat()
    return data

@router.get("/cycle/{batch_id}/", response_model=CultureCycleAnalysis)
def analyze_cycle(batch_id: int, version: Optional[int] = None, include_review: bool = False, db: Session = Depends(get_db)):
    """周期分析。默认排除未裁定/已驳回记录；version 指定时按冻结的结算版本重放。"""
    if version is not None:
        return _frozen_snapshot(db, batch_id, version, "cycle")
    batch, pond = _load_batch_and_pond(db, batch_id)
    return lifecycle.compute_cycle_analysis(db, batch, pond, include_review=include_review)

@router.get("/traceability/{batch_id}/", response_model=BatchTraceability)
def batch_traceability(batch_id: int, version: Optional[int] = None, include_review: bool = False, db: Session = Depends(get_db)):
    """批次追溯。默认排除未裁定/已驳回记录；version 指定时按冻结的结算版本重放。"""
    if version is not None:
        return _frozen_snapshot(db, batch_id, version, "traceability")
    batch, pond = _load_batch_and_pond(db, batch_id)
    return lifecycle.compute_traceability(db, batch, pond, include_review=include_review)

@router.get("/trace-by-number/{batch_number}/", response_model=BatchTraceability)
def trace_by_batch_number(batch_number: str, version: Optional[int] = None, include_review: bool = False, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise HTTPException(status_code=404, detail=f"批次号 {batch_number} 不存在")
    return batch_traceability(batch.id, version, include_review, db)
