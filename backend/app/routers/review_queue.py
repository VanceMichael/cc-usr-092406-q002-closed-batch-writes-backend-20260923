from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import ReviewQueueItem
from ..schemas import ReviewQueueItemResponse, ReviewDecisionRequest
from .. import lifecycle

router = APIRouter(
    prefix="/api/review-queue",
    tags=["复核队列"]
)

@router.get("/", response_model=List[ReviewQueueItemResponse])
def list_review_items(batch_id: int = None, status: str = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(ReviewQueueItem)
    if batch_id is not None:
        query = query.filter(ReviewQueueItem.batch_id == batch_id)
    if status:
        query = query.filter(ReviewQueueItem.status == status)
    items = query.order_by(ReviewQueueItem.id).offset(skip).limit(limit).all()
    return [ReviewQueueItemResponse(**lifecycle.review_item_to_dict(item)) for item in items]

@router.get("/{item_id}/", response_model=ReviewQueueItemResponse)
def get_review_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(ReviewQueueItem).filter(ReviewQueueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="复核项不存在")
    return ReviewQueueItemResponse(**lifecycle.review_item_to_dict(item))

@router.post("/{item_id}/decision", response_model=ReviewQueueItemResponse)
def decide_review_item(item_id: int, req: ReviewDecisionRequest, db: Session = Depends(get_db)):
    """裁定复核项：approved 恢复/应用记录，rejected 持续排除。裁定结果落库，重启后可继续处理剩余项。"""
    item = db.query(ReviewQueueItem).filter(ReviewQueueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="复核项不存在")
    item = lifecycle.apply_decision(db, item, req.decision, decided_by=req.decided_by, note=req.note)
    db.commit()
    return ReviewQueueItemResponse(**lifecycle.review_item_to_dict(item))
