from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from ..database import get_db, WriterSessionLocal
from ..models import ReviewItem
from ..schemas import ReviewItemResponse, ReviewDecision, ReviewResolveResponse, SettlementVersionInfo
from ..services import lifecycle
from ..services.lifecycle import LifecycleError

router = APIRouter(
    prefix="/api/review-items",
    tags=["复核队列"]
)

@router.get("/", response_model=List[ReviewItemResponse])
def list_review_items(
    batch_id: Optional[int] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """复核队列。默认仅返回 pending（未裁定）；分析与追溯默认排除这些记录。"""
    query = db.query(ReviewItem)
    if batch_id is not None:
        query = query.filter(ReviewItem.batch_id == batch_id)
    if status:
        query = query.filter(ReviewItem.status == status)
    else:
        query = query.filter(ReviewItem.status == lifecycle.REVIEW_PENDING)
    return query.order_by(ReviewItem.id).offset(skip).limit(limit).all()

@router.get("/{item_id}/", response_model=ReviewItemResponse)
def get_review_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(ReviewItem).filter(ReviewItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="复核记录不存在")
    return item

@router.post("/{item_id}/resolve/", response_model=ReviewResolveResponse)
def resolve_review_item(item_id: int, decision: ReviewDecision):
    """关闭后的更正流程：批准则恢复生效并在批次关闭时冻结新结算版本，拒绝则仅留痕。"""
    try:
        return lifecycle.resolve_review_item(
            WriterSessionLocal, item_id, decision.decision,
            note=decision.note, actor=decision.actor)
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


versions_router = APIRouter(
    prefix="/api/batches",
    tags=["结算版本"]
)

@versions_router.get("/{batch_id}/versions/", response_model=List[SettlementVersionInfo])
def list_versions(batch_id: int):
    try:
        return lifecycle.list_versions(WriterSessionLocal, batch_id)
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@versions_router.get("/{batch_id}/versions/{version_no}/")
def get_version(batch_id: int, version_no: int):
    """按版本重放：返回该版本冻结时的完整记录快照与指标。"""
    try:
        return lifecycle.get_version_snapshot(WriterSessionLocal, batch_id, version_no)
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
