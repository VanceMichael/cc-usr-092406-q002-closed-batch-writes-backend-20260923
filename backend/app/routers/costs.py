from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import CostRecord
from ..schemas import CostRecordCreate, CostRecordUpdate, CostRecordResponse
from ._guard import submit_create, submit_update, submit_delete
from ..services.lifecycle import LifecycleError

RECORD_TYPE = "cost"

router = APIRouter(
    prefix="/api/cost-records",
    tags=["成本核算"]
)

@router.post("/", response_model=CostRecordResponse)
def create_cost_record(record: CostRecordCreate):
    try:
        return submit_create(RECORD_TYPE, record.dict())
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.get("/", response_model=List[CostRecordResponse])
def get_cost_records(skip: int = 0, limit: int = 100, batch_id: int = None, cost_type: str = None, db: Session = Depends(get_db)):
    query = db.query(CostRecord)
    if batch_id:
        query = query.filter(CostRecord.batch_id == batch_id)
    if cost_type:
        query = query.filter(CostRecord.cost_type == cost_type)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=CostRecordResponse)
def get_cost_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(CostRecord).filter(CostRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="成本记录不存在")
    return record

@router.put("/{record_id}/", response_model=CostRecordResponse)
def update_cost_record(record_id: int, record: CostRecordUpdate):
    try:
        return submit_update(RECORD_TYPE, record_id, record.dict(exclude_unset=True))
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.delete("/{record_id}/")
def delete_cost_record(record_id: int):
    try:
        quarantined = submit_delete(RECORD_TYPE, record_id)
        return quarantined if quarantined is not None else {"message": "成本记录删除成功"}
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
