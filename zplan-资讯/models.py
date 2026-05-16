from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from db_engine import build_engine


class Base(DeclarativeBase):
    pass


class StockList(Base):
    __tablename__ = "stock_list"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    industry: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    listing_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)


class DailyPrice(Base):
    """兼容层：请以 ``zplan_shared.models.DailyPrice`` 为准，见 zplan-共享/docs/DATA_ARCHITECTURE.md。"""

    __tablename__ = "daily_prices"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_ts_trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amplitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_chg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    change_amt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    adjust_type: Mapped[str] = mapped_column(String(8), nullable=False, default="qfq")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="akshare_em")
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NewsFeed(Base):
    __tablename__ = "news_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class TopicConfig(Base):
    __tablename__ = "topic_configs"
    __table_args__ = (UniqueConstraint("topic_key", name="uq_topic_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class NewsRun(Base):
    __tablename__ = "news_runs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_news_runs_dedupe_key"),
        UniqueConstraint("topic_key", "window_start", "window_end", name="uq_news_runs_window"),
        Index("ix_news_runs_topic_created", "topic_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)


class NewsItemRaw(Base):
    __tablename__ = "news_items_raw"
    __table_args__ = (
        UniqueConstraint("source", "post_id", name="uq_news_items_source_post"),
        Index("ix_news_items_run_published", "run_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    post_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class FinancialIndicator(Base):
    __tablename__ = "financial_indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    report_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    pe_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_indexes()


def _ensure_sqlite_indexes() -> None:
    """已有库上补建索引（create_all 不会改旧表结构）。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_news_runs_topic_created ON news_runs (topic_key, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_news_items_run_published ON news_items_raw (run_id, published_at)",
    ]
    with engine.begin() as conn:
        for sql in stmts:
            conn.execute(text(sql))
