from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base

#: 业务记录的复核状态取值
REVIEW_STATUS_CLEAR = "clear"        # 正常记录，参与分析与追溯
REVIEW_STATUS_PENDING = "pending"    # 待裁定（越界隔离或关闭后更正），默认排除
REVIEW_STATUS_APPROVED = "approved"  # 复核通过，参与分析与追溯
REVIEW_STATUS_REJECTED = "rejected"  # 复核驳回，持续排除

REVIEW_STATUSES = (
    REVIEW_STATUS_CLEAR,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_REJECTED,
)

class Pond(Base):
    __tablename__ = "ponds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    area = Column(Float, nullable=False, comment="面积(亩)")
    water_depth = Column(Float, nullable=False, comment="水深(米)")
    species = Column(String(100), comment="养殖品种")
    status = Column(String(20), default="active", comment="状态: active, inactive")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batches = relationship("Batch", back_populates="pond")

class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_number = Column(String(50), unique=True, index=True, nullable=False, comment="批次号")
    pond_id = Column(Integer, ForeignKey("ponds.id"), nullable=False)
    species = Column(String(100), nullable=False, comment="养殖品种")
    stocking_date = Column(Date, nullable=False, comment="放苗日期")
    estimated_harvest_date = Column(Date, comment="预计收获日期")
    actual_harvest_date = Column(Date, comment="实际收获日期")
    status = Column(String(20), default="active", comment="状态: active, harvested, closed")
    closed_at = Column(DateTime, comment="关闭时间(结算冻结点)")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pond = relationship("Pond", back_populates="batches")
    stocking_records = relationship("StockingRecord", back_populates="batch")
    feeding_records = relationship("FeedingRecord", back_populates="batch")
    water_quality_records = relationship("WaterQualityRecord", back_populates="batch")
    medication_records = relationship("MedicationRecord", back_populates="batch")
    cost_records = relationship("CostRecord", back_populates="batch")
    harvest_sales = relationship("HarvestSale", back_populates="batch")
    settlement_versions = relationship("SettlementVersion", back_populates="batch")

class StockingRecord(Base):
    __tablename__ = "stocking_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    species = Column(String(100), nullable=False, comment="品种")
    quantity = Column(Integer, nullable=False, comment="数量(尾)")
    source = Column(String(200), comment="来源")
    batch_number = Column(String(50), comment="苗种批次号")
    weight_per_unit = Column(Float, comment="单重(克/尾)")
    total_weight = Column(Float, comment="总重量(公斤)")
    notes = Column(Text, comment="备注")
    review_status = Column(String(20), default=REVIEW_STATUS_CLEAR, nullable=False, index=True, comment="复核状态: clear, pending, approved, rejected")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="stocking_records")

class FeedingRecord(Base):
    __tablename__ = "feeding_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    feeding_date = Column(Date, nullable=False, comment="投喂日期")
    feed_type = Column(String(100), nullable=False, comment="饲料类型")
    feed_quantity = Column(Float, nullable=False, comment="投喂量(公斤)")
    feeding_time = Column(String(20), comment="投喂时间")
    weather = Column(String(50), comment="天气情况")
    water_temperature = Column(Float, comment="水温(℃)")
    notes = Column(Text, comment="备注")
    review_status = Column(String(20), default=REVIEW_STATUS_CLEAR, nullable=False, index=True, comment="复核状态: clear, pending, approved, rejected")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="feeding_records")

class WaterQualityRecord(Base):
    __tablename__ = "water_quality_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    record_date = Column(Date, nullable=False, comment="检测日期")
    record_time = Column(String(20), comment="检测时间")
    water_temperature = Column(Float, comment="水温(℃)")
    ph_value = Column(Float, comment="pH值")
    dissolved_oxygen = Column(Float, comment="溶解氧(mg/L)")
    ammonia_nitrogen = Column(Float, comment="氨氮(mg/L)")
    nitrite = Column(Float, comment="亚硝酸盐(mg/L)")
    transparency = Column(Float, comment="透明度(cm)")
    notes = Column(Text, comment="备注")
    review_status = Column(String(20), default=REVIEW_STATUS_CLEAR, nullable=False, index=True, comment="复核状态: clear, pending, approved, rejected")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="water_quality_records")

