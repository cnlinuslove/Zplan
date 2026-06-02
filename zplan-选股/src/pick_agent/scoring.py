"""综合打分：技术 + 财务 + 资讯 + 板块相对强弱 + 分时。"""
from __future__ import annotations

from typing import Any

import pandas as pd

from zplan_shared.features import feature_flag

from pick_agent.strategy import PickStrategy
from pick_agent.technical import TechnicalSnapshot, analyze_technical


def financial_score_from_rows(rows: list[dict[str, Any]]) -> tuple[float | None, str]:
    if not rows:
        return None, "财报缺失"
    latest = rows[0]
    score = 50.0
    notes: list[str] = []
    np_ = latest.get("net_profit")
    if np_ is not None:
        score += 15 if np_ > 0 else -15
        notes.append("净利润为正" if np_ > 0 else "净利润为负")
    if len(rows) >= 2 and rows[0].get("revenue") and rows[1].get("revenue"):
        if rows[0]["revenue"] > rows[1]["revenue"]:
            score += 10
            notes.append("营收改善")
        else:
            score -= 5
    return max(0.0, min(100.0, score)), "；".join(notes) if notes else "财报粗评"


def news_score(ctx: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """资讯分：``news_stock_link`` 关联优先，LIKE 回退计数。"""
    news = ctx.get("news_mentions") or {}
    total = int(news.get("total", 0) or 0)
    linked = ctx.get("news_linked") or {}
    event_types = linked.get("event_types") or {}
    score = 50.0
    if total > 0:
        score += min(30.0, total * 4)

    risk_events = ("减持", "监管", "立案", "警示")
    for ev, cnt in event_types.items():
        if ev in risk_events and cnt:
            score -= min(20.0, 8 * cnt)

    return max(0.0, min(100.0, score)), {
        "hits": total,
        "via": news.get("via"),
        "event_types": event_types,
    }


def industry_relative_score(
    ts_code: str,
    ret_20d: float | None,
    industry_map: dict[str, str],
    ret_by_industry: dict[str, list[float]],
) -> tuple[float | None, str | None]:
    industry = industry_map.get(ts_code)
    if not industry or ret_20d is None:
        return None, None
    peers = ret_by_industry.get(industry) or []
    if len(peers) < 3:
        return None, None
    s = pd.Series(peers, dtype=float)
    pctile = float((s <= ret_20d).mean())
    # 用分位数映射 0-100
    score = pctile * 100
    note = f"行业「{industry}」20日收益分位约 {pctile:.0%}"
    return score, note


def intraday_adjust(tech: TechnicalSnapshot, ctx: dict[str, Any], strategy: PickStrategy) -> float:
    adj = 0.0
    intra = ctx.get("intraday") or {}
    cfg = strategy.intraday
    pm_share = intra.get("afternoon_volume_share")
    if pm_share is not None and pm_share >= 0.55:
        adj += cfg.get("afternoon_volume_share_bonus", 3)
    vol_r = intra.get("volume_ratio_vs_prior")
    thr = cfg.get("volume_ratio_high_threshold", 1.3)
    if vol_r is not None and vol_r >= thr and (tech.features.get("ret_5d") or 0) > 0:
        adj += cfg.get("volume_ratio_high_bonus", 4)
    return adj


def composite_score(
    *,
    tech: TechnicalSnapshot,
    fin_score: float | None,
    news_sc: float,
    industry_sc: float | None,
    intraday_adj: float,
    strategy: PickStrategy,
) -> float:
    w = strategy.weights
    tech_s = min(100.0, tech.score + intraday_adj)
    ind_s = industry_sc if industry_sc is not None else 50.0
    fin_s = fin_score if fin_score is not None else 50.0
    total_w = w["technical"] + w["financial"] + w["news"] + w["industry_relative"]
    composite = (
        w["technical"] * tech_s
        + w["financial"] * fin_s
        + w["news"] * news_sc
        + w["industry_relative"] * ind_s
    ) / total_w
    return round(max(0.0, min(100.0, composite)), 1)


def momentum_penalty(ret_20d: float | None, *, max_ret_20d: float | None = 12.0) -> float:
    """20 日涨幅过热扣分（用于技术分 / 排序，非 forward 预测）。"""
    if ret_20d is None:
        return 0.0
    r = float(ret_20d)
    cap = float(max_ret_20d) if max_ret_20d is not None else 12.0
    if r > cap:
        return 25.0
    if r > 10:
        return 15.0 + (r - 10) * 1.5
    if r > 8:
        return 8.0 + (r - 8) * 2.0
    if r > 5:
        return (r - 5) * 2.0
    return 0.0


def apply_momentum_cap(
    score: float,
    ret_20d: float | None,
    *,
    max_ret_20d: float | None = 12.0,
) -> float:
    """过热时压低综合/技术分上限。"""
    s = float(score)
    if ret_20d is None:
        return round(max(0.0, min(100.0, s)), 1)
    r = float(ret_20d)
    cap = float(max_ret_20d) if max_ret_20d is not None else 12.0
    if r > cap:
        s = min(s, 62.0)
    elif r > 10:
        s = min(s, 72.0)
    elif r > 8:
        s = min(s, 78.0)
    elif r > 5:
        s = min(s, 85.0)
    return round(max(0.0, min(100.0, s)), 1)


def verdict_from_score(score: float) -> str:
    if score >= 70:
        return "偏多"
    if score < 45:
        return "偏空"
    return "中性"


def quick_technical_score(features: dict[str, float | None]) -> float:
    """向量化预筛用的轻量技术分（与 analyze_technical 共用 P0 快照字段）。"""
    score = 50.0
    ma5, ma20, ma60 = features.get("ma5"), features.get("ma20"), features.get("ma60")
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            score += 15
        elif ma5 < ma20 < ma60:
            score -= 15
    if feature_flag(features, "ma5_cross_ma20"):
        score += 8
    if feature_flag(features, "macd_cross_up"):
        score += 6
    if feature_flag(features, "kdj_golden_cross"):
        score += 5
    elif feature_flag(features, "kdj_death_cross"):
        score -= 5
    ret20 = features.get("ret_20d")
    if ret20 is not None:
        score -= momentum_penalty(ret20)
        if ret20 < -15:
            score -= 6
        elif -3 <= ret20 <= 3:
            score += 3
    hist = features.get("macd_hist")
    if hist is not None and hist > 0:
        score += 4
    if feature_flag(features, "vol_breakout") and (features.get("ret_5d") or 0) > 0:
        score += 5
    h60 = features.get("high_60d_pct")
    if h60 is not None and h60 >= 98:
        score += 4
    cvm = features.get("close_vs_ma20")
    if cvm is not None and -3 <= cvm <= 2:
        score += 2
    return max(0.0, min(100.0, score))
