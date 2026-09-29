from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from .database import engine, Base
from .routers import ponds, batches, stocking, feeding, water_quality, medication, costs, harvest, analysis, review_queue

Base.metadata.create_all(bind=engine)

def ensure_schema():
    """为存量数据库补齐新增列（create_all 不会修改已有表）。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())

        def missing_column(table, column):
            if table not in tables:
                return False
            return column not in {c["name"] for c in inspector.get_columns(table)}

        if missing_column("batches", "closed_at"):
            conn.execute(text("ALTER TABLE batches ADD COLUMN closed_at DATETIME"))
        for table in (
            "stocking_records", "feeding_records", "water_quality_records",
            "medication_records", "cost_records", "harvest_sales",
        ):
            if missing_column(table, "review_status"):
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN review_status VARCHAR(20) NOT NULL DEFAULT 'clear'")
                )

ensure_schema()

app = FastAPI(
    title="水产养殖管理系统",
    description="一个完整的水产养殖管理系统，支持塘口管理、投苗记录、日常管理、成本核算、出塘销售和养殖周期分析",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ponds.router)
app.include_router(batches.router)
app.include_router(stocking.router)
app.include_router(feeding.router)
app.include_router(water_quality.router)
app.include_router(medication.router)
app.include_router(costs.router)
app.include_router(harvest.router)
app.include_router(analysis.router)
app.include_router(review_queue.router)

@app.get("/")
def root():
    return {
        "message": "欢迎使用水产养殖管理系统API",
        "docs": "/docs",
        "version": "1.0.0"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
