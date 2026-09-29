from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import os
from pathlib import Path
from .config import BUSY_TIMEOUT_MS


def create_sqlite_engines(database_url: str):
    """构造 (reader_engine, writer_engine)；内存库下两者为同一引擎。

    writer 引擎用 autocommit 连接 + begin 事件手工发出 BEGIN IMMEDIATE，
    保证关闭与记录写入在同一把写锁上严格串行。
    """
    is_memory = ":memory:" in database_url

    if not is_memory:
        db_path = database_url.split("sqlite:///", 1)[1]
        db_dir = Path(db_path).parent
        if str(db_dir) not in ("", "."):
            db_dir.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000}
    poolclass = StaticPool if is_memory else None

    writer = create_engine(
        database_url, connect_args=connect_args, poolclass=poolclass,
        isolation_level=None,
    )

    @event.listens_for(writer, "connect")
    def _set_writer_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if not is_memory:
            # WAL：读者不阻塞写者，写者之间在提交点串行，配合 IMMEDIATE 写事务消除关闭竞态。
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(writer, "begin")
    def _begin_immediate(conn):
        # 2.0 的 begin 事件拿到的是 Engine 层 Connection，用 exec_driver_sql 发原生语句。
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    if is_memory:
        # 内存库只有一个共享连接：读写共用同一引擎，否则两个引擎看到不同的内存库。
        return writer, writer

    reader = create_engine(database_url, connect_args=connect_args, poolclass=poolclass)

    @event.listens_for(reader, "connect")
    def _set_reader_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return reader, writer


SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./aquaculture.db"
)

_is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")
_is_memory = ":memory:" in SQLALCHEMY_DATABASE_URL

if _is_sqlite:
    engine, writer_engine = create_sqlite_engines(SQLALCHEMY_DATABASE_URL)
else:  # pragma: no cover - 本项目存储约定为 SQLite
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
    writer_engine = engine

SessionLocal = sessionmaker(
    autocommit=False, autoflush=False,
    # 内存库只有一个共享连接，读写同引擎，否则两个引擎看到不同的内存库。
    bind=writer_engine if _is_memory else engine,
)
# 写会话：所有批次生命周期变更与业务记录写入都走该工厂（BEGIN IMMEDIATE）。
WriterSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=writer_engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
