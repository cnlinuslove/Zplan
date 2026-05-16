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

from zplan_shared.db_engine import build_engine


class Base(DeclarativeBase):
    pass


class StockList(Base):
    __tablename__ = "stock_list"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    industry: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    listing_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)


class DailyPrice(Base):
    """A 股日线 OHLCV + 衍生字段（Phase A）。架构说明见 ``docs/DATA_ARCHITECTURE.md``。"""

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


class FinancialAlert(Base):
    """国内快讯（AkShare 东方财富 `stock_info_global_em`）。"""

    __tablename__ = "financial_alerts"
    __table_args__ = (UniqueConstraint("url_hash", name="uq_financial_alerts_url_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(48), nullable=False, default="eastmoney_flash")
    published_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    article_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class MarketSentiment(Base):
    """量化情绪因子：北向、两融账户、指数换手率等（均为东财系接口）。"""

    __tablename__ = "market_sentiment"
    __table_args__ = (
        UniqueConstraint(
            "factor_kind",
            "as_of_utc",
            "subject",
            "metric_name",
            name="uq_market_sentiment_point",
        ),
        Index("ix_market_sentiment_kind_asof", "factor_kind", "as_of_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    as_of_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extra_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class GlobalNews(Base):
    """NewsAPI 与 Google News RSS 统一入库。"""

    __tablename__ = "global_news"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_global_news_url_hash"),
        Index("ix_global_news_channel_published", "channel", "published_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    published_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    article_url: Mapped[str] = mapped_column(Text, nullable=False)
    rss_keyword: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_daily_prices_phase_a()
    _ensure_sqlite_indexes()


def _migrate_daily_prices_phase_a() -> None:
    """已有库补列（create_all 不修改旧表）。Phase A 字段见 DATA_ARCHITECTURE.md。"""
    url = str(engine.url)
    additions: list[tuple[str, str]] = [
        ("amplitude", "REAL"),
        ("pct_chg", "REAL"),
        ("change_amt", "REAL"),
        ("turnover_rate", "REAL"),
        ("adjust_type", "VARCHAR(8) NOT NULL DEFAULT 'qfq'"),
        ("source", "VARCHAR(32) NOT NULL DEFAULT 'akshare_em'"),
        ("ingested_at", "DATETIME"),
    ]
    if url.startswith("sqlite"):
        with engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(daily_prices)")).fetchall()
            }
            for col, ddl in additions:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE daily_prices ADD COLUMN {col} {ddl}"))
        return
    if url.startswith("postgresql"):
        pg_types = {
            "amplitude": "DOUBLE PRECISION",
            "pct_chg": "DOUBLE PRECISION",
            "change_amt": "DOUBLE PRECISION",
            "turnover_rate": "DOUBLE PRECISION",
            "adjust_type": "VARCHAR(8) NOT NULL DEFAULT 'qfq'",
            "source": "VARCHAR(32) NOT NULL DEFAULT 'akshare_em'",
            "ingested_at": "TIMESTAMP",
        }
        with engine.begin() as conn:
            existing = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'daily_prices'"
                    )
                ).fetchall()
            }
            for col, ddl in pg_types.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE daily_prices ADD COLUMN {col} {ddl}"))


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
