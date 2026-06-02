"""全市场扫描：截面预过滤 → 批量指标 → 深度技术打分。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from sqlalchemy import select

from zplan_shared.feature_store import get_features_panel
from zplan_shared.features import scan_universe_features
from zplan_shared.fundamentals import get_financials, get_snapshot
from zplan_shared.market import get_history_window, get_panel, latest_trade_date
from zplan_shared.market_health import check_market_health
from zplan_shared.models import SessionLocal, StockList, init_db
from zplan_shared.pick_context import get_pick_context

from pick_agent.concept_tags import attach_concepts
from pick_agent.scoring import (
    apply_momentum_cap,
    composite_score,
    financial_score_from_rows,
    industry_relative_score,
    intraday_adjust,
    momentum_penalty,
    news_score,
    quick_technical_score,
)
from pick_agent.strategy import PickStrategy, load_strategy
from pick_agent.technical import analyze_technical

logger = logging.getLogger(__name__)


def _load_stock_meta() -> pd.DataFrame:
    init_db()
    with SessionLocal() as session:
        rows = session.execute(select(StockList.ts_code, StockList.name, StockList.industry)).all()
    return pd.DataFrame(rows, columns=["ts_code", "name", "industry"])


def _prefilter_panel(panel: pd.DataFrame, meta: pd.DataFrame, strategy: PickStrategy) -> pd.DataFrame:
    df = panel.merge(meta, on="ts_code", how="left")
    df = df.dropna(subset=["close"])
    if strategy.min_turnover_rate > 0 and "turnover_rate" in df.columns:
        tr = pd.to_numeric(df["turnover_rate"], errors="coerce")
        df = df[tr.isna() | (tr >= strategy.min_turnover_rate)]
    if strategy.min_volume > 0 and "volume" in df.columns:
        vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df = df[vol >= strategy.min_volume]
    if strategy.exclude_st and "name" in df.columns:
        df = df[~df["name"].fillna("").str.contains("ST", case=False, na=False)]
    if strategy.exclude_bj:
        df = df[~df["ts_code"].astype(str).str.startswith("92")]
    return df


def _apply_snapshot_filters(df: pd.DataFrame, strategy: PickStrategy, as_of) -> pd.DataFrame:
    snap = get_snapshot(as_of)
    if snap.empty:
        return df
    merged = df.merge(snap, on="ts_code", how="left", suffixes=("", "_snap"))
    f = strategy.filters
    if f.get("max_pe_ttm") is not None and "pe_ttm" in merged.columns:
        merged = merged[merged["pe_ttm"].isna() | (merged["pe_ttm"] <= float(f["max_pe_ttm"]))]
    if f.get("min_total_mv") is not None and "total_mv" in merged.columns:
        merged = merged[merged["total_mv"].isna() | (merged["total_mv"] >= float(f["min_total_mv"]))]
    if f.get("max_total_mv") is not None and "total_mv" in merged.columns:
        merged = merged[merged["total_mv"].isna() | (merged["total_mv"] <= float(f["max_total_mv"]))]
    return merged


def _apply_feature_filters(
    feat_df: pd.DataFrame,
    strategy: PickStrategy,
) -> pd.DataFrame:
    """基于特征列的后过滤（如 max_ret_20d）。"""
    f = strategy.filters
    max_r20 = f.get("max_ret_20d")
    if max_r20 is not None and "ret_20d" in feat_df.columns:
        r20 = pd.to_numeric(feat_df["ret_20d"], errors="coerce")
        feat_df = feat_df[r20.isna() | (r20 <= float(max_r20))]
    return feat_df


def scan_universe(
    *,
    as_of: str | None = None,
    top_n: int = 20,
    min_bars: int | None = None,
    min_score: float | None = None,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
) -> dict[str, Any]:
    strat = strategy or load_strategy()
    min_bars = min_bars if min_bars is not None else strat.min_bars
    min_score = min_score if min_score is not None else strat.min_score

    if not skip_health_check:
        health = check_market_health(
            min_panel_rows=strat.min_panel_rows,
            max_stale_days=strat.max_stale_days,
        )
        if not health.ok:
            return {"ok": False, "picks": [], "message": health.message, "health": health.__dict__}
    else:
        health = check_market_health(
            min_panel_rows=1,
            max_stale_days=999,
        )

    trade_date = latest_trade_date()
    if trade_date is None:
        return {"ok": False, "picks": [], "message": "无日线数据，请先运行 zplan-股价"}

    panel = get_panel(as_of or trade_date, fields=["close", "pct_chg", "turnover_rate", "volume"])
    if panel.empty:
        return {"ok": False, "picks": [], "message": "截面为空"}

    meta = _load_stock_meta()
    filtered = _prefilter_panel(panel, meta, strat)
    filtered = _apply_snapshot_filters(filtered, strat, trade_date)
    if filtered.empty:
        return {
            "ok": True,
            "as_of": str(trade_date),
            "scanned": len(panel),
            "qualified": 0,
            "picks": [],
            "message": "预过滤后无标的",
            "rule_version": strat.rule_version,
        }

    codes = filtered["ts_code"].tolist()
    feat_df = get_features_panel(trade_date)
    if not feat_df.empty:
        feat_df = feat_df[feat_df["ts_code"].isin(codes)]
    if feat_df.empty or len(feat_df) < max(50, len(codes) // 4):
        history = get_history_window(end=trade_date, calendar_days=150, ts_codes=codes)
        feat_df = scan_universe_features(history, min_bars=min_bars)
    feat_df = _apply_feature_filters(feat_df, strat)
    if feat_df.empty:
        return {
            "ok": True,
            "as_of": str(trade_date),
            "scanned": len(panel),
            "qualified": 0,
            "picks": [],
            "message": "指标池为空（20日涨幅等过滤后无标的）",
            "rule_version": strat.rule_version,
        }

    max_ret = strat.filters.get("max_ret_20d")
    feat_df["quick_score"] = feat_df.apply(
        lambda r: apply_momentum_cap(
            quick_technical_score(r.to_dict()),
            r.get("ret_20d"),
            max_ret_20d=max_ret,
        ),
        axis=1,
    )
    pool_n = min(len(feat_df), top_n * strat.prefilter_top_multiplier)
    shortlist = feat_df.nlargest(pool_n, "quick_score")
    shortlist_codes = shortlist["ts_code"].tolist()

    industry_map = dict(zip(meta["ts_code"], meta["industry"].fillna("")))
    ret_by_industry: dict[str, list[float]] = {}
    for _, row in feat_df.iterrows():
        ind = industry_map.get(row["ts_code"])
        r20 = row.get("ret_20d")
        if ind and pd.notna(r20):
            ret_by_industry.setdefault(ind, []).append(float(r20))

    candidates: list[dict[str, Any]] = []
    for code in shortlist_codes:
        tech = analyze_technical(code, min_bars=min_bars)
        if tech.score < min_score:
            continue
        if strat.require_any_signals and not any(s in tech.signals for s in strat.require_any_signals):
            continue

        ctx = get_pick_context(code)
        fin_df = get_financials(code, limit=8)
        fin_rows = fin_df.to_dict("records") if not fin_df.empty else []
        fin_sc, _ = financial_score_from_rows(fin_rows)
        news_sc, news_detail = news_score(ctx)
        ind_sc, ind_note = industry_relative_score(
            code,
            tech.features.get("ret_20d"),
            industry_map,
            ret_by_industry,
        )
        intra_adj = intraday_adjust(tech, ctx, strat)
        final = composite_score(
            tech=tech,
            fin_score=fin_sc,
            news_sc=news_sc,
            industry_sc=ind_sc,
            intraday_adj=intra_adj,
            strategy=strat,
        )
        final = apply_momentum_cap(final, tech.features.get("ret_20d"), max_ret_20d=max_ret)

        row = filtered.loc[filtered["ts_code"] == code].iloc[0]
        candidates.append(
            attach_concepts(
            {
                "ts_code": code,
                "name": ctx.get("name"),
                "industry": industry_map.get(code),
                "close": float(row["close"]) if pd.notna(row.get("close")) else tech.close,
                "pct_chg": float(row["pct_chg"]) if pd.notna(row.get("pct_chg")) else None,
                "turnover_rate": float(row["turnover_rate"])
                if pd.notna(row.get("turnover_rate"))
                else None,
                "tech_score": tech.score,
                "composite_score": final,
                "verdict": tech.verdict,
                "signals": tech.signals[:5],
                "kdj_k": tech.features.get("kdj_k"),
                "kdj_d": tech.features.get("kdj_d"),
                "ret_20d": tech.features.get("ret_20d"),
                "ma5_cross_ma20": tech.features.get("ma5_cross_ma20"),
                "high_60d_pct": tech.features.get("high_60d_pct"),
                "vol_breakout": tech.features.get("vol_breakout"),
                "industry_relative_note": ind_note,
                "news_mentions_48h": news_detail.get("hits", 0),
                "volume_ratio_vs_prior": (ctx.get("intraday") or {}).get("volume_ratio_vs_prior"),
            }
            )
        )

    ranked = sorted(
        candidates,
        key=lambda x: (
            x["composite_score"] - momentum_penalty(x.get("ret_20d"), max_ret_20d=max_ret),
            x["tech_score"],
        ),
        reverse=True,
    )[:top_n]

    return {
        "ok": True,
        "as_of": str(trade_date),
        "scanned": len(panel),
        "prefiltered": len(filtered),
        "qualified": len(candidates),
        "picks": ranked,
        "health": health.__dict__,
        "rule_version": strat.rule_version,
    }
