"""选股上下文：近端分时微观结构 + 资讯域只读摘要（Phase A.1）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select, text

from zplan_shared.config import INTRADAY_COARSE_PERIOD, INTRADAY_FINE_PERIOD
from zplan_shared.market import get_minute_bars, resolve_ts_code
from zplan_shared.models import SessionLocal, StockList, init_db


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


def _news_mentions(code: str, name: str | None, since: datetime) -> dict[str, int]:
    init_db()
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
        clauses.append(f"title LIKE :{key} OR COALESCE(summary, '') LIKE :{key}")

    where_like = " OR ".join(clauses)
    counts: dict[str, int] = {}
    with SessionLocal() as session:
        for table in ("financial_alerts", "global_news"):
            try:
                row = session.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table} WHERE "
                        f"({where_like}) AND published_at_utc >= :since"
                    ),
                    params,
                ).scalar_one()
                counts[table] = int(row or 0)
            except Exception:
                counts[table] = 0
    counts["total"] = sum(counts.values())
    return counts


def get_pick_context(ts_code: str, *, news_hours: int = 48) -> dict[str, Any]:
    """单票选股上下文：分时微观结构 + 近期舆情/新闻命中数。"""
    code = resolve_ts_code(ts_code)
    since = datetime.utcnow() - timedelta(hours=news_hours)

    init_db()
    name: str | None = None
    with SessionLocal() as session:
        name = session.execute(
            select(StockList.name).where(StockList.ts_code == code)
        ).scalar_one_or_none()

    intraday = summarize_recent_intraday(code)
    news = _news_mentions(code, name, since)

    return {
        "ts_code": code,
        "name": name,
        "news_hours": news_hours,
        "intraday": intraday,
        "news_mentions": news,
    }
