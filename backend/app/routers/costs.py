from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import CostRecord
from ..schemas import CostRecordCreate, CostRecordUpdate, CostRecordResponse
from .. import lifecycle

router = APIRouter(
    prefix="/api/cost-records",
    tags=["成本核算"]
)

RECORD_TYPE = "cost"

@router.post("/", response_model=CostRecordResponse)
def create_cost_record(record: CostRecordCreate, db: Session = Depends(get_db)):
    lifecycle.guard_record_write(db, record.batch_id, RECORD_TYPE, record.cost_date)
    new_record = CostRecord(**record.model_dump())
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

@router.get("/", response_model=List[CostRecordResponse])
def get_cost_records(skip: int = 0, limit: int = 100, batch_id: int = None, cost_type: str = None, review_status: str = None, db: Session = Depends(get_db)):
    query = db.query(CostRecord)
    if batch_id:
        query = query.filter(CostRecord.batch_id == batch_id)
    if cost_type:
        query = query.filter(CostRecord.cost_type == cost_type)
    if review_status:
        query = query.filter(CostRecord.review_status == review_status)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=CostRecordResponse)
def get_cost_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(CostRecord).filter(CostRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="成本记录不存在")
    return record

@router.put("/{record_id}/", response_model=CostRecordResponse)
def update_cost_record(record_id: int, record: CostRecordUpdate, db: Session = Depends(get_db)):
    db_record = db.query(CostRecord).filter(CostRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="成本记录不存在")

    update_data = record.model_dump(exclude_unset=True)
    lifecycle.guard_record_mutation(db, db_record, RECORD_TYPE, update_data)
    for key, value in update_data.items():
        setattr(db_record, key, value)

    db.commit()
    db.refresh(db_record)
    return db_record

@router.delete("/{record_id}/")
def delete_cost_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(CostRecord).filter(CostRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="成本记录不存在")

    lifecycle.guard_record_mutation(db, db_record, RECORD_TYPE, None)
    db.delete(db_record)
    db.commit()
    return {"message": "成本记录删除成功"}
