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


class DailyFeature(Base):
    """技术指标日频快照（Phase A.3，每票每个交易日一行）。"""

    __tablename__ = "daily_features"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_daily_features_ts_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
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
    __table_args__ = (UniqueConstraint("ts_code", "trade_date", name="uq_snapshot_ts_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trade_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    pe_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ps_ttm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_mv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    circ_mv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="akshare_em")
    ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class FinancialIndicator(Base):
    __tablename__ = "financial_indicators"
    __table_args__ = (UniqueConstraint("ts_code", "report_date", name="uq_fi_ts_report"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    report_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
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
        UniqueConstraint("news_source", "news_id", "ts_code", name="uq_news_stock_link"),
        Index("ix_news_stock_link_ts_pub", "ts_code", "published_at_utc"),
        Index("ix_news_stock_link_source_news", "news_source", "news_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_source: Mapped[str] = mapped_column(String(32), nullable=False)
    news_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
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
        UniqueConstraint("concept_name", "ts_code", name="uq_stock_concept_member"),
        Index("ix_stock_concept_name", "concept_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    concept_name: Mapped[str] = mapped_column(String(128), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    synced_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class PickWatchlist(Base):
    """用户持仓 / 关注订阅（每日简报）。"""

    __tablename__ = "pick_watchlist"
    __table_args__ = (UniqueConstraint("ts_code", name="uq_pick_watchlist_ts_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_sync_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_brief_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class StockRuleScore(Base):
    """全市场规则引擎打分快照（按交易日 + rule_version 唯一）。"""

    __tablename__ = "stock_rule_scores"
    __table_args__ = (
        UniqueConstraint(
            "ts_code",
            "trade_date_as_of",
            "rule_version",
            name="uq_stock_rule_scores",
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


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_daily_prices_phase_a()
    _migrate_financial_indicators_phase_d()
    _migrate_pick_entries_predictions()
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
