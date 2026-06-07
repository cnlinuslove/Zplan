"""跨市场（A+H）策略信号 — H 股领先滞后 → A 股选股加成。

核心发现：H 股普遍领先 A 股 1-5 天（相关系数 0.89-0.94）。
本模块将这一领先关系量化为选股 Alpha 信号。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from zplan_shared.ah_analysis import _close_on
from zplan_shared.market import get_bars, latest_trade_date
from zplan_shared.models import AhCrossRef, DailyPrice, SessionLocal, init_db

logger = logging.getLogger(__name__)


def _hk_lead_return(a_code: str, hk_code: str, lead_days: int = 1) -> float | None:
    """H 股前 N 日收益 → 作为 A 股今日收益的领先信号。"""
    end = latest_trade_date(market="hk")
    if end is None:
        return None
    start = end - timedelta(days=lead_days * 3)

    hk = get_bars(hk_code, start=start, end=end, market="hk")
    if hk.empty or len(hk) < lead_days + 2:
        return None

    if lead_days == 0:
        # 当日 H 股收益
        ret = hk["close"].pct_change().iloc[-1]
    else:
        # H 股前 lead_days 日累计收益
        ret = hk["close"].iloc[-1] / hk["close"].iloc[-(lead_days + 1)] - 1

    return float(ret) if not np.isnan(ret) else None


def hk_momentum_signal(a_code: str, *, lookback: list[int] | None = None) -> dict[str, Any]:
    """对 A 股查询其 H 股的动量领先信号。

    返回前端 1/3/5 日的 H 股收益及综合信号强度。
    """
    init_db()
    if lookback is None:
        lookback = [1, 3, 5]

    # 查配对
    with SessionLocal() as session:
        row = session.execute(
            select(AhCrossRef.hk_code, AhCrossRef.ah_premium_pct).where(
                AhCrossRef.a_code == a_code
            )
        ).first()

    if row is None:
        return {"has_hk_pair": False}

    hk_code, premium = row[0], row[1]
    signals = {}
    for d in lookback:
        ret = _hk_lead_return(a_code, hk_code, lead_days=d)
        if ret is not None:
            signals[f"hk_ret_{d}d"] = round(ret * 100, 2)

    # 综合强度：最近 1 日 H 股收益 + 方向一致性
    composite = 0.0
    if signals:
        rets = list(signals.values())
        composite = sum(rets) / len(rets)
        # 所有方向一致 → 增强
        if all(r > 0 for r in rets) or all(r < 0 for r in rets):
            composite *= 1.3

    return {
        "has_hk_pair": True,
        "hk_code": hk_code,
        "ah_premium_pct": premium,
        "hk_signals": signals,
        "hk_composite": round(composite, 2),
    }


def cross_market_score(a_code: str) -> tuple[float, dict]:
    """跨市场 Alpha 分（0-10），加入选股综合评分。

    - H 股上涨 → A 股看涨信号 → 加分
    - H 股下跌 → A 股承压 → 减分
    - 溢价极值 → 均值回归 → 反向信号

    返回 (score, detail_dict)。
    """
    signal = hk_momentum_signal(a_code)
    if not signal.get("has_hk_pair"):
        return 5.0, {"reason": "无 H 股配对"}

    score = 5.0
    reasons = []

    # 1) H 股动量信号（核心）
    comp = signal.get("hk_composite", 0)
    if comp > 1:
        bonus = min(3, comp)
        score += bonus
        reasons.append(f"H股动量↑ +{bonus:.1f}")
    elif comp < -1:
        penalty = max(-3, comp)
        score += penalty
        reasons.append(f"H股动量↓ {penalty:.1f}")

    # 2) 溢价均值回归信号（次要）
    premium = signal.get("ah_premium_pct")
    if premium is not None:
        hist = _premium_history_from_db(a_code, signal["hk_code"])
        if len(hist) >= 20:
            mean = np.mean(hist)
            std = np.std(hist) or 0.01
            z = (premium - mean) / std
            if z < -2:
                # H 股极端便宜 → 港股可能反弹 → A 股也利好
                score += 1.5
                reasons.append(f"H股极端折价(z={z:.1f}) +1.5")
            elif z > 2:
                score -= 1.0
                reasons.append(f"H股极端溢价(z={z:.1f}) -1.0")

    score = max(0, min(10, score))
    return round(score, 1), {"reasons": reasons, **signal}


def _premium_history_from_db(a_code: str, hk_code: str, lookback: int = 60) -> list[float]:
    """从库内计算 AH 溢价历史序列。"""
    end = latest_trade_date(market="a")
    if end is None:
        return []
    start = end - timedelta(days=lookback * 2)
    a = get_bars(a_code, start=start, end=end, market="a")
    hk = get_bars(hk_code, start=start, end=end, market="hk")
    if a.empty or hk.empty:
        return []
    common = a.index.intersection(hk.index)
    if len(common) < 10:
        return []
    premium = (hk.loc[common, "close"] / a.loc[common, "close"] - 1) * 100
    return premium.tail(lookback).tolist()


def cross_market_scan(top_n: int = 30) -> pd.DataFrame:
    """全量 A+H 对的跨市场信号扫描。

    返回按 ``cross_score`` 排序的 A 股列表（Top = H 股动量最强）。
    """
    init_db()
    with SessionLocal() as session:
        pairs = session.execute(
            select(AhCrossRef.a_code, AhCrossRef.a_name, AhCrossRef.hk_code)
        ).all()

    results = []
    for a_code, a_name, hk_code in pairs:
        score, detail = cross_market_score(a_code)
        results.append({
            "a_code": a_code,
            "a_name": a_name,
            "hk_code": hk_code,
            "cross_score": score,
            "hk_signals": detail.get("hk_signals", {}),
            "reasons": detail.get("reasons", []),
            "ah_premium_pct": detail.get("ah_premium_pct"),
        })

    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("cross_score", ascending=False)
    return df.head(top_n)
