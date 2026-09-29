from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Batch, Pond
from ..schemas import (
    BatchCreate, BatchUpdate, BatchResponse,
    BatchCloseRequest, BatchCloseResponse,
    SettlementVersionResponse, CorrectionRequest, ReviewQueueItemResponse,
)
from .. import lifecycle

router = APIRouter(
    prefix="/api/batches",
    tags=["批次管理"]
)

@router.post("/", response_model=BatchResponse)
def create_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    db_pond = db.query(Pond).filter(Pond.id == batch.pond_id).first()
    if not db_pond:
        raise HTTPException(status_code=404, detail="塘口不存在")

    db_batch = db.query(Batch).filter(Batch.batch_number == batch.batch_number).first()
    if db_batch:
        raise HTTPException(status_code=400, detail="批次号已存在")

    new_batch = Batch(**batch.model_dump())
    db.add(new_batch)
    db.commit()
    db.refresh(new_batch)
    return new_batch

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

@router.put("/{batch_id}/", response_model=BatchResponse)
def update_batch(batch_id: int, batch: BatchUpdate, db: Session = Depends(get_db)):
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    update_data = batch.model_dump(exclude_unset=True)
    if db_batch.status == "closed":
        raise HTTPException(status_code=409, detail="批次已关闭，批次信息不可再修改")
    if update_data.get("status") == "closed":
        raise HTTPException(status_code=400, detail="请使用关闭接口 POST /{batch_id}/close 关闭批次")
    if "status" in update_data and update_data["status"] not in ("active", "harvested"):
        raise HTTPException(status_code=400, detail="非法批次状态，仅支持 active, harvested")

    for key, value in update_data.items():
        setattr(db_batch, key, value)

    db.commit()
    db.refresh(db_batch)
    return db_batch

@router.delete("/{batch_id}/")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if db_batch.status == "closed":
        raise HTTPException(status_code=409, detail="批次已关闭并冻结结算，禁止删除")

    db.delete(db_batch)
    db.commit()
    return {"message": "批次删除成功"}

@router.post("/{batch_id}/close", response_model=BatchCloseResponse)
def close_batch(batch_id: int, payload: BatchCloseRequest = None, db: Session = Depends(get_db)):
    """关闭批次：同一事务内确认未决写入（隔离历史越界记录）并冻结结算版本。

    幂等：重复关闭（无论是否携带相同 request_id）返回原结算结果；
    事务失败整体回滚，重试不会产生第二份结算版本。
    """
    payload = payload or BatchCloseRequest()
    settlement, batch, already_closed = lifecycle.close_batch(
        db,
        batch_id,
        request_id=payload.request_id,
        closed_by=payload.closed_by,
        note=payload.note,
    )
    db.commit()
    return BatchCloseResponse(
        batch_id=batch.id,
        batch_number=batch.batch_number,
        status=batch.status,
        already_closed=already_closed,
        settlement=SettlementVersionResponse(**lifecycle.settlement_to_dict(settlement)),
    )

@router.get("/{batch_id}/settlement", response_model=SettlementVersionResponse)
def get_settlement(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    settlement = lifecycle.get_settlement(db, batch_id)
    if not settlement:
        raise HTTPException(status_code=404, detail="批次尚未关闭，无结算版本")
    return SettlementVersionResponse(**lifecycle.settlement_to_dict(settlement))

@router.post("/{batch_id}/corrections", response_model=ReviewQueueItemResponse)
def submit_correction(batch_id: int, req: CorrectionRequest, db: Session = Depends(get_db)):
    """关闭后的更正流程入口：登记进复核队列，裁定后才生效。"""
    item = lifecycle.submit_correction(db, batch_id, req)
    db.commit()
    return ReviewQueueItemResponse(**lifecycle.review_item_to_dict(item))
