from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import FeedingRecord
from ..schemas import FeedingRecordCreate, FeedingRecordUpdate, FeedingRecordResponse
from ._guard import submit_create, submit_update, submit_delete
from ..services.lifecycle import LifecycleError

RECORD_TYPE = "feeding"

router = APIRouter(
    prefix="/api/feeding-records",
    tags=["投喂记录"]
)

@router.post("/", response_model=FeedingRecordResponse)
def create_feeding_record(record: FeedingRecordCreate):
    try:
        return submit_create(RECORD_TYPE, record.dict())
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.get("/", response_model=List[FeedingRecordResponse])
def get_feeding_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(FeedingRecord)
    if batch_id:
        query = query.filter(FeedingRecord.batch_id == batch_id)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=FeedingRecordResponse)
def get_feeding_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(FeedingRecord).filter(FeedingRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="投喂记录不存在")
    return record

@router.put("/{record_id}/", response_model=FeedingRecordResponse)
def update_feeding_record(record_id: int, record: FeedingRecordUpdate):
    try:
        return submit_update(RECORD_TYPE, record_id, record.dict(exclude_unset=True))
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.delete("/{record_id}/")
def delete_feeding_record(record_id: int):
    try:
        quarantined = submit_delete(RECORD_TYPE, record_id)
        return quarantined if quarantined is not None else {"message": "投喂记录删除成功"}
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
