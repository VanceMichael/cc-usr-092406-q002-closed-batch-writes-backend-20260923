from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import StockingRecord
from ..schemas import StockingRecordCreate, StockingRecordUpdate, StockingRecordResponse
from .. import lifecycle

router = APIRouter(
    prefix="/api/stocking-records",
    tags=["投苗记录"]
)

RECORD_TYPE = "stocking"

@router.post("/", response_model=StockingRecordResponse)
def create_stocking_record(record: StockingRecordCreate, db: Session = Depends(get_db)):
    # 投苗记录没有独立业务日期，投苗行为即批次投苗日，仅校验批次未关闭
    lifecycle.guard_record_write(db, record.batch_id, RECORD_TYPE, None)
    new_record = StockingRecord(**record.model_dump())
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

@router.get("/", response_model=List[StockingRecordResponse])
def get_stocking_records(skip: int = 0, limit: int = 100, batch_id: int = None, review_status: str = None, db: Session = Depends(get_db)):
    query = db.query(StockingRecord)
    if batch_id:
        query = query.filter(StockingRecord.batch_id == batch_id)
    if review_status:
        query = query.filter(StockingRecord.review_status == review_status)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=StockingRecordResponse)
def get_stocking_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(StockingRecord).filter(StockingRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="投苗记录不存在")
    return record

@router.put("/{record_id}/", response_model=StockingRecordResponse)
def update_stocking_record(record_id: int, record: StockingRecordUpdate, db: Session = Depends(get_db)):
    db_record = db.query(StockingRecord).filter(StockingRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="投苗记录不存在")

    update_data = record.model_dump(exclude_unset=True)
    lifecycle.guard_record_mutation(db, db_record, RECORD_TYPE, update_data)
    for key, value in update_data.items():
        setattr(db_record, key, value)

    db.commit()
    db.refresh(db_record)
    return db_record

@router.delete("/{record_id}/")
def delete_stocking_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(StockingRecord).filter(StockingRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="投苗记录不存在")

    lifecycle.guard_record_mutation(db, db_record, RECORD_TYPE, None)
    db.delete(db_record)
    db.commit()
    return {"message": "投苗记录删除成功"}