class MedicationRecord(Base):
    __tablename__ = "medication_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    medication_date = Column(Date, nullable=False, comment="用药日期")
    drug_name = Column(String(200), nullable=False, comment="药品名称")
    drug_type = Column(String(50), comment="药品类型")
    dosage = Column(Float, comment="用量")
    dosage_unit = Column(String(20), default="kg", comment="用量单位")
    administration_method = Column(String(100), comment="施用方法")
    purpose = Column(String(200), comment="用途")
    manufacturer = Column(String(200), comment="生产厂家")
    batch_number = Column(String(50), comment="药品批次号")
    notes = Column(Text, comment="备注")
    review_status = Column(String(20), default=REVIEW_STATUS_CLEAR, nullable=False, index=True, comment="复核状态: clear, pending, approved, rejected")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="medication_records")

class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    cost_date = Column(Date, nullable=False, comment="费用日期")
    cost_type = Column(String(50), nullable=False, comment="费用类型: feed, medicine, labor, electricity, other")
    amount = Column(Float, nullable=False, comment="金额(元)")
    description = Column(String(500), comment="费用描述")
    quantity = Column(Float, comment="数量")
    unit = Column(String(20), comment="单位")
    unit_price = Column(Float, comment="单价")
    notes = Column(Text, comment="备注")
    review_status = Column(String(20), default=REVIEW_STATUS_CLEAR, nullable=False, index=True, comment="复核状态: clear, pending, approved, rejected")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="cost_records")

class HarvestSale(Base):
    __tablename__ = "harvest_sales"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    sale_date = Column(Date, nullable=False, comment="销售日期")
    weight = Column(Float, nullable=False, comment="重量(公斤)")
    unit_price = Column(Float, nullable=False, comment="单价(元/公斤)")
    total_amount = Column(Float, comment="总金额(元)")
    buyer = Column(String(200), comment="买家")
    batch_number = Column(String(50), comment="追溯批次号")
    quality_grade = Column(String(50), comment="质量等级")
    notes = Column(Text, comment="备注")
    review_status = Column(String(20), default=REVIEW_STATUS_CLEAR, nullable=False, index=True, comment="复核状态: clear, pending, approved, rejected")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="harvest_sales")

class SettlementVersion(Base):
    """批次关闭时冻结的结算版本。每个批次至多一个版本，request_id 为幂等键。"""

    __tablename__ = "settlement_versions"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), unique=True, nullable=False, index=True)
    version_no = Column(Integer, nullable=False, default=1, comment="结算版本号")
    request_id = Column(String(64), unique=True, nullable=False, index=True, comment="关闭请求幂等键")
    closed_by = Column(String(100), comment="关闭操作人")
    note = Column(Text, comment="关闭备注")
    closed_at = Column(DateTime, nullable=False, comment="关闭时间")
    quarantined_count = Column(Integer, nullable=False, default=0, comment="关闭时隔离的越界记录数")
    snapshot = Column(Text, nullable=False, comment="冻结的周期分析与追溯快照(JSON)")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="settlement_versions")

class ReviewQueueItem(Base):
    """可恢复的复核队列：历史越界/关闭后写入的记录在此等待裁定。"""

    __tablename__ = "review_queue_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    record_type = Column(String(30), nullable=False, comment="记录类型: stocking, feeding, water_quality, medication, cost, harvest_sale")
    record_id = Column(Integer, comment="关联记录主键")
    operation = Column(String(20), nullable=False, comment="操作: quarantine, create, update, delete")
    reason = Column(String(50), nullable=False, comment="入队原因: out_of_cycle_bounds, post_close_correction")
    payload = Column(Text, comment="记录快照或拟变更内容(JSON)")
    status = Column(String(20), default="pending", nullable=False, index=True, comment="状态: pending, approved, rejected")
    submitted_by = Column(String(100), comment="提交人")
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, comment="裁定时间")
    decided_by = Column(String(100), comment="裁定人")
    decision_note = Column(Text, comment="裁定备注")
