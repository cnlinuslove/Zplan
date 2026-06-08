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
    __table_args__ = (UniqueConstraint("ts_code", "market", name="uq_stock_list_ts_market"),)

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    industry: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    listing_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")


class DailyPrice(Base):
    """A 股 / 港股日线 OHLCV + 衍生字段（Phase A）。架构说明见 ``docs/DATA_ARCHITECTURE.md``。"""

    __tablename__ = "daily_prices"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", "market", name="uq_ts_trade_date_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
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


class DailyFeature(Base):
    """技术指标日频快照（Phase A.3，每票每个交易日一行）。"""

    __tablename__ = "daily_features"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", "market", name="uq_daily_features_ts_date_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma5: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma10: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma20: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma60: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    macd_dif: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    macd_dea: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    macd_hist: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rsi14: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kdj_k: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kdj_d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kdj_j: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atr14: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atr_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_5d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_20d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_60d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vol_ratio20: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close_vs_ma20: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma20_slope_5d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high_60d_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    drawdown_20d_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_chg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    above_ma20: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma5_cross_ma20: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    macd_cross_up: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vol_breakout: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kdj_golden_cross: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kdj_death_cross: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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


class DailySnapshot(Base):
    """日频估值 / 市值截面（Phase B）。"""

    __tablename__ = "daily_snapshot"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", "market", name="uq_snapshot_ts_date_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    pe_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ps_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_mv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    circ_mv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="akshare_em")
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DailyChip(Base):
    """筹码峰日频数据（东方财富 CYQ，90 日区间）。

    每票每天一行，记录该交易日收盘后的筹码分布状态。
    数据源：``ak.stock_cyq_em(symbol, adjust="qfq")``。
    """

    __tablename__ = "daily_chip"
    __table_args__ = (
        UniqueConstraint("ts_code", "trade_date", "market", name="uq_daily_chip_ts_date_market"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    profit_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="获利比例 (%)")
    avg_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="平均成本 (元)")
    cost_90_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="90%成本-低")
    cost_90_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="90%成本-高")
    concentration_90: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="90集中度")
    cost_70_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="70%成本-低")
    cost_70_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="70%成本-高")
    concentration_70: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="70集中度")
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class FinancialIndicator(Base):
    __tablename__ = "financial_indicators"
    __table_args__ = (UniqueConstraint("ts_code", "report_date", "market", name="uq_fi_ts_report_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    report_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    pe_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="akshare_sina")
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


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


class NewsStockLink(Base):
    """新闻 ↔ 个股关联（选股 / 资讯共用，替代 title LIKE '%代码%'）。"""

    __tablename__ = "news_stock_link"
    __table_args__ = (
        UniqueConstraint("news_source", "news_id", "ts_code", "market", name="uq_news_stock_link_market"),
        Index("ix_news_stock_link_ts_pub", "ts_code", "published_at_utc"),
        Index("ix_news_stock_link_source_news", "news_source", "news_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_source: Mapped[str] = mapped_column(String(32), nullable=False)
    news_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    matched_by: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    published_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
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


class StockConceptMember(Base):
    """东财概念板块成份（按需同步，供题材筛选）。"""

    __tablename__ = "stock_concept_members"
    __table_args__ = (
        UniqueConstraint("concept_name", "ts_code", "market", name="uq_stock_concept_member_market"),
        Index("ix_stock_concept_name", "concept_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    concept_name: Mapped[str] = mapped_column(String(128), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    synced_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class PickWatchlist(Base):
    """用户持仓 / 关注订阅（每日简报）。"""

    __tablename__ = "pick_watchlist"
    __table_args__ = (UniqueConstraint("ts_code", "market", name="uq_pick_watchlist_ts_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_sync_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_brief_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class StockRuleScore(Base):
    """全市场规则引擎打分快照（按交易日 + rule_version + market 唯一）。"""

    __tablename__ = "stock_rule_scores"
    __table_args__ = (
        UniqueConstraint(
            "ts_code",
            "trade_date_as_of",
            "rule_version",
            "market",
            name="uq_stock_rule_scores_market",
        ),
        Index(
            "ix_stock_rule_scores_date_composite",
            "trade_date_as_of",
            "composite_score",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    trade_date_as_of: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    tech_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    composite_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rank_by_composite: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    signals_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    features_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PickRun(Base):
    """选股 Agent 一次运行（扫描 / 单票研报 / 批量）。"""

    __tablename__ = "pick_runs"
    __table_args__ = (Index("ix_pick_runs_kind_created", "run_kind", "created_at_utc"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date_as_of: Mapped[Optional[Date]] = mapped_column(Date, nullable=True, index=True)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    llm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    llm_model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    symbol_query: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    params_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class PickEntry(Base):
    """单次运行内每只标的的打分、分析过程与研报快照。"""

    __tablename__ = "pick_entries"
    __table_args__ = (
        Index("ix_pick_entries_run_rank", "run_id", "rank_in_run"),
        Index("ix_pick_entries_ts_created", "ts_code", "created_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pick_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    rank_in_run: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rule_tech_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rule_composite_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_composite_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_technical_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_financial_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_news_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    final_composite_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recommendation: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    analysis_process_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    predicted_buy_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    predicted_target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    predicted_stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class PickPredictionOutcome(Base):
    """选股预测价事后验证结果。"""

    __tablename__ = "pick_prediction_outcomes"
    __table_args__ = (
        UniqueConstraint("entry_id", "horizon_days", name="uq_pick_outcome_entry_horizon"),
        Index("ix_pick_outcome_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pick_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    as_of_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    predicted_buy_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    predicted_target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    predicted_stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close_at_as_of: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    next_open: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close_at_horizon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buy_touched: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    target_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    stop_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    buy_gap_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_from_buy_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_from_close_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    horizon_start: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    horizon_end: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    evaluated_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )



class PickLlmEvaluation(Base):
    """LLM 选股 Top 池事后评估与失败标签。"""

    __tablename__ = "pick_llm_evaluations"
    __table_args__ = (UniqueConstraint("entry_id", name="uq_pick_llm_eval_entry"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pick_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    rank_in_run: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    as_of_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    llm_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rule_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_20d_at_pick: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close_vs_buy_gap_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_from_close_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    failure_tags_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_trend: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recommendation: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    evaluated_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class PatternEvent(Base):
    """模式学习标签 — 历史极值点及其未来走势分类。

    每个 (ts_code, event_date, event_type, horizon_days, market) 唯一。
    """

    __tablename__ = "pattern_events"
    __table_args__ = (
        UniqueConstraint(
            "ts_code", "event_date", "event_type", "horizon_days", "market",
            name="uq_pattern_events_market",
        ),
        Index("ix_pattern_events_label", "label"),
        Index("ix_pattern_events_ts_code", "ts_code"),
        Index("ix_pattern_events_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    event_date: Mapped[Date] = mapped_column(Date, nullable=False)
    event_type: Mapped[str] = mapped_column(String(8), nullable=False)
    formation_start: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    label_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    runup_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forward_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close_at_event: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atr_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    horizon_end_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    extra_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class PatternPrediction(Base):
    """模式模型推理结果 — 每票每日的模式判断快照。"""

    __tablename__ = "pattern_predictions"
    __table_args__ = (
        UniqueConstraint(
            "ts_code", "trade_date", "model_version", "approach", "market",
            name="uq_pattern_predictions_market",
        ),
        Index("ix_pattern_predictions_date", "trade_date"),
        Index("ix_pattern_predictions_class", "pattern_class"),
        Index("ix_pattern_predictions_model", "model_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    approach: Mapped[str] = mapped_column(String(8), nullable=False)
    event_type: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    pattern_class: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pattern_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    proba_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    features_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    formation_start: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class AhCrossRef(Base):
    """A+H 股跨市场对照表（同一公司在两个市场上市）。"""

    __tablename__ = "ah_cross_ref"
    __table_args__ = (
        UniqueConstraint("a_code", "hk_code", name="uq_ah_pair"),
        Index("ix_ah_a_code", "a_code"),
        Index("ix_ah_hk_code", "hk_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    a_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    a_name: Mapped[str] = mapped_column(String(64), nullable=False)
    hk_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    hk_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 最新 AH 溢价（H 股相对 A 股，%）：正 = H 股溢价，负 = A 股溢价
    ah_premium_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    a_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hk_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    premium_as_of: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ChatHistory(Base):
    """企微/微信对话历史记录 — 用于回复质量审计与持续优化。"""

    __tablename__ = "chat_history"
    __table_args__ = (
        Index("ix_chat_history_created", "created_at_utc"),
        Index("ix_chat_history_channel_intent", "channel", "bot_intent"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    user_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    chat_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    bot_intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    bot_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


# ── Web 对话模型（zplan-web 专用）──────────────────────────────


class WebChatSession(Base):
    """Web 聊天会话（独立于企微的 in-memory session）。"""

    __tablename__ = "web_chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class WebChatMessage(Base):
    """Web 聊天消息（含 token 用量追踪）。"""

    __tablename__ = "web_chat_messages"
    __table_args__ = (
        Index("ix_web_chat_msg_session_created", "session_id", "created_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("web_chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )


class LlmResponseCache(Base):
    """LLM 响应缓存——相同 prompt hash 命中时直接返回，节省 API 费用。"""

    __tablename__ = "llm_response_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _migrate_web_chat() -> None:
    """SQLite 旧库补建 web_chat 相关表。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "web_chat_sessions" not in existing:
            conn.execute(
                text(
                    """CREATE TABLE web_chat_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title VARCHAR(128),
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at_utc DATETIME NOT NULL DEFAULT (datetime('now')),
                        updated_at_utc DATETIME NOT NULL DEFAULT (datetime('now'))
                    )"""
                )
            )
        if "web_chat_messages" not in existing:
            conn.execute(
                text(
                    """CREATE TABLE web_chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL REFERENCES web_chat_sessions(id) ON DELETE CASCADE,
                        role VARCHAR(16) NOT NULL,
                        content TEXT NOT NULL,
                        intent VARCHAR(32),
                        prompt_tokens INTEGER,
                        output_tokens INTEGER,
                        cost_usd FLOAT,
                        elapsed_ms INTEGER,
                        created_at_utc DATETIME NOT NULL DEFAULT (datetime('now'))
                    )"""
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_chat_msg_session_created "
                    "ON web_chat_messages (session_id, created_at_utc)"
                )
            )
        if "llm_response_cache" not in existing:
            conn.execute(
                text(
                    """CREATE TABLE llm_response_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cache_key VARCHAR(128) UNIQUE NOT NULL,
                        response_json TEXT NOT NULL,
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        created_at_utc DATETIME NOT NULL DEFAULT (datetime('now')),
                        ttl_seconds INTEGER NOT NULL DEFAULT 3600
                    )"""
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_llm_cache_key ON llm_response_cache (cache_key)")
            )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_daily_prices_phase_a()
    _migrate_financial_indicators_phase_d()
    _migrate_pick_entries_predictions()
    _migrate_pattern_tables()
    _migrate_chat_history()
    _migrate_hk_market_column()
    _migrate_ah_cross_ref()
    _migrate_daily_chip()
    _migrate_web_chat()
    _ensure_sqlite_indexes()
    _ensure_news_stock_link_table()


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




def _migrate_pattern_tables() -> None:
    """预留：形态识别表迁移（Phase E）。当前无操作。"""


def _migrate_pick_entries_predictions() -> None:
    """pick_entries 补预测价列（已有库兼容）。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    additions = [
        ("predicted_buy_price", "REAL"),
        ("predicted_target_price", "REAL"),
        ("predicted_stop_loss", "REAL"),
        ("price_source", "VARCHAR(16)"),
    ]
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(pick_entries)")).fetchall()
        }
        if not existing:
            return
        for col, ddl in additions:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE pick_entries ADD COLUMN {col} {ddl}"))

def _migrate_financial_indicators_phase_d() -> None:
    """Phase D：financial_indicators 补列。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    additions = [
        ("roe", "REAL"),
        ("source", "VARCHAR(32) NOT NULL DEFAULT 'akshare_sina'"),
        ("ingested_at", "DATETIME"),
    ]
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(financial_indicators)")).fetchall()
        }
        if not existing:
            return
        for col, ddl in additions:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE financial_indicators ADD COLUMN {col} {ddl}"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_fi_ts_report "
                "ON financial_indicators (ts_code, report_date)"
            )
        )


def _migrate_pattern_tables() -> None:
    """Phase E：pattern_events / pattern_predictions 表（模式学习）。

    create_all 对新库直接建表；此函数为旧库补建。
    """
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing_tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "pattern_events" not in existing_tables:
            conn.execute(
                text(
                    """CREATE TABLE pattern_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_code VARCHAR(16) NOT NULL,
                        event_date DATE NOT NULL,
                        event_type VARCHAR(8) NOT NULL,
                        formation_start DATE,
                        horizon_days INTEGER NOT NULL,
                        label VARCHAR(20),
                        label_confidence FLOAT,
                        runup_pct FLOAT,
                        forward_return FLOAT,
                        close_at_event FLOAT,
                        atr_pct FLOAT,
                        horizon_end_date DATE,
                        extra_json TEXT,
                        created_at DATETIME NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(ts_code, event_date, event_type, horizon_days)
                    )"""
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pattern_events_label "
                    "ON pattern_events (label)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pattern_events_ts_code "
                    "ON pattern_events (ts_code)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pattern_events_date "
                    "ON pattern_events (event_date)"
                )
            )
        if "pattern_predictions" not in existing_tables:
            conn.execute(
                text(
                    """CREATE TABLE pattern_predictions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_code VARCHAR(16) NOT NULL,
                        trade_date DATE NOT NULL,
                        model_version VARCHAR(32) NOT NULL,
                        approach VARCHAR(8) NOT NULL,
                        event_type VARCHAR(8),
                        pattern_class VARCHAR(20),
                        pattern_score FLOAT,
                        proba_json TEXT,
                        features_json TEXT,
                        formation_start DATE,
                        created_at DATETIME NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(ts_code, trade_date, model_version, approach)
                    )"""
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pattern_predictions_date "
                    "ON pattern_predictions (trade_date)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pattern_predictions_class "
                    "ON pattern_predictions (pattern_class)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pattern_predictions_model "
                    "ON pattern_predictions (model_version)"
                )
            )
        if "concept_product_cache" not in existing_tables:
            conn.execute(
                text(
                    """CREATE TABLE concept_product_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_code VARCHAR(16) NOT NULL,
                        concept_name VARCHAR(128) NOT NULL,
                        product_summary TEXT NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(ts_code, concept_name)
                    )"""
                )
            )


def _migrate_chat_history() -> None:
    """已有库补建 chat_history 表（create_all 对新库直接建表）。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "chat_history" not in existing:
            conn.execute(
                text(
                    """CREATE TABLE chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel VARCHAR(32) NOT NULL DEFAULT 'unknown',
                        user_id VARCHAR(128),
                        chat_id VARCHAR(128),
                        user_message TEXT NOT NULL,
                        bot_intent VARCHAR(32),
                        bot_reply TEXT,
                        error TEXT,
                        elapsed_ms INTEGER,
                        created_at_utc DATETIME NOT NULL DEFAULT (datetime('now'))
                    )"""
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_history_created "
                    "ON chat_history (created_at_utc)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_history_channel_intent "
                    "ON chat_history (channel, bot_intent)"
                )
            )


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


def _ensure_news_stock_link_table() -> None:
    """SQLite 旧库补建 news_stock_link（create_all 对新表有效，此处兜底索引）。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_news_stock_link_ts_pub ON news_stock_link (ts_code, published_at_utc)",
        "CREATE INDEX IF NOT EXISTS ix_news_stock_link_source_news ON news_stock_link (news_source, news_id)",
    ]
    with engine.begin() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
            except Exception:
                pass


def _migrate_hk_market_column() -> None:
    """港股支持：已有库补 ``market`` 列（默认 'a'），并更新唯一约束。

    所有带 ``market`` 列的表均在此迁移中处理。
    """
    url = str(engine.url)

    # 需要补 market 列的表 → (表名, 列类型)
    tables_with_market: list[tuple[str, str]] = [
        ("stock_list", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("daily_prices", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("daily_features", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("daily_snapshot", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("financial_indicators", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("news_stock_link", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("stock_concept_members", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("pick_watchlist", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("stock_rule_scores", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("pick_runs", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("pick_entries", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("pick_llm_evaluations", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("pattern_events", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
        ("pattern_predictions", "VARCHAR(8) NOT NULL DEFAULT 'a'"),
    ]

    # 旧约束名 → 新约束名（用于重建唯一索引）
    constraint_rename_map = {
        "uq_ts_trade_date": ("daily_prices", "uq_ts_trade_date_market",
                              "ts_code, trade_date", "ts_code, trade_date, market"),
        "uq_daily_features_ts_date": ("daily_features", "uq_daily_features_ts_date_market",
                                       "ts_code, trade_date", "ts_code, trade_date, market"),
        "uq_snapshot_ts_date": ("daily_snapshot", "uq_snapshot_ts_date_market",
                                 "ts_code, trade_date", "ts_code, trade_date, market"),
        "uq_fi_ts_report": ("financial_indicators", "uq_fi_ts_report_market",
                             "ts_code, report_date", "ts_code, report_date, market"),
        "uq_stock_concept_member": ("stock_concept_members", "uq_stock_concept_member_market",
                                     "concept_name, ts_code", "concept_name, ts_code, market"),
        "uq_pick_watchlist_ts_code": ("pick_watchlist", "uq_pick_watchlist_ts_market",
                                       "ts_code", "ts_code, market"),
        "uq_stock_rule_scores": ("stock_rule_scores", "uq_stock_rule_scores_market",
                                  "ts_code, trade_date_as_of, rule_version",
                                  "ts_code, trade_date_as_of, rule_version, market"),
        "uq_news_stock_link": ("news_stock_link", "uq_news_stock_link_market",
                                "news_source, news_id, ts_code",
                                "news_source, news_id, ts_code, market"),
        "uq_pattern_events": ("pattern_events", "uq_pattern_events_market",
                               "ts_code, event_date, event_type, horizon_days",
                               "ts_code, event_date, event_type, horizon_days, market"),
        "uq_pattern_predictions": ("pattern_predictions", "uq_pattern_predictions_market",
                                    "ts_code, trade_date, model_version, approach",
                                    "ts_code, trade_date, model_version, approach, market"),
    }

    if url.startswith("sqlite"):
        with engine.begin() as conn:
            # 1) 补列
            for table_name, col_ddl in tables_with_market:
                try:
                    existing_cols = {
                        row[1]
                        for row in conn.execute(
                            text(f"PRAGMA table_info({table_name})")
                        ).fetchall()
                    }
                except Exception:
                    continue  # 表不存在则跳过
                if "market" not in existing_cols:
                    try:
                        conn.execute(
                            text(f"ALTER TABLE {table_name} ADD COLUMN market {col_ddl}")
                        )
                    except Exception:
                        pass

            # 2) 更新唯一约束：删旧索引，建新索引
            existing_indexes = {
                row[1]
                for row in conn.execute(
                    text("SELECT type, name FROM sqlite_master WHERE type='index'")
                ).fetchall()
            }
            for old_name, (table, new_name, old_cols, new_cols) in constraint_rename_map.items():
                if old_name in existing_indexes:
                    try:
                        conn.execute(text(f"DROP INDEX IF EXISTS {old_name}"))
                    except Exception:
                        pass
                if new_name not in existing_indexes:
                    try:
                        conn.execute(
                            text(
                                f"CREATE UNIQUE INDEX IF NOT EXISTS {new_name} "
                                f"ON {table} ({new_cols})"
                            )
                        )
                    except Exception:
                        pass
        return

    if url.startswith("postgresql"):
        with engine.begin() as conn:
            for table_name, _col_ddl in tables_with_market:
                pg_col_type = "VARCHAR(8) NOT NULL DEFAULT 'a'"
                try:
                    existing_cols = {
                        row[0]
                        for row in conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                f"WHERE table_name = '{table_name}'"
                            )
                        ).fetchall()
                    }
                except Exception:
                    continue
                if "market" not in existing_cols:
                    try:
                        conn.execute(
                            text(
                                f"ALTER TABLE {table_name} ADD COLUMN market {pg_col_type}"
                            )
                        )
                    except Exception:
                        pass

            for old_name, (table, new_name, old_cols, new_cols) in constraint_rename_map.items():
                try:
                    conn.execute(
                        text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old_name}")
                    )
                except Exception:
                    pass
                try:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table} ADD CONSTRAINT {new_name} "
                            f"UNIQUE ({new_cols})"
                        )
                    )
                except Exception:
                    pass


def _migrate_ah_cross_ref() -> None:
    """SQLite 旧库补建 ah_cross_ref 表。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "ah_cross_ref" not in existing:
            conn.execute(
                text(
                    """CREATE TABLE ah_cross_ref (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        a_code VARCHAR(16) NOT NULL,
                        a_name VARCHAR(64) NOT NULL,
                        hk_code VARCHAR(16) NOT NULL,
                        hk_name VARCHAR(64) NOT NULL,
                        ah_premium_pct FLOAT,
                        a_close FLOAT,
                        hk_close FLOAT,
                        premium_as_of DATE,
                        updated_at DATETIME NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(a_code, hk_code)
                    )"""
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ah_a_code ON ah_cross_ref (a_code)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ah_hk_code ON ah_cross_ref (hk_code)"))


def _migrate_daily_chip() -> None:
    """SQLite 旧库补建 daily_chip 表（筹码峰数据）。"""
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "daily_chip" not in existing:
            conn.execute(
                text(
                    """CREATE TABLE daily_chip (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_code VARCHAR(16) NOT NULL,
                        trade_date DATE NOT NULL,
                        market VARCHAR(8) NOT NULL DEFAULT 'a',
                        profit_ratio FLOAT,
                        avg_cost FLOAT,
                        cost_90_low FLOAT,
                        cost_90_high FLOAT,
                        concentration_90 FLOAT,
                        cost_70_low FLOAT,
                        cost_70_high FLOAT,
                        concentration_70 FLOAT,
                        ingested_at DATETIME,
                        UNIQUE(ts_code, trade_date, market)
                    )"""
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_daily_chip_trade_date ON daily_chip (trade_date)")
            )
