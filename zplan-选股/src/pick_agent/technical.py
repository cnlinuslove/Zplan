"""技术面打分与信号（KDJ / 均线 / MACD / 量价）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from zplan_shared.features import enrich_bars, feature_flag, latest_features, suggested_price_levels
from zplan_shared.market import get_bars


@dataclass
class TechnicalSnapshot:
    ts_code: str
    bars: int
    as_of: str | None
    close: float | None
    features: dict[str, float | None] = field(default_factory=dict)
    trend: dict[str, Any] = field(default_factory=dict)
    kdj: dict[str, Any] = field(default_factory=dict)
    macd: dict[str, Any] = field(default_factory=dict)
    volume: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    signals: list[str] = field(default_factory=list)
    verdict: str = "中性"


def analyze_technical(ts_code: str, *, min_bars: int = 60) -> TechnicalSnapshot:
    bars = get_bars(ts_code)
    snap = TechnicalSnapshot(ts_code=ts_code, bars=len(bars), as_of=None, close=None)
    if len(bars) < min_bars:
        snap.verdict = "数据不足"
        snap.signals.append(f"日线不足 {min_bars} 根（当前 {len(bars)}）")
        return snap

    enriched = enrich_bars(bars)
    snap.as_of = str(enriched.index[-1])
    snap.close = float(enriched["close"].iloc[-1]) if pd.notna(enriched["close"].iloc[-1]) else None
    snap.features = latest_features(enriched)

    f = snap.features
    score = 50.0
    signals: list[str] = []

    # --- 趋势：均线多头 ---
    ma5, ma20, ma60 = f.get("ma5"), f.get("ma20"), f.get("ma60")
    close = f.get("close")
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            score += 8           # was +12，减弱追涨偏好
            signals.append("均线多头排列（MA5>MA20>MA60）")
            snap.trend["alignment"] = "bull"
        elif ma5 < ma20 < ma60:
            score -= 12
            signals.append("均线空头排列")
            snap.trend["alignment"] = "bear"
        else:
            snap.trend["alignment"] = "mixed"
    above_ma20 = feature_flag(f, "above_ma20")
    if close and ma20:
        snap.trend["above_ma20"] = above_ma20 if f.get("above_ma20") is not None else close > ma20
        if snap.trend["above_ma20"]:
            score += 4
        else:
            score -= 4

    if feature_flag(f, "ma5_cross_ma20"):
        score += 10
        signals.append("MA5 上穿 MA20")
        snap.trend["ma_cross"] = "golden"

    slope = f.get("ma20_slope_5d")
    if slope is not None:
        snap.trend["ma20_slope_5d"] = slope
        if slope > 0.5:
            score += 3
        elif slope < -0.5:
            score -= 3

    h60 = f.get("high_60d_pct")
    if h60 is not None:
        snap.trend["high_60d_pct"] = h60
        if h60 >= 98:
            score -= 8
            signals.append(f"极端高位 ({h60:.1f}%)，追高风险极大")
        elif h60 >= 95:
            score -= 6
            signals.append(f"接近60日高点 ({h60:.1f}%)，追高风险")
        elif h60 >= 90:
            score -= 3
            signals.append(f"60日高位 ({h60:.1f}%)，注意追高风险")

    dd = f.get("drawdown_20d_pct")
    if dd is not None:
        snap.trend["drawdown_20d_pct"] = dd
        if -20 <= dd <= -8:
            score += 5
            signals.append("20日显著回撤，低吸机会")
        elif -8 < dd <= -3:
            score += 2

    ret20 = f.get("ret_20d")
    if ret20 is not None:
        snap.trend["ret_20d_pct"] = ret20
        if ret20 > 12:
            score -= 16
            signals.append(f"20日涨幅过高 ({ret20:.1f}%)，追高风险极大")
        elif ret20 > 8:
            score -= 12
            signals.append(f"20日已涨 {ret20:.1f}%，注意追高")
        elif ret20 > 5:
            score -= 6
            signals.append(f"20日涨幅 {ret20:.1f}%，不宜追涨")
        elif ret20 > 3:
            score -= 2
        elif -3 <= ret20 <= 3:
            score += 5
            signals.append("20日横盘/低吸区")
        elif ret20 < -10:
            score += 5            # 深度超卖→均值回归机会（ML r=-0.56）
            signals.append(f"20日深度回调 ({ret20:.1f}%)，均值回归机会")
        elif ret20 < -7:
            score += 3            # 中度回调

    # --- KDJ ---
    k, d, j = f.get("kdj_k"), f.get("kdj_d"), f.get("kdj_j")
    snap.kdj = {"k": k, "d": d, "j": j}
    if k is not None and d is not None:
        if k < 20 and d < 20:
            score += 8
            signals.append("KDJ 超卖区")
            snap.kdj["zone"] = "oversold"
        elif k > 80 and d > 80:
            score -= 14           # was -10，加强超买惩罚
            signals.append("KDJ 超买区")
            snap.kdj["zone"] = "overbought"
        else:
            snap.kdj["zone"] = "neutral"

        if feature_flag(f, "kdj_golden_cross"):
            score += 10
            signals.append("KDJ 金叉")
            snap.kdj["cross"] = "golden"
        elif feature_flag(f, "kdj_death_cross"):
            score -= 10
            signals.append("KDJ 死叉")
            snap.kdj["cross"] = "death"

    # --- MACD ---
    hist = f.get("macd_hist")
    snap.macd = {"hist": hist, "dif": f.get("macd_dif"), "dea": f.get("macd_dea")}
    if hist is not None:
        if hist > 0:
            score += 5
        else:
            score -= 3
        if feature_flag(f, "macd_cross_up"):
            score += 8
            signals.append("MACD 柱由负转正")
            snap.macd["hist_turn"] = "up"

    # --- RSI ---
    rsi = f.get("rsi14")
    if rsi is not None:
        if 40 <= rsi <= 65:
            score += 4
        elif rsi > 75:
            score -= 6
            signals.append(f"RSI 偏高 ({rsi:.1f})")
        elif rsi < 30:
            score += 3
            signals.append(f"RSI 超卖 ({rsi:.1f})")

    # --- 量价 ---
    vol_ratio = f.get("vol_ratio20")
    turnover = f.get("turnover_rate")
    atr_pct = f.get("atr_pct")
    snap.volume = {
        "vol_ratio20": vol_ratio,
        "turnover_rate": turnover,
        "vol_breakout": feature_flag(f, "vol_breakout"),
        "atr_pct": atr_pct,
    }
    if feature_flag(f, "vol_breakout"):
        if ret20 and ret20 > 0:
            score += 5
            signals.append("放量上涨")
        else:
            score -= 3
            signals.append("放量下跌")
    if atr_pct is not None and atr_pct > 6:
        score -= 4
        signals.append(f"波动偏高 (ATR% {atr_pct:.1f})")

    # 缩量上涨检测（ML 验证 vol_ratio 是前五大特征，与 ret_20d 组合预测反转）
    if vol_ratio is not None and ret20 is not None:
        if ret20 > 3 and vol_ratio < 0.8:
            score -= 8
            signals.append(f"缩量上涨 (量比{vol_ratio:.2f})，疑似诱多")
        elif ret20 > 0 and vol_ratio < 0.6:
            score -= 5
            signals.append(f"极度缩量 ({vol_ratio:.2f})，动能衰竭")
        elif ret20 < -7 and vol_ratio > 1.5:
            score += 4
            signals.append(f"下跌放量 (量比{vol_ratio:.2f})，恐慌出清")

    snap.score = max(0.0, min(100.0, round(score, 1)))
    snap.signals = signals
    if snap.score >= 68:
        snap.verdict = "偏多"
    elif snap.score <= 42:
        snap.verdict = "偏空"
    else:
        snap.verdict = "中性"
    return snap


def price_levels(bars: pd.DataFrame) -> dict[str, float | None]:
    """支撑/阻力与建议价位（基于近端高低点与均线）。"""
    return suggested_price_levels(bars)
