from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from pathlib import Path

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./aquaculture.db"
)

if SQLALCHEMY_DATABASE_URL.startswith("sqlite:///"):
    db_path = SQLALCHEMY_DATABASE_URL.replace("sqlite:///", "")
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    # SQLite 默认是延迟事务：读事务升级到写事务时，若期间有其他连接提交过写入，
    # 会基于过期快照写入甚至死锁。这里让所有事务以 BEGIN IMMEDIATE 开始，
    # 事务的第一条语句即取得数据库级写锁，配合 busy timeout 串行化并发写入，
    # 保证“校验批次状态 -> 写入记录”与“关闭批次”之间不会出现部分成功。
    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
