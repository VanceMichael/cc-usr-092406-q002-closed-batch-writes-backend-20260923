from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from ..database import get_db, WriterSessionLocal
from ..models import Batch, Pond
from ..schemas import BatchCreate, BatchUpdate, BatchResponse, BatchCloseResponse
from ..services import lifecycle
from ..services.lifecycle import LifecycleError

router = APIRouter(
    prefix="/api/batches",
    tags=["批次管理"]
)

@router.post("/", response_model=BatchResponse)
def create_batch(batch: BatchCreate):
    db = WriterSessionLocal()
    try:
        db_pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
        if not db_pond:
            raise HTTPException(status_code=404, detail="塘口不存在")

        db_batch = db.query(Batch).filter(Batch.batch_number == batch.batch_number).first()
        if db_batch:
            raise HTTPException(status_code=400, detail="批次号已存在")

        data = batch.dict()
        if data.get("status") not in (None, lifecycle.BATCH_ACTIVE):
            raise HTTPException(status_code=400, detail="新建批次只能处于 active 状态")
        if data.get("actual_harvest_date") and data["actual_harvest_date"] < data["stocking_date"]:
            raise HTTPException(status_code=400, detail="实际收获日不能早于投苗日")
        data["status"] = lifecycle.BATCH_ACTIVE

        new_batch = Batch(**data)
        db.add(new_batch)
        db.commit()
        db.refresh(new_batch)
        return new_batch
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@router.get("/", response_model=List[BatchResponse])
def get_batches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    batches = db.query(Batch).offset(skip).limit(limit).all()
    return batches

@router.get("/{batch_id}/", response_model=BatchResponse)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch

@router.get("/by-number/{batch_number}/", response_model=BatchResponse)
def get_batch_by_number(batch_number: str, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch

@router.post("/{batch_id}/close/", response_model=BatchCloseResponse)
def close_batch(batch_id: int, confirm_pending: bool = False, actor: Optional[str] = None):
    """场区结算关闭批次：同事务确认未决写入并冻结结算版本。

    - 存在未裁定复核项时返回 409（除非 confirm_pending=true 显式确认）；
    - 重复关闭返回原冻结结果（idempotent=true），不会产生第二份结算版本。
    """
    try:
        return lifecycle.close_batch(WriterSessionLocal, batch_id,
                                     confirm_pending=confirm_pending, actor=actor)
    except LifecycleError as exc:
        if exc.extra:
            raise HTTPException(status_code=exc.status_code,
                                detail={"message": exc.detail, **exc.extra})
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.put("/{batch_id}/", response_model=BatchResponse)
def update_batch(batch_id: int, batch: BatchUpdate):
    db = WriterSessionLocal()
    try:
        db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if not db_batch:
            raise HTTPException(status_code=404, detail="批次不存在")

        update_data = batch.dict(exclude_unset=True)

        # 关闭后的批次为冻结态：只能通过关闭/复核流程演进，不允许直接改主数据。
        if db_batch.status == lifecycle.BATCH_CLOSED:
            frozen_forbidden = {"status", "stocking_date", "actual_harvest_date",
                                "pond_id", "species", "batch_number"}
            touched = frozen_forbidden & set(update_data)
            if touched:
                raise HTTPException(
                    status_code=409,
                    detail="批次已关闭并冻结，生命周期字段不得直接修改；如需更正请走复核流程")

        if "status" in update_data:
            new_status = update_data["status"]
            allowed = {
                lifecycle.BATCH_ACTIVE: {lifecycle.BATCH_HARVESTED},
                lifecycle.BATCH_HARVESTED: {lifecycle.BATCH_ACTIVE},
                lifecycle.BATCH_CLOSED: set(),
            }
            if new_status not in allowed.get(db_batch.status, set()):
                raise HTTPException(
                    status_code=409,
                    detail=f"不允许从 {db_batch.status} 迁移到 {new_status}；关闭请使用 close 端点")

        stocking_date = update_data.get("stocking_date", db_batch.stocking_date)
        harvest_date = update_data.get("actual_harvest_date", db_batch.actual_harvest_date)
        if harvest_date and harvest_date < stocking_date:
            raise HTTPException(status_code=400, detail="实际收获日不能早于投苗日")

        for key, value in update_data.items():
            setattr(db_batch, key, value)

        db.commit()
        db.refresh(db_batch)
        return db_batch
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@router.delete("/{batch_id}/")
def delete_batch(batch_id: int):
    db = WriterSessionLocal()
    try:
        db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
        if not db_batch:
            raise HTTPException(status_code=404, detail="批次不存在")
        if db_batch.status == lifecycle.BATCH_CLOSED:
            raise HTTPException(status_code=409, detail="批次已关闭并冻结，不能删除")

        db.delete(db_batch)
        db.commit()
        return {"message": "批次删除成功"}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
