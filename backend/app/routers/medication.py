from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import MedicationRecord
from ..schemas import MedicationRecordCreate, MedicationRecordUpdate, MedicationRecordResponse
from .. import lifecycle

router = APIRouter(
    prefix="/api/medication-records",
    tags=["用药记录"]
)

RECORD_TYPE = "medication"

@router.post("/", response_model=MedicationRecordResponse)
def create_medication_record(record: MedicationRecordCreate, db: Session = Depends(get_db)):
    lifecycle.guard_record_write(db, record.batch_id, RECORD_TYPE, record.medication_date)
    new_record = MedicationRecord(**record.model_dump())
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
    return new_record

@router.get("/", response_model=List[MedicationRecordResponse])
def get_medication_records(skip: int = 0, limit: int = 100, batch_id: int = None, review_status: str = None, db: Session = Depends(get_db)):
    query = db.query(MedicationRecord)
    if batch_id:
        query = query.filter(MedicationRecord.batch_id == batch_id)
    if review_status:
        query = query.filter(MedicationRecord.review_status == review_status)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=MedicationRecordResponse)
def get_medication_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(MedicationRecord).filter(MedicationRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="用药记录不存在")
    return record

@router.put("/{record_id}/", response_model=MedicationRecordResponse)
def update_medication_record(record_id: int, record: MedicationRecordUpdate, db: Session = Depends(get_db)):
    db_record = db.query(MedicationRecord).filter(MedicationRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="用药记录不存在")

    update_data = record.model_dump(exclude_unset=True)
    lifecycle.guard_record_mutation(db, db_record, RECORD_TYPE, update_data)
    for key, value in update_data.items():
        setattr(db_record, key, value)

    db.commit()
    db.refresh(db_record)
    return db_record

@router.delete("/{record_id}/")
def delete_medication_record(record_id: int, db: Session = Depends(get_db)):
    db_record = db.query(MedicationRecord).filter(MedicationRecord.id == record_id).first()
    if not db_record:
        raise HTTPException(status_code=404, detail="用药记录不存在")

    lifecycle.guard_record_mutation(db, db_record, RECORD_TYPE, None)
    db.delete(db_record)
    db.commit()
    return {"message": "用药记录删除成功"}
