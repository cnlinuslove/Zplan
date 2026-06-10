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


def cross_market_score_from_pair(ts_code: str) -> tuple[float, dict]:
    """跨市场 Alpha 分 → 百分制，供 composite_score 使用。"""
    try:
        from zplan_shared.cross_market import cross_market_score as cm_score
        raw, detail = cm_score(ts_code)
        return raw * 10, detail
    except Exception:
        return 50.0, {"reason": "unavailable"}


def composite_score(
    *,
    tech: TechnicalSnapshot,
    fin_score: float | None,
    news_sc: float,
    industry_sc: float | None,
    intraday_adj: float,
    strategy: PickStrategy,
    ts_code: str | None = None,
) -> float:
    w = strategy.weights
    tech_s = min(100.0, tech.score + intraday_adj)
    ind_s = industry_sc if industry_sc is not None else 50.0
    fin_s = fin_score if fin_score is not None else 50.0

    # 跨市场 Alpha 加成
    cm_s = 50.0
    cm_w = w.get("cross_market", 0)
    if ts_code and cm_w > 0:
        cm_s, _ = cross_market_score_from_pair(ts_code)

    total_w = (
        w.get("technical", 0.65)
        + w.get("financial", 0.15)
        + w.get("news", 0.10)
        + w.get("industry_relative", 0.10)
        + cm_w
    )
    composite = (
        w.get("technical", 0.65) * tech_s
        + w.get("financial", 0.15) * fin_s
        + w.get("news", 0.10) * news_sc
        + w.get("industry_relative", 0.10) * ind_s
        + cm_w * cm_s
    ) / total_w
    return round(max(0.0, min(100.0, composite)), 1)


def momentum_penalty(ret_20d: float | None, *, max_ret_20d: float | None = 5.0) -> float:
    """20 日涨幅过热扣分（用于技术分 / 排序，非 forward 预测）。

    ML 实验证实：ret_late（近期涨幅）是反转信号的最强单一特征（r=-0.56 与 forward_return）。
    阈值下调至 5% 与 strategy.yaml 预筛一致。
    """
    if ret_20d is None:
        return 0.0
    r = float(ret_20d)
    cap = float(max_ret_20d) if max_ret_20d is not None else 5.0
    if r > cap:
        return 30.0
    if r > 7:
        return 25.0 + (r - 7) * 3.0
    if r > 5:
        return 15.0 + (r - 5) * 4.0
    if r > 3:
        return (r - 3) * 3.0
    return 0.0


def apply_momentum_cap(
    score: float,
    ret_20d: float | None,
    *,
    max_ret_20d: float | None = 5.0,
    vol_ratio20: float | None = None,
) -> float:
    """过热时压低综合/技术分上限；缩量上涨额外惩罚。

    预筛已剔除 ret_20d>5%，此处对 3-5% 区间温和限上限，避免分数底板效应。
    """
    s = float(score)
    if ret_20d is None:
        return round(max(0.0, min(100.0, s)), 1)
    r = float(ret_20d)
    cap_val = float(max_ret_20d) if max_ret_20d is not None else 5.0
    if r > cap_val:
        s = min(s, 55.0)          # 理论上预筛已剔除，兜底
    elif r > 4:
        s = min(s, 68.0)          # was 65，放宽至 68
    elif r > 3:
        s = min(s, 75.0)          # was 72，放宽至 75

    # 缩量上涨：ML 证实 body_last + vol_ratio 是前五大特征
    if r > 2 and vol_ratio20 is not None and float(vol_ratio20) < 0.8:
        s = max(48.0, s - 7.0)    # was max(45, -8)，稍温和

    return round(max(0.0, min(100.0, s)), 1)


def verdict_from_score(score: float) -> str:
    if score >= 70:
        return "偏多"
    if score < 45:
        return "偏空"
    return "中性"


def quick_technical_score(features: dict[str, float | None]) -> float:
    """向量化预筛用的轻量技术分（与 analyze_technical 共用 P0 快照字段）。

    2026-06-10: 减弱动量驱动加分，增强反转因子权重。
    —— 规则分审计发现：动量加分导致高分=追涨=亏损，反转因子才是 alpha 来源。
    """
    score = 50.0
    ma5, ma20, ma60 = features.get("ma5"), features.get("ma20"), features.get("ma60")
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            score += 5           # was +8，减弱追涨偏好
        elif ma5 < ma20 < ma60:
            score -= 15
    if feature_flag(features, "ma5_cross_ma20"):
        score += 5               # was +8
    if feature_flag(features, "macd_cross_up"):
        score += 4               # was +6
    if feature_flag(features, "kdj_golden_cross"):
        score += 3               # was +5
    elif feature_flag(features, "kdj_death_cross"):
        score -= 5
    ret20 = features.get("ret_20d")
    if ret20 is not None:
        score -= momentum_penalty(ret20)
        if ret20 < -12:
            score += 8            # was +5，深度超卖→强均值回归机会
        elif ret20 < -7:
            score += 5            # was +3，中度回调→低吸机会
        elif -3 <= ret20 <= 3:
            score += 6            # was +4，横盘整理/低吸区
        elif ret20 > 5:
            score -= 2            # 追涨惩罚（预筛已剔除 >5%）
    hist = features.get("macd_hist")
    if hist is not None and hist > 0:
        score += 3               # was +4
    if feature_flag(features, "vol_breakout") and (features.get("ret_5d") or 0) > 0:
        score += 3               # was +5
    # 60 日高位 → 追高风险，扣分（was +4 奖励）
    h60 = features.get("high_60d_pct")
    if h60 is not None:
        if h60 >= 98:
            score -= 8          # 极端高位
        elif h60 >= 95:
            score -= 5          # 追高风险
    cvm = features.get("close_vs_ma20")
    if cvm is not None and -5 <= cvm <= 2:
        score += 4               # was +2，反转因子加强
    # 缩量上涨 = 诱多（ML 验证 vol_ratio 是前五大特征）
    vol_r = features.get("vol_ratio20")
    if ret20 is not None and vol_r is not None:
        if ret20 > 3 and vol_r < 0.8:
            score -= 8           # 涨了但缩量→诱多概率高
        elif ret20 > 0 and vol_r < 0.6:
            score -= 5           # 微涨但极度缩量→动能衰竭
        elif ret20 < -7 and vol_r > 1.5:
            score += 3           # 下跌放量→恐慌出清，均值回归机会
    return max(0.0, min(100.0, score))
