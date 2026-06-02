"""全市场规则分初始化 → ``stock_rule_scores``。"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import pandas as pd

from zplan_shared.feature_store import get_features_panel
from zplan_shared.features import feature_flag, scan_universe_features
from zplan_shared.market import get_history_window, get_panel, latest_trade_date
from zplan_shared.market_health import check_market_health
from zplan_shared.stock_rule_scores import count_scores, upsert_rule_scores

from pick_agent.scanner import (
    _apply_snapshot_filters,
    _load_stock_meta,
    _prefilter_panel,
)
from pick_agent.scoring import apply_momentum_cap, quick_technical_score, verdict_from_score
from pick_agent.strategy import PickStrategy, load_strategy

logger = logging.getLogger(__name__)


def _signals_from_features(features: dict[str, float | None]) -> list[str]:
    signals: list[str] = []
    ma5, ma20, ma60 = features.get("ma5"), features.get("ma20"), features.get("ma60")
    if ma5 and ma20 and ma60 and ma5 > ma20 > ma60:
        signals.append("均线多头排列（MA5>MA20>MA60）")
    if feature_flag(features, "ma5_cross_ma20"):
        signals.append("MA5 上穿 MA20")
    if feature_flag(features, "kdj_golden_cross"):
        signals.append("KDJ 金叉")
    elif feature_flag(features, "kdj_death_cross"):
        signals.append("KDJ 死叉")
    if feature_flag(features, "macd_cross_up"):
        signals.append("MACD 柱由负转正")
    if feature_flag(features, "vol_breakout") and (features.get("ret_5d") or 0) > 0:
        signals.append("放量上涨")
    h60 = features.get("high_60d_pct")
    if h60 is not None and h60 >= 98:
        signals.append("接近 60 日新高")
    ret20 = features.get("ret_20d")
    if ret20 is not None and -3 <= ret20 <= 2:
        signals.append("20 日回撤低吸区")
    return signals[:5]


def build_rule_scores_universe(
    *,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
) -> dict[str, Any]:
    """向量化规则分写入 ``stock_rule_scores``（全预筛池，非仅 Top N）。"""
    strat = strategy or load_strategy()

    if not skip_health_check:
        health = check_market_health(
            min_panel_rows=strat.min_panel_rows,
            max_stale_days=strat.max_stale_days,
        )
        if not health.ok:
            return {"ok": False, "message": health.message, "health": health.__dict__}

    trade_date = latest_trade_date()
    if trade_date is None:
        return {"ok": False, "message": "无日线数据，请先运行 zplan-股价"}

    panel = get_panel(trade_date, fields=["close", "pct_chg", "turnover_rate", "volume"])
    if panel.empty:
        return {"ok": False, "message": "截面为空"}

    meta = _load_stock_meta()
    filtered = _prefilter_panel(panel, meta, strat)
    filtered = _apply_snapshot_filters(filtered, strat, trade_date)
    if filtered.empty:
        return {"ok": False, "message": "预过滤后无标的"}

    codes = filtered["ts_code"].tolist()
    feat_source = "computed"
    feat_panel = get_features_panel(trade_date)
    if not feat_panel.empty:
        feat_df = feat_panel.merge(
            filtered[["ts_code"]],
            on="ts_code",
            how="inner",
        )
        if len(feat_df) >= len(codes) * 0.5:
            feat_source = "daily_features"
            logger.info(
                "规则初始化：使用 daily_features 物化表 %s 只（预筛 %s）",
                len(feat_df),
                len(codes),
            )
        else:
            feat_df = pd.DataFrame()
    else:
        feat_df = pd.DataFrame()

    if feat_df.empty:
        logger.info("规则初始化：物化表不足，批量计算指标（预筛 %s 只）…", len(codes))
        history = get_history_window(end=trade_date, calendar_days=150, ts_codes=codes)
        feat_df = scan_universe_features(history, min_bars=strat.min_bars)
        feat_source = "computed"
    if feat_df.empty:
        return {"ok": False, "message": "历史 K 线不足，无法计算指标"}

    max_ret = strat.filters.get("max_ret_20d")
    if max_ret is not None and "ret_20d" in feat_df.columns:
        feat_df = feat_df[
            feat_df["ret_20d"].isna() | (feat_df["ret_20d"] <= float(max_ret))
        ]
    if feat_df.empty:
        return {"ok": False, "message": "预筛后无标的（20日涨幅过滤过严）"}

    name_map = dict(zip(meta["ts_code"], meta["name"].fillna("")))
    close_map = dict(zip(filtered["ts_code"], filtered["close"]))

    rows: list[dict[str, Any]] = []
    for _, r in feat_df.iterrows():
        code = str(r["ts_code"])
        features = {k: r[k] for k in r.index if k != "ts_code" and pd.notna(r[k])}
        ret20 = features.get("ret_20d")
        tech = apply_momentum_cap(
            round(quick_technical_score(features), 1),
            ret20,
            max_ret_20d=max_ret,
        )
        rows.append(
            {
                "ts_code": code,
                "name": name_map.get(code),
                "tech_score": tech,
                "composite_score": tech,
                "verdict": verdict_from_score(tech),
                "close": float(close_map[code]) if code in close_map and pd.notna(close_map.get(code)) else features.get("close"),
                "signals": _signals_from_features(features),
                "features": {
                    k: features[k]
                    for k in (
                        "ret_5d",
                        "ret_20d",
                        "kdj_k",
                        "kdj_d",
                        "ma5",
                        "ma20",
                        "macd_hist",
                        "high_60d_pct",
                    )
                    if k in features
                },
            }
        )

    as_of_d = date.fromisoformat(str(trade_date)[:10])
    n = upsert_rule_scores(rows, trade_date_as_of=as_of_d, rule_version=strat.rule_version)
    total = count_scores(trade_date_as_of=as_of_d, rule_version=strat.rule_version)

    return {
        "ok": True,
        "as_of": str(trade_date),
        "rule_version": strat.rule_version,
        "panel_rows": len(panel),
        "prefiltered": len(filtered),
        "scored": n,
        "table_total": total,
        "feat_source": feat_source,
    }
