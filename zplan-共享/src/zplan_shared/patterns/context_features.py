"""方案 B 上下文特征 — 从已有数据提取多维信息（不依赖 daily_snapshot 回填）。

可用数据源：
- stock_list: industry, listing_date（无时间依赖）
- stock_concept_members: concept_count（无时间依赖）
- financial_indicators: net_profit, roe（按报告期）
- daily_prices: turnover_rate（直接可用）
- 板块相对强度: 从 history DataFrame 实时计算
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from zplan_shared.models import (
    DailySnapshot,
    FinancialIndicator,
    SessionLocal,
    StockConceptMember,
    StockList,
    init_db,
)

logger = logging.getLogger(__name__)


def build_context_cache_v2(
    ts_codes: list[str],
    event_dates: pd.Series,
    history: pd.DataFrame,
    *,
    financial_lookback_months: int = 9,
) -> dict[str, dict[str, Any]]:
    """为事件批量预加载上下文特征（v2：不依赖 daily_snapshot）。

    Parameters
    ----------
    ts_codes : 股票代码列表
    event_dates : 对应的事件日期
    history : 全市场日线长表（用于计算板块相对强度）
    financial_lookback_months : 财报回溯月数
    """
    init_db()
    codes = list(set(ts_codes))
    cache: dict[str, dict[str, Any]] = {}

    # ── 1. 行业映射 ──────────────────────────────────────
    with SessionLocal() as session:
        rows = session.execute(
            select(StockList.ts_code, StockList.industry, StockList.listing_date)
            .where(StockList.ts_code.in_(codes))
        ).all()
    industry_map: dict[str, str] = {}
    listing_map: dict[str, date] = {}
    for code, ind, ld in rows:
        industry_map[code] = ind or "unknown"
        if ld:
            listing_map[code] = ld if isinstance(ld, date) else ld.date()

    # 行业编码（字符串 → 数值）
    all_industries = sorted(set(industry_map.values()))
    ind_to_code = {ind: i / max(len(all_industries) - 1, 1) for i, ind in enumerate(all_industries)}
    # 每个行业的股票数（用于加权）
    ind_counts = {ind: list(industry_map.values()).count(ind) for ind in all_industries}

    # ── 2. 概念数量 ──────────────────────────────────────
    concept_counts: dict[str, int] = {}
    with SessionLocal() as session:
        rows = session.execute(
            select(StockConceptMember.ts_code, func.count(StockConceptMember.concept_name))
            .where(StockConceptMember.ts_code.in_(codes))
            .group_by(StockConceptMember.ts_code)
        ).all()
    for code, cnt in rows:
        concept_counts[code] = int(cnt)

    # ── 3. 财务指标 ──────────────────────────────────────
    unique_dates = sorted(pd.to_datetime(event_dates).dt.date.unique())
    fin_map: dict[str, dict[str, float]] = {}
    if unique_dates:
        fin_start = unique_dates[0].replace(day=1) - timedelta(days=financial_lookback_months * 31)
        fin_end = unique_dates[-1]
        with SessionLocal() as session:
            fin_rows = session.execute(
                select(
                    FinancialIndicator.ts_code, FinancialIndicator.report_date,
                    FinancialIndicator.net_profit, FinancialIndicator.roe,
                )
                .where(
                    FinancialIndicator.ts_code.in_(codes),
                    FinancialIndicator.report_date >= fin_start,
                    FinancialIndicator.report_date <= fin_end,
                )
            ).all()
        fin_df = pd.DataFrame(fin_rows, columns=["ts_code", "report_date", "net_profit", "roe"])
        if not fin_df.empty:
            fin_df["report_date"] = pd.to_datetime(fin_df["report_date"]).dt.date

        # 为每个 (code, event_date) 找最近财报
        for _, row in fin_df.iterrows():
            code = row["ts_code"]
            rpt_date = row["report_date"]
            key = (code, rpt_date)
            if code not in fin_map:
                fin_map[code] = {}
            for col in ["net_profit", "roe"]:
                val = row[col]
                if val is not None and not np.isnan(float(val)):
                    fin_map[code][f"{col}_{rpt_date}"] = float(val)
    else:
        fin_df = pd.DataFrame()

    # ── 4. 估值快照（历史 PE/PB/市值）─────────────────────
    snap_by_code: dict[str, dict[date, dict[str, float]]] = {}
    if unique_dates:
        min_date = unique_dates[0] - timedelta(days=7)
        max_date = unique_dates[-1] + timedelta(days=1)
        with SessionLocal() as session:
            snap_rows = session.execute(
                select(
                    DailySnapshot.ts_code, DailySnapshot.trade_date,
                    DailySnapshot.pe_ttm, DailySnapshot.pb,
                    DailySnapshot.total_mv, DailySnapshot.circ_mv,
                )
                .where(
                    DailySnapshot.ts_code.in_(codes),
                    DailySnapshot.trade_date >= min_date,
                    DailySnapshot.trade_date <= max_date,
                )
            ).all()
        for row in snap_rows:
            code = row.ts_code
            td = row.trade_date if isinstance(row.trade_date, date) else row.trade_date.date()
            if code not in snap_by_code:
                snap_by_code[code] = {}
            snap_by_code[code][td] = {
                "pe_ttm": float(row.pe_ttm) if row.pe_ttm is not None and not np.isnan(float(row.pe_ttm)) else 0.0,
                "pb": float(row.pb) if row.pb is not None and not np.isnan(float(row.pb)) else 0.0,
                "total_mv": float(row.total_mv) if row.total_mv is not None and not np.isnan(float(row.total_mv)) else 0.0,
                "circ_mv": float(row.circ_mv) if row.circ_mv is not None and not np.isnan(float(row.circ_mv)) else 0.0,
            }

    # ── 5. 逐事件组装 ────────────────────────────────────
    for code, evt_date in zip(ts_codes, event_dates):
        evt_date_clean = evt_date if isinstance(evt_date, date) else pd.Timestamp(evt_date).date()
        key = f"{code}_{evt_date_clean}"
        feats: dict[str, float] = {}

        # 行业编码
        industry = industry_map.get(code, "unknown")
        feats["industry_encoded"] = round(ind_to_code.get(industry, 0.5), 4)
        feats["industry_size"] = float(ind_counts.get(industry, 1))

        # 上市天数
        list_date = listing_map.get(code)
        if list_date:
            feats["days_listed"] = float(max(0, (evt_date_clean - list_date).days))
        else:
            feats["days_listed"] = 365.0

        # 概念数量
        feats["concept_count"] = float(concept_counts.get(code, 0))

        # 估值快照（离事件日最近的一个交易日）
        code_snaps = snap_by_code.get(code, {})
        best_snap = None; best_delta = 999
        for s_date, s_vals in code_snaps.items():
            if s_date <= evt_date_clean:
                delta = (evt_date_clean - s_date).days
                if delta < best_delta:
                    best_delta = delta; best_snap = s_vals
        if best_snap and best_delta <= 7:
            feats["pe_ttm"] = round(best_snap.get("pe_ttm", 0.0), 4)
            feats["pb"] = round(best_snap.get("pb", 0.0), 4)
            feats["log_market_cap"] = round(np.log(max(best_snap.get("total_mv", 1e6), 1e6)), 4)
            feats["pe_clipped"] = round(max(min(feats.get("pe_ttm", 0), 500), -500), 4)
        else:
            feats["pe_ttm"] = 0.0; feats["pb"] = 0.0
            feats["log_market_cap"] = 0.0; feats["pe_clipped"] = 0.0

        # 财报（最近一期在事件日之前的）
        best_net_profit = 0.0
        best_roe = 0.0
        if code in fin_map:
            for col_key, val in fin_map[code].items():
                try:
                    rpt_str = col_key.split("_")[-1]
                    rpt_date = date.fromisoformat(rpt_str) if "-" in rpt_str else None
                except (ValueError, IndexError):
                    continue
                if rpt_date and rpt_date <= evt_date_clean:
                    if "net_profit" in col_key:
                        best_net_profit = val
                    elif "roe" in col_key:
                        best_roe = val
        feats["net_profit"] = round(best_net_profit, 4)
        feats["roe"] = round(best_roe, 4)
        feats["net_profit_positive"] = 1.0 if best_net_profit > 0 else 0.0

        cache[key] = feats

    # 统计
    n_with_fin = sum(1 for v in cache.values() if v.get("net_profit", 0) != 0 or v.get("roe", 0) != 0)
    n_with_snap = sum(1 for v in cache.values() if v.get("pe_ttm", 0) != 0)
    logger.info(
        "上下文缓存 v2: %d 事件, %d 有财报, %d 有PE/PB快照",
        len(cache), n_with_fin, n_with_snap,
    )

    return cache


def merge_context_features(
    agg_features: dict[str, float],
    context: dict[str, Any] | None,
) -> dict[str, float]:
    """合并价格聚合特征 + 上下文特征 → 方案 B 特征向量。"""
    merged = dict(agg_features)
    if context:
        for k, v in context.items():
            if k not in merged:
                merged[k] = float(v) if v is not None else 0.0
    return merged
