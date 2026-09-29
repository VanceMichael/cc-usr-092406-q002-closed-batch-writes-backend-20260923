from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


# 记录生命周期状态：active=生效（参与分析/结算）；flagged=历史遗留越界记录，待复核，默认排除
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_FLAGGED = "flagged"


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
    closed_at = Column(DateTime, comment="批次关闭时间（UTC）")
    current_version = Column(Integer, comment="当前冻结结算版本号，关闭时生成，更正流程裁定后递增")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pond = relationship("Pond", back_populates="batches")
    stocking_records = relationship("StockingRecord", back_populates="batch")
    feeding_records = relationship("FeedingRecord", back_populates="batch")
    water_quality_records = relationship("WaterQualityRecord", back_populates="batch")
    medication_records = relationship("MedicationRecord", back_populates="batch")
    cost_records = relationship("CostRecord", back_populates="batch")
    harvest_sales = relationship("HarvestSale", back_populates="batch")
    review_items = relationship("ReviewItem", back_populates="batch")
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
    lifecycle_status = Column(String(20), default=LIFECYCLE_ACTIVE, nullable=False,
                             server_default=LIFECYCLE_ACTIVE,
                             comment="active=生效; flagged=历史越界待复核")
    review_item_id = Column(Integer, nullable=True, comment="历史迁移收容的复核项ID")
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
    lifecycle_status = Column(String(20), default=LIFECYCLE_ACTIVE, nullable=False,
                             server_default=LIFECYCLE_ACTIVE,
                             comment="active=生效; flagged=历史越界待复核")
    review_item_id = Column(Integer, nullable=True, comment="历史迁移收容的复核项ID")
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
    lifecycle_status = Column(String(20), default=LIFECYCLE_ACTIVE, nullable=False,
                             server_default=LIFECYCLE_ACTIVE,
                             comment="active=生效; flagged=历史越界待复核")
    review_item_id = Column(Integer, nullable=True, comment="历史迁移收容的复核项ID")
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
    lifecycle_status = Column(String(20), default=LIFECYCLE_ACTIVE, nullable=False,
                             server_default=LIFECYCLE_ACTIVE,
                             comment="active=生效; flagged=历史越界待复核")
    review_item_id = Column(Integer, nullable=True, comment="历史迁移收容的复核项ID")
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
    lifecycle_status = Column(String(20), default=LIFECYCLE_ACTIVE, nullable=False,
                             server_default=LIFECYCLE_ACTIVE,
                             comment="active=生效; flagged=历史越界待复核")
    review_item_id = Column(Integer, nullable=True, comment="历史迁移收容的复核项ID")
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
    lifecycle_status = Column(String(20), default=LIFECYCLE_ACTIVE, nullable=False,
                             server_default=LIFECYCLE_ACTIVE,
                             comment="active=生效; flagged=历史越界待复核")
    review_item_id = Column(Integer, nullable=True, comment="历史迁移收容的复核项ID")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="harvest_sales")


class ReviewItem(Base):
    """越界 / 关闭后写入记录的复核队列（可恢复）。

    生命周期：pending（待裁定）→ approved（已恢复生效）/ rejected（拒绝并撤销）。
    approved/rejected 项保留不删，作为审计轨迹。
    """
    __tablename__ = "review_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    record_type = Column(String(30), nullable=False, comment="记录类型: stocking/feeding/water_quality/medication/cost/harvest_sale")
    operation = Column(String(10), nullable=False, comment="操作: create/update/delete")
    record_id = Column(Integer, comment="关联记录ID；delete 操作在撤销后可能为空")
    reason = Column(String(30), nullable=False, comment="收容原因: before_stocking/after_harvest/outside_backfill/batch_closed")
    business_date = Column(Date, comment="业务发生日")
    # 待应用的数据：create/update 为字段字典，delete 为删除前快照
    payload_json = Column(Text, nullable=False)
    status = Column(String(20), default="pending", nullable=False, index=True, comment="pending/approved/rejected")
    resolution_note = Column(Text)
    resolved_by = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime)

    batch = relationship("Batch", back_populates="review_items")

    @property
    def payload(self):
        import json
        return json.loads(self.payload_json) if self.payload_json else None

    __table_args__ = (
        Index("ix_review_items_batch_status", "batch_id", "status"),
    )


class SettlementVersion(Base):
    """批次结算冻结版本。关闭时生成 v1；关闭后更正流程每批次完成一轮裁定生成新版本。"""
    __tablename__ = "settlement_versions"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False, comment="批次内递增版本号，从1开始")
    trigger = Column(String(20), nullable=False, comment="close/correction")
    snapshot_json = Column(Text, nullable=False, comment="冻结时全部生效记录与分析指标快照")
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(100))

    batch = relationship("Batch", back_populates="settlement_versions")

    __table_args__ = (
        UniqueConstraint("batch_id", "version", name="uq_settlement_batch_version"),
    )
