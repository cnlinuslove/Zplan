"""综合打分：技术 + 财务 + 资讯 + 板块相对强弱 + 分时。"""
from __future__ import annotations

from typing import Any

import pandas as pd

from zplan_shared.features import feature_flag

from pick_agent.strategy import PickStrategy
from pick_agent.technical import TechnicalSnapshot, analyze_technical


def financial_score_from_rows(rows: list[dict[str, Any]]) -> tuple[float | None, str]:
    """财务多维评分（对标机构研报财务分析深度）。

    评分维度（总分 100，base=50）：
    - 盈利能力（30分）：净利润正负、净利率趋势、ROE水平
    - 成长性（20分）：营收增速、利润增速
    - 估值合理性（±15分）：PE/PB相对合理区间
    - 数据完整性（±10分）：至少3期数据可得性
    """
    if not rows:
        return None, "财报缺失（需 Phase D：股价 Agent 季报 ETL）"

    latest = rows[0]
    score = 50.0
    notes: list[str] = []
    plus_items: list[str] = []
    minus_items: list[str] = []

    # ── 1. 盈利能力（30 分）──
    np_latest = latest.get("net_profit")
    rev_latest = latest.get("revenue")

    if np_latest is not None:
        if np_latest > 0:
            score += 15
            plus_items.append(f"净利润 {np_latest/1e8:.2f} 亿（盈利）")
            # 净利率
            if rev_latest and rev_latest > 0:
                margin = np_latest / rev_latest * 100
                if margin > 15:
                    score += 10
                    plus_items.append(f"净利率 {margin:.1f}%（极强）")
                elif margin > 5:
                    score += 5
                    plus_items.append(f"净利率 {margin:.1f}%（良好）")
                elif margin < 2:
                    score -= 5
                    minus_items.append(f"净利率仅 {margin:.2f}%（极薄）")
                else:
                    notes.append(f"净利率 {margin:.1f}%")
        else:
            score -= 15
            minus_items.append(f"净利润 {np_latest/1e8:.2f} 亿（亏损）")

    # ROE
    roe_latest = latest.get("roe")
    if roe_latest is not None:
        if roe_latest > 15:
            score += 5
            plus_items.append(f"ROE {roe_latest:.1f}%（优秀）")
        elif roe_latest > 5:
            score += 2
            plus_items.append(f"ROE {roe_latest:.1f}%（尚可）")
        elif roe_latest < 0:
            score -= 5
            minus_items.append(f"ROE {roe_latest:.1f}%（负值）")

    # ── 2. 成长性（20 分）──
    if len(rows) >= 2:
        rev_prev = rows[1].get("revenue")
        np_prev = rows[1].get("net_profit")

        # 营收增速
        if rev_latest and rev_prev and rev_prev > 0:
            rev_growth = (rev_latest / rev_prev - 1) * 100
            if rev_growth > 20:
                score += 10
                plus_items.append(f"营收增速 {rev_growth:.1f}%（高增）")
            elif rev_growth > 5:
                score += 5
                plus_items.append(f"营收增速 {rev_growth:.1f}%（稳健）")
            elif rev_growth < -10:
                score -= 5
                minus_items.append(f"营收增速 {rev_growth:.1f}%（下滑）")
            else:
                notes.append(f"营收增速 {rev_growth:.1f}%")

        # 利润增速
        if np_latest and np_prev and np_prev != 0:
            np_growth = (np_latest / np_prev - 1) * 100 if np_prev > 0 else None
            if np_growth is not None:
                if np_growth > 100:
                    score += 10
                    plus_items.append(f"利润增速 {np_growth:.0f}%（爆发）")
                elif np_growth > 20:
                    score += 5
                    plus_items.append(f"利润增速 {np_growth:.0f}%（增长）")
                elif np_growth < -30:
                    score -= 8
                    minus_items.append(f"利润增速 {np_growth:.0f}%（恶化）")
        # 扭亏为盈特别加分
        if np_latest and np_latest > 0 and np_prev and np_prev < 0:
            score += 10
            plus_items.append("扭亏为盈（重大改善）")

    # ── 3. 估值合理性（±15 分）──
    pe = latest.get("pe_ttm")
    pb = latest.get("pb")
    if pe is not None and pe > 0:
        if pe > 100:
            score -= 8
            minus_items.append(f"PE {pe:.0f}x（极高估值）")
        elif pe > 50:
            score -= 3
            minus_items.append(f"PE {pe:.0f}x（偏高）")
        elif pe < 15:
            score += 8
            plus_items.append(f"PE {pe:.0f}x（低估值）")
        elif pe < 25:
            score += 3
            plus_items.append(f"PE {pe:.0f}x（合理）")
    if pb is not None and pb > 0:
        if pb > 10:
            score -= 5
            minus_items.append(f"PB {pb:.1f}x（极高）")
        elif pb < 1:
            score += 5
            plus_items.append(f"PB {pb:.2f}x（破净）")

    # ── 4. 多期趋势稳定性（±10 分）──
    if len(rows) >= 3:
        # 连续营收增长
        revs = [r.get("revenue") for r in rows[:3] if r.get("revenue")]
        if len(revs) >= 3 and all(revs[i] > revs[i+1] for i in range(len(revs)-1)):
            score += 5
            plus_items.append("连续3期营收增长")
        # 连续亏损
        nps = [r.get("net_profit") for r in rows[:3] if r.get("net_profit") is not None]
        if len(nps) >= 3 and all(np <= 0 for np in nps):
            score -= 5
            minus_items.append("连续3期亏损/无盈利")

    # ── 组装结果 ──
    final_score = max(0.0, min(100.0, score))
    parts = []
    if plus_items:
        parts.append("✓ " + "；".join(plus_items))
    if minus_items:
        parts.append("✗ " + "；".join(minus_items))
    if notes:
        parts.append("；".join(notes))
    return final_score, " | ".join(parts) if parts else "财报粗评"


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
