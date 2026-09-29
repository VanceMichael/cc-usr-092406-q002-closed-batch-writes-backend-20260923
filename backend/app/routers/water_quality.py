from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import WaterQualityRecord
from ..schemas import WaterQualityRecordCreate, WaterQualityRecordUpdate, WaterQualityRecordResponse
from ._guard import submit_create, submit_update, submit_delete
from ..services.lifecycle import LifecycleError

RECORD_TYPE = "water_quality"

router = APIRouter(
    prefix="/api/water-quality-records",
    tags=["水质监测"]
)

@router.post("/", response_model=WaterQualityRecordResponse)
def create_water_quality_record(record: WaterQualityRecordCreate):
    try:
        return submit_create(RECORD_TYPE, record.dict())
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.get("/", response_model=List[WaterQualityRecordResponse])
def get_water_quality_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(WaterQualityRecord)
    if batch_id:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    records = query.offset(skip).limit(limit).all()
    return records

@router.get("/{record_id}/", response_model=WaterQualityRecordResponse)
def get_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(WaterQualityRecord).filter(WaterQualityRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="水质监测记录不存在")
    return record

@router.put("/{record_id}/", response_model=WaterQualityRecordResponse)
def update_water_quality_record(record_id: int, record: WaterQualityRecordUpdate):
    try:
        return submit_update(RECORD_TYPE, record_id, record.dict(exclude_unset=True))
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.delete("/{record_id}/")
def delete_water_quality_record(record_id: int):
    try:
        quarantined = submit_delete(RECORD_TYPE, record_id)
        return quarantined if quarantined is not None else {"message": "水质监测记录删除成功"}
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
