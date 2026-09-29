"""批次生命周期相关配置。"""
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 关闭前允许的补录窗口（天）：业务发生日距今天数 <= 该值才允许直接写入。
BACKFILL_WINDOW_DAYS = _int_env("BACKFILL_WINDOW_DAYS", 7)

# 等待 SQLite 写锁的最长时间（毫秒），配合 BEGIN IMMEDIATE 串行化关闭与写入。
BUSY_TIMEOUT_MS = _int_env("BUSY_TIMEOUT_MS", 10000)
