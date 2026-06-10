"""选股上下文：近端分时微观结构 + 资讯域只读摘要（Phase A.1 + P0 news_stock_link）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select, text

from zplan_shared.config import INTRADAY_COARSE_PERIOD, INTRADAY_FINE_PERIOD
from zplan_shared.market import get_minute_bars, resolve_ts_code
from zplan_shared.models import SessionLocal, StockList, init_db
from zplan_shared.news_linker import get_linked_news_for_stock


def summarize_recent_intraday(ts_code: str) -> dict[str, Any]:
    """由近端分时推导的成交趋势特征（供选股/资讯融合）。"""
    code = resolve_ts_code(ts_code)
    coarse = get_minute_bars(code, period=INTRADAY_COARSE_PERIOD)
    fine = get_minute_bars(code, period=INTRADAY_FINE_PERIOD)

    out: dict[str, Any] = {
        "ts_code": code,
        "bars_5m": len(coarse),
        "bars_1m": len(fine),
        "has_intraday": not coarse.empty or not fine.empty,
    }
    target = coarse if not coarse.empty else fine
    if target.empty:
        return out

    target = target.copy()
    if "bar_time" not in target.columns:
        target = target.reset_index()
    target["bar_time"] = pd.to_datetime(target["bar_time"])
    target["trade_date"] = target["bar_time"].dt.date
    daily_vol = target.groupby("trade_date")["volume"].sum()
    if len(daily_vol) >= 2:
        recent = float(daily_vol.iloc[-1])
        prior = float(daily_vol.iloc[:-1].mean())
        out["volume_ratio_vs_prior"] = round(recent / prior, 4) if prior else None
    else:
        out["volume_ratio_vs_prior"] = None

    if "amount" in target.columns and target["amount"].notna().any():
        vwap = (target["close"] * target["volume"]).sum() / target["volume"].sum()
        out["session_vwap"] = round(float(vwap), 4) if target["volume"].sum() else None
    else:
        out["session_vwap"] = None

    afternoon = target[target["bar_time"].dt.hour >= 13]
    morning = target[target["bar_time"].dt.hour < 12]
    if not afternoon.empty and not morning.empty:
        am_vol = float(morning["volume"].sum())
        pm_vol = float(afternoon["volume"].sum())
        out["afternoon_volume_share"] = round(pm_vol / (am_vol + pm_vol), 4) if (am_vol + pm_vol) else None
    else:
        out["afternoon_volume_share"] = None

    if "pct_chg" in target.columns:
        out["avg_bar_pct_chg_5m"] = round(float(target["pct_chg"].mean()), 4)
    return out


def _news_mentions_legacy_like(code: str, name: str | None, since: datetime) -> dict[str, int]:
    """兼容：无 news_stock_link 时的 LIKE 回退计数。"""
    patterns = [f"%{code}%"]
    if name:
        compact = name.replace(" ", "").strip()
        if compact:
            patterns.append(f"%{compact}%")

    clauses = []
    params: dict[str, Any] = {"since": since}
    for i, _ in enumerate(patterns):
        key = f"p{i}"
        params[key] = patterns[i]
        clauses.append(f"title LIKE :{key}")

    where_like = " OR ".join(clauses)
    counts: dict[str, int] = {}
    with SessionLocal() as session:
        try:
            row = session.execute(
                text(
                    f"SELECT COUNT(*) FROM financial_alerts WHERE "
                    f"({where_like}) AND published_at_utc >= :since"
                ),
                params,
            ).scalar_one()
            counts["financial_alerts"] = int(row or 0)
        except Exception:
            counts["financial_alerts"] = 0
        try:
            row = session.execute(
                text(
                    f"SELECT COUNT(*) FROM global_news WHERE "
                    f"({where_like}) AND published_at_utc >= :since"
                ),
                params,
            ).scalar_one()
            counts["global_news"] = int(row or 0)
        except Exception:
            counts["global_news"] = 0
    counts["total"] = sum(counts.values())
    return counts


def _summarize_linked_news(rows: list[dict[str, object]]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    event_types: dict[str, int] = {}
    sentiments: list[str] = []
    for r in rows:
        src = str(r.get("source_label") or r.get("news_source") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1
        et = r.get("event_type")
        if et:
            event_types[str(et)] = event_types.get(str(et), 0) + 1
    return {
        "total": len(rows),
        "by_source": by_source,
        "event_types": event_types,
        "items": rows[:12],
    }


def _news_stock_link(code: str, since: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """若资讯侧已建 ``news_stock_link`` 表则返回关联条目。"""
    init_db()
    try:
        with SessionLocal() as session:
            rows = session.execute(
                text(
                    "SELECT n.title, n.published_at_utc, l.confidence, l.matched_by, "
                    "COALESCE(l.event_type, '') AS event_type, "
                    "COALESCE(l.sentiment, '') AS sentiment "
                    "FROM news_stock_link l "
                    "JOIN ("
                    "  SELECT id, title, published_at_utc FROM financial_alerts "
                    "  UNION ALL "
                    "  SELECT id, title, published_at_utc FROM global_news"
                    ") n ON n.id = l.news_id "
                    "WHERE l.ts_code = :code AND n.published_at_utc >= :since "
                    "ORDER BY n.published_at_utc DESC LIMIT :lim"
                ),
                {"code": code, "since": since, "lim": limit},
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _news_sentiment_summary(events: list[dict[str, Any]], mentions: dict[str, int]) -> dict[str, Any]:
    pos = sum(1 for e in events if str(e.get("sentiment", "")).lower() in ("pos", "positive", "利好"))
    neg = sum(1 for e in events if str(e.get("sentiment", "")).lower() in ("neg", "negative", "利空"))
    if not pos and not neg and mentions.get("total", 0) > 0:
        return {"neutral": True, "total": mentions["total"]}
    return {"positive": pos, "negative": neg, "total": mentions.get("total", 0)}


def get_pick_context(ts_code: str, *, news_hours: int = 48, as_of: date | None = None) -> dict[str, Any]:
    """单票选股上下文：分时微观结构 + 关联新闻（news_stock_link 优先）。

    ``as_of`` 为 None 时用当前时间（生产模式）；传 date 时用于历史回测。
    """
    code = resolve_ts_code(ts_code)
    if as_of is not None:
        since = datetime.combine(as_of, datetime.min.time()) - timedelta(hours=news_hours)
    else:
        since = datetime.utcnow() - timedelta(hours=news_hours)

    init_db()
    industry: str | None = None
    listing_date = None
    name: str | None = None
    with SessionLocal() as session:
        row = session.execute(
            select(StockList.name, StockList.industry, StockList.listing_date).where(
                StockList.ts_code == code
            )
        ).first()
        if row:
            name, industry, listing_date = row[0], row[1], row[2]

    intraday = summarize_recent_intraday(code)
    linked = get_linked_news_for_stock(code, hours=news_hours, limit=24)
    news_summary = _summarize_linked_news(linked)

    fa_n = sum(1 for r in linked if r.get("news_source") == "financial_alerts")
    gn_n = sum(1 for r in linked if r.get("news_source") == "global_news")
    via = "news_stock_link"
    if any(r.get("matched_by") == "title_like" for r in linked):
        via = "news_stock_link+title_like"
    news_mentions = {
        "financial_alerts": fa_n,
        "global_news": gn_n,
        "total": news_summary["total"],
        "via": via,
    }
    if news_summary["total"] == 0:
        legacy = _news_mentions_legacy_like(code, name, since)
        news_mentions = {**legacy, "via": "title_like_fallback"}

    # 筹码峰数据
    chip = get_chip_context(code)

    return {
        "ts_code": code,
        "name": name,
        "industry": industry,
        "listing_date": listing_date.isoformat() if listing_date else None,
        "news_hours": news_hours,
        "intraday": intraday,
        "news_mentions": news_mentions,
        "news_linked": news_summary,
        "chip": chip,
    }


def get_chip_context(ts_code: str) -> dict[str, Any]:
    """单票筹码峰数据快照（成本分布、获利比例、集中度）。"""
    code = resolve_ts_code(ts_code)
    try:
        from zplan_shared.market import get_stock_chip
        from zplan_shared.market import latest_trade_date as _ltd

        as_of = _ltd()
        data = get_stock_chip(code, as_of=as_of)
        if not data:
            return {"available": False}
        return {
            "available": True,
            "as_of": str(as_of) if as_of else None,
            "profit_ratio": data.get("profit_ratio"),
            "avg_cost": data.get("avg_cost"),
            "concentration_90": data.get("concentration_90"),
            "concentration_70": data.get("concentration_70"),
            "cost_90_low": data.get("cost_90_low"),
            "cost_90_high": data.get("cost_90_high"),
        }
    except Exception:
        return {"available": False}
