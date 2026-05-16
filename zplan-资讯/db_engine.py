"""SQLAlchemy engine: SQLite WAL + 连接健壮性；可选 PostgreSQL 大容量部署。"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from config import DB_URL


def _is_sqlite(url: str) -> bool:
    return url.strip().lower().startswith("sqlite")


def build_engine() -> Engine:
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if _is_sqlite(DB_URL):
        # 单文件可拷走备份；WAL 降低写损坏风险、提升并发读
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "5"))
        kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "15"))
        kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE", "1800"))

    engine = create_engine(DB_URL, **kwargs)

    if _is_sqlite(DB_URL):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine
