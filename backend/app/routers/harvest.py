from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import HarvestSale
from ..schemas import HarvestSaleCreate, HarvestSaleUpdate, HarvestSaleResponse
from .. import lifecycle

router = APIRouter(
    prefix="/api/harvest-sales",
    tags=["出塘销售"]
)

RECORD_TYPE = "harvest_sale"

@router.post("/", response_model=HarvestSaleResponse)
def create_harvest_sale(sale: HarvestSaleCreate, db: Session = Depends(get_db)):
    lifecycle.guard_record_write(db, sale.batch_id, RECORD_TYPE, sale.sale_date)

    if sale.total_amount is None:
        sale.total_amount = sale.weight * sale.unit_price

    new_sale = HarvestSale(**sale.model_dump())
    db.add(new_sale)
    db.commit()
    db.refresh(new_sale)
    return new_sale

@router.get("/", response_model=List[HarvestSaleResponse])
def get_harvest_sales(skip: int = 0, limit: int = 100, batch_id: int = None, review_status: str = None, db: Session = Depends(get_db)):
    query = db.query(HarvestSale)
    if batch_id:
        query = query.filter(HarvestSale.batch_id == batch_id)
    if review_status:
        query = query.filter(HarvestSale.review_status == review_status)
    sales = query.offset(skip).limit(limit).all()
    return sales

@router.get("/{sale_id}/", response_model=HarvestSaleResponse)
def get_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")
    return sale

@router.put("/{sale_id}/", response_model=HarvestSaleResponse)
def update_harvest_sale(sale_id: int, sale: HarvestSaleUpdate, db: Session = Depends(get_db)):
    db_sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not db_sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")

    update_data = sale.model_dump(exclude_unset=True)
    lifecycle.guard_record_mutation(db, db_sale, RECORD_TYPE, update_data)

    if 'weight' in update_data or 'unit_price' in update_data:
        weight = update_data.get('weight', db_sale.weight)
        unit_price = update_data.get('unit_price', db_sale.unit_price)
        update_data['total_amount'] = weight * unit_price

    for key, value in update_data.items():
        setattr(db_sale, key, value)

    db.commit()
    db.refresh(db_sale)
    return db_sale

@router.delete("/{sale_id}/")
def delete_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    db_sale = db.query(HarvestSale).filter(HarvestSale.id == sale_id).first()
    if not db_sale:
        raise HTTPException(status_code=404, detail="出塘销售记录不存在")

    lifecycle.guard_record_mutation(db, db_sale, RECORD_TYPE, None)
    db.delete(db_sale)
    db.commit()
    return {"message": "出塘销售记录删除成功"}
