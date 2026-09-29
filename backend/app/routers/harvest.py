from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import HarvestSale
from ..schemas import HarvestSaleCreate, HarvestSaleUpdate, HarvestSaleResponse
from ._guard import submit_create, submit_update, submit_delete
from ..services.lifecycle import LifecycleError

RECORD_TYPE = "harvest_sale"

router = APIRouter(
    prefix="/api/harvest-sales",
    tags=["出塘销售"]
)

@router.post("/", response_model=HarvestSaleResponse)
def create_harvest_sale(sale: HarvestSaleCreate):
    try:
        return submit_create(RECORD_TYPE, sale.dict())
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.get("/", response_model=List[HarvestSaleResponse])
def get_harvest_sales(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(HarvestSale)
    if batch_id:
        query = query.filter(HarvestSale.batch_id == batch_id)
    sales = query.offset(skip).limit(limit).all()
    return sales

@router.get("/{sale_id}/", response_model=HarvestSaleResponse)
def get_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")
    return sale

@router.put("/{sale_id}/", response_model=HarvestSaleResponse)
def update_harvest_sale(sale_id: int, sale: HarvestSaleUpdate):
    try:
        return submit_update(RECORD_TYPE, sale_id, sale.dict(exclude_unset=True))
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

@router.delete("/{sale_id}/")
def delete_harvest_sale(sale_id: int):
    try:
        quarantined = submit_delete(RECORD_TYPE, sale_id)
        return quarantined if quarantined is not None else {"message": "出塘销售记录删除成功"}
    except LifecycleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
