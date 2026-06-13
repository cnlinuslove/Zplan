"""规则引擎 v2 — 反转+质量+趋势质量因子，替代纯动量追涨。

与 v1 (scoring.py) 并存，输入签名相同（``features: dict[str, float|None]``），
可直接在回测脚本中替换 ``quick_technical_score`` 做 A/B 对比。

因子设计原则：
1. 每个因子独立可测 — 单独跑回测验证 Spearman ρ
2. 反转优先 — A 股动量因子已证实无效（ρ≈0）
3. 输出 [-50, +50]，便于等权合成
"""
from __future__ import annotations

import math
from typing import Any, Callable

from zplan_shared.features import feature_flag

# ── 类型 ──────────────────────────────────────────────

FactorFn = Callable[[dict[str, float | None]], float]
"""因子函数签名：接受 features dict，返回 [-50, 50] 的得分贡献。"""


# ── 工具函数 ──────────────────────────────────────────

def _clamp(v: float, lo: float = -50.0, hi: float = 50.0) -> float:
    return max(lo, min(hi, v))


def _safe_float(feat: dict[str, float | None], key: str) -> float | None:
    v = feat.get(key)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


# ═══════════════════════════════════════════════════════
# 族 1：反转因子（Reversal）
# ═══════════════════════════════════════════════════════

def factor_ret_20d_reversal(feat: dict[str, float | None]) -> float:
    """20 日跌幅越大，反弹概率越高——但超过 25% 后递减（基本面崩盘风险）。

    - ret_20d <= -25% → +25（不再无限加分，超跌可能是基本面问题）
    - ret_20d [ -25%, -15% ] → +20 ~ +25（最佳反弹区间）
    - ret_20d [ -15%, -8% ] → +12 ~ +20
    - ret_20d [ -8%, -3% ] → +5 ~ +12
    - ret_20d [ -3%, +3% ] → 0
    - ret_20d [ +3%, +8% ] → -5 ~ -15
    - ret_20d [ +8%, +15% ] → -15 ~ -25
    - ret_20d >= +15% → -25 ~ -35
    """
    r = _safe_float(feat, "ret_20d")
    if r is None:
        return 0.0
    if r <= -25:
        return 25.0  # 封顶，不再奖励极端超跌
    if r <= -15:
        return _clamp(20.0 + (abs(r) - 15) / 10 * 5)
    if r <= -8:
        return _clamp(12.0 + (abs(r) - 8) / 7 * 8)
    if r <= -3:
        return _clamp(5.0 + (abs(r) - 3) / 5 * 7)
    if r <= 3:
        return 0.0
    if r <= 8:
        return _clamp(-5.0 - (r - 3) / 5 * 10)
    if r <= 15:
        return _clamp(-15.0 - (r - 8) / 7 * 10)
    return _clamp(-25.0 - (r - 15) * 1.5)


def factor_ret_20d_trend(feat: dict[str, float | None]) -> float:
    """温和动量：奖励缓涨、惩罚急涨和所有下跌。

    - ret_20d [ +1%, +5% ] → +15 ~ +25（黄金区间，温和上涨）
    - ret_20d [ 0%, +1% ] → +5 ~ +15
    - ret_20d [ -3%, 0% ] → 0 ~ -5
    - ret_20d [ -8%, -3% ] → -5 ~ -15
    - ret_20d < -8% → -15 ~ -25（下跌趋势）
    - ret_20d > +8% → -5 ~ -20（追高风险）
    """
    r = _safe_float(feat, "ret_20d")
    if r is None:
        return 0.0
    if 1.0 <= r <= 5.0:
        return _clamp(15.0 + (r - 1) / 4 * 10)
    if 0 <= r < 1.0:
        return _clamp(5.0 + r / 1 * 10)
    if -3.0 <= r < 0:
        return _clamp(0.0 + (3 + r) / 3 * (-5))
    if -8.0 <= r < -3.0:
        return _clamp(-5.0 + (abs(r) - 3) / 5 * (-10))
    if r < -8.0:
        return _clamp(-15.0 - (abs(r) - 8) / 12 * 10, lo=-25)
    if r > 8.0:
        return _clamp(-5.0 - (r - 8) / 7 * 15, lo=-20)
    return 0.0


def factor_drawdown_reversal(feat: dict[str, float | None]) -> float:
    """20 日回撤幅度越大，超卖反弹潜力越高。

    drawdown_20d_pct 是负值（如 -12%），绝对值越大加分越多。
    """
    dd = _safe_float(feat, "drawdown_20d_pct")
    if dd is None:
        return 0.0
    if dd <= -20:
        return 35.0
    if dd <= -15:
        return _clamp(25.0 + (abs(dd) - 15) / 5 * 10)
    if dd <= -10:
        return _clamp(15.0 + (abs(dd) - 10) / 5 * 10)
    if dd <= -5:
        return _clamp(5.0 + (abs(dd) - 5) / 5 * 10)
    if dd <= -2:
        return _clamp(0.0 + (abs(dd) - 2) / 3 * 5)
    return 0.0  # 未回撤不扣分


def factor_rsi_oversold(feat: dict[str, float | None]) -> float:
    """RSI 超卖加分，超买扣分（反转视角）。

    - RSI < 25: +25 ~ +35
    - RSI 25-40: +5 ~ +25
    - RSI 40-60: 0
    - RSI 60-75: -5 ~ -15
    - RSI > 75: -15 ~ -25
    """
    rsi = _safe_float(feat, "rsi14")
    if rsi is None:
        return 0.0
    if rsi < 20:
        return 35.0
    if rsi < 25:
        return _clamp(25.0 + (25 - rsi) / 5 * 10)
    if rsi < 40:
        return _clamp(5.0 + (40 - rsi) / 15 * 20)
    if rsi <= 60:
        return 0.0
    if rsi <= 75:
        return _clamp(-5.0 - (rsi - 60) / 15 * 10)
    return _clamp(-15.0 - (rsi - 75) / 25 * 10)


def factor_kdj_oversold(feat: dict[str, float | None]) -> float:
    """KDJ 超卖/超买（J 值视角）。

    - J < 0: +20 ~ +30
    - J 0-20: +5 ~ +20
    - J 80-100: -5 ~ -15
    - J > 100: -15 ~ -25
    """
    j = _safe_float(feat, "kdj_j")
    k = _safe_float(feat, "kdj_k")
    if j is None and k is None:
        return 0.0

    score = 0.0
    if j is not None:
        if j < 0:
            score += _clamp(20.0 + abs(j) / 10 * 10, hi=30)
        elif j < 20:
            score += _clamp(5.0 + (20 - j) / 20 * 15)
        elif j > 100:
            score += _clamp(-15.0 - (j - 100) / 10 * 10, lo=-25)
        elif j > 80:
            score += _clamp(-5.0 - (j - 80) / 20 * 10)
    if k is not None:
        if k < 20:
            score += 5.0
        elif k > 80:
            score -= 5.0
    return _clamp(score)


def factor_close_vs_ma20(feat: dict[str, float | None]) -> float:
    """价格低于 MA20 越多，均值回归潜力越大。

    - close_vs_ma20 < -15%: +20
    - close_vs_ma20 [-15%, -5%]: +5 ~ +20
    - close_vs_ma20 [-5%, -2%]: +2 ~ +5
    - close_vs_ma20 [-2%, +2%]: 0
    - close_vs_ma20 > +5%: 轻微扣分（已偏离均线过高）
    """
    cvm = _safe_float(feat, "close_vs_ma20")
    if cvm is None:
        return 0.0
    if cvm < -15:
        return 20.0
    if cvm < -5:
        return _clamp(5.0 + (abs(cvm) - 5) / 10 * 15)
    if cvm < -2:
        return _clamp(2.0 + (abs(cvm) - 2) / 3 * 3)
    if cvm <= 2:
        return 0.0
    if cvm <= 5:
        return -2.0
    if cvm <= 10:
        return _clamp(-2.0 - (cvm - 5) / 5 * 8)
    return -10.0


def factor_days_since_high(feat: dict[str, float | None]) -> float:
    """距 60 日高点越远（即涨幅已回吐），反弹空间越大。

    high_60d_pct < 80% 表示从 60 日高点跌了 20%+。
    回测验证：near_60d_high=10/10，需加强高位扣分。
    """
    h60 = _safe_float(feat, "high_60d_pct")
    if h60 is None:
        return 0.0
    if h60 < 70:
        return 30.0
    if h60 < 80:
        return _clamp(10.0 + (80 - h60) / 10 * 20)
    if h60 < 85:
        return _clamp(2.0 + (85 - h60) / 5 * 8)
    if h60 <= 90:
        return 0.0
    if h60 <= 95:
        return _clamp(-5.0 - (h60 - 90) / 5 * 10, lo=-15)
    return -20.0  # 接近最高点风险极大


def factor_stabilization(feat: dict[str, float | None]) -> float:
    """5 日跌幅收窄或转正 → 超卖后企稳信号。

    - ret_5d > +3%: 反弹确认 +20
    - ret_5d [0, +3%]: 止跌企稳 +10
    - ret_5d [-3%, 0]: 跌势减缓 +5
    - ret_5d [-8%, -3%]: 仍在下跌 -5
    - ret_5d <= -8%: 加速下跌 -15

    这个因子确保只在超卖后出现企稳迹象时才买入，避免接飞刀。
    """
    r5 = _safe_float(feat, "ret_5d")
    if r5 is None:
        return 0.0
    if r5 > 5:
        return 25.0
    if r5 > 3:
        return _clamp(20.0 + (r5 - 3) / 2 * 5)
    if r5 >= 0:
        return _clamp(10.0 + r5 / 3 * 10)
    if r5 >= -3:
        return _clamp(5.0 + (3 + r5) / 3 * 5)
    if r5 >= -8:
        return _clamp(-5.0 - (abs(r5) - 3) / 5 * 5)
    return _clamp(-10.0 - (abs(r5) - 8) / 7 * 5, lo=-20)


# ═══════════════════════════════════════════════════════
# 族 2：趋势质量因子（Trend Quality）
# ═══════════════════════════════════════════════════════

def factor_volume_health(feat: dict[str, float | None]) -> float:
    """量价配合：放量上涨 + 缩量下跌 = 健康趋势。

    使用 vol_breakout (量比 >= 1.5) 和 ret_5d 的组合。
    """
    breakout = feature_flag(feat, "vol_breakout")
    ret5 = _safe_float(feat, "ret_5d")

    if not breakout or ret5 is None:
        return 0.0

    # 放量 + 正收益 → 强信号
    if ret5 > 0:
        bonus = min(15.0, ret5 * 2 + 5)
        return _clamp(bonus)
    # 放量 + 负收益 → 抛售信号（反向）
    elif ret5 < -3:
        return _clamp(ret5 * 1.5, lo=-15)

    return 0.0


def factor_ma_slope_health(feat: dict[str, float | None]) -> float:
    """MA20 斜率适中最佳；太平（无趋势）或太陡（追高风险）扣分。

    - 斜率 0.5% ~ 3%: 温和上升 → +5 ~ +10
    - 斜率 -1% ~ 0.5%: 整理/微跌 → 0
    - 斜率 > 5%: 急涨 → -10 ~ -15
    - 斜率 < -3%: 急跌 → 反转因子会另外加分，此因子中性
    """
    slope = _safe_float(feat, "ma20_slope_5d")
    if slope is None:
        return 0.0
    if 0.5 <= slope <= 3.0:
        return _clamp(5.0 + (slope - 0.5) / 2.5 * 5, hi=10)
    if 3.0 < slope <= 5.0:
        return _clamp(-2.0 - (slope - 3) / 2 * 8, lo=-10)
    if slope > 5.0:
        return -15.0
    if -1.0 <= slope < 0.5:
        return 0.0
    if -3.0 <= slope < -1.0:
        return 0.0  # 反转因子覆盖
    return 0.0


def factor_low_volatility(feat: dict[str, float | None]) -> float:
    """低波动溢价：ATR% 低的股票更稳定，在震荡市中表现更好。

    atr_pct < 2%: +10
    atr_pct 2-3%: +5
    atr_pct 3-5%: 0
    atr_pct 5-7%: -5
    atr_pct > 7%: -10
    """
    atr = _safe_float(feat, "atr_pct")
    if atr is None:
        return 0.0
    if atr < 2.0:
        return 10.0
    if atr < 3.0:
        return _clamp(10.0 - (atr - 2) / 1 * 5)
    if atr <= 5.0:
        return 0.0
    if atr <= 7.0:
        return _clamp(-(atr - 5) / 2 * 5, lo=-5)
    return _clamp(-5.0 - (atr - 7) / 3 * 5, lo=-10)


# ═══════════════════════════════════════════════════════
# 族 3：资金流向因子（Capital Flow）
# ═══════════════════════════════════════════════════════

def factor_volume_direction(feat: dict[str, float | None]) -> float:
    """量价方向：高量+正收益=资金流入，高量+负收益=资金流出。

    这是 A 股短线最重要的因子之一——量在价先。
    """
    vol_ratio = _safe_float(feat, "vol_ratio20")
    ret5 = _safe_float(feat, "ret_5d")

    if vol_ratio is None or ret5 is None:
        return 0.0

    # 核心逻辑：量 * 方向 = 资金信号
    direction = 1.0 if ret5 > 0 else (-1.0 if ret5 < 0 else 0.0)
    intensity = min(abs(vol_ratio - 1.0), 2.0)  # 量偏离均值程度

    score = direction * intensity * 10.0

    # 极端情况加强
    if vol_ratio >= 2.0 and ret5 > 2:
        score += 5.0  # 放量明显上涨 → 强资金流入
    elif vol_ratio >= 2.0 and ret5 < -2:
        score -= 10.0  # 放量明显下跌 → 资金出逃

    return _clamp(score, -20, 20)


def factor_concept_heat(feat: dict[str, float | None]) -> float:
    """概念热度：股票所属概念的平均 ret_20d。

    概念热度 > 0 → 板块资金流入，热度高说明市场认可该主题。
    从回测脚本注入特征 _concept_heat。
    """
    heat = _safe_float(feat, "_concept_heat")
    if heat is None:
        return 0.0
    # 映射：概念热度 5% → +15; -5% → -15
    return _clamp(heat * 3.0, -20, 20)


def factor_sector_momentum(feat: dict[str, float | None]) -> float:
    """行业动量：股票所属行业（申万一级）的平均 ret_20d 在所有行业中的排名。

    板块轮动的核心信号——压对板块比压对个股更重要。
    从 rule_universe 注入特征 _industry_heat（行业平均 ret_20d）和 _industry_rank_pct（行业排名百分位）。

    设计原理：
    - 行业排名前 20%（领涨板块）→ +20 ~ +30（强板块溢价）
    - 行业排名 20-40% → +5 ~ +20
    - 行业排名 40-60%（中性）→ -5 ~ +5
    - 行业排名 60-80% → -5 ~ -15
    - 行业排名 80-100%（领跌板块）→ -15 ~ -30（弱板块折价）

    比 concept_heat 更强的信号：行业分类更稳定、每只股票有且只有一个行业、覆盖全市场。
    """
    rank_pct = _safe_float(feat, "_industry_rank_pct")
    if rank_pct is None:
        # 回退：如果有 _industry_heat 但没有排名，用绝对热度
        heat = _safe_float(feat, "_industry_heat")
        if heat is None:
            return 0.0
        return _clamp(heat * 3.0, -25, 25)

    # rank_pct: 0=最弱行业, 100=最强行业
    if rank_pct >= 80:
        return _clamp(20.0 + (rank_pct - 80) / 20 * 10, hi=30)
    if rank_pct >= 60:
        return _clamp(5.0 + (rank_pct - 60) / 20 * 15)
    if rank_pct >= 40:
        return _clamp(-5.0 + (rank_pct - 40) / 20 * 10)
    if rank_pct >= 20:
        return _clamp(-15.0 + (rank_pct - 20) / 20 * 10)
    return _clamp(-30.0 + rank_pct / 20 * 15, lo=-30)


def factor_industry_leader(feat: dict[str, float | None]) -> float:
    """行业龙头溢价：股票在自身行业内的 ret_20d 排名。

    同一行业内，龙头股（涨幅领先的）往往能持续获得资金关注。
    从 rule_universe 注入特征 _industry_relative_rank（行业内排名百分位，0-100）。
    """
    rel_rank = _safe_float(feat, "_industry_relative_rank")
    if rel_rank is None:
        return 0.0
    if rel_rank >= 80:
        return _clamp(10.0 + (rel_rank - 80) / 20 * 10, hi=20)
    if rel_rank >= 60:
        return _clamp(0.0 + (rel_rank - 60) / 20 * 10)
    if rel_rank >= 40:
        return _clamp(-5.0 + (rel_rank - 40) / 20 * 5)
    if rel_rank >= 20:
        return _clamp(-10.0 + (rel_rank - 20) / 20 * 5)
    return _clamp(-15.0 + rel_rank / 20 * 5, lo=-20)


def factor_concept_diversity(feat: dict[str, float | None]) -> float:
    """概念多样性：概念数越多，潜在催化事件越多。

    0-2 个概念: 0
    3-5: +3
    6-10: +6
    >10: +8
    """
    count = _safe_float(feat, "_concept_count")
    if count is None:
        return 0.0
    if count <= 2:
        return 0.0
    if count <= 5:
        return 3.0
    if count <= 10:
        return 6.0
    return 8.0


def factor_turnover_attention(feat: dict[str, float | None]) -> float:
    """换手率适中：一定换手说明市场关注，但过高（>15%）是投机/出货。

    换手率 2-8%: 适度活跃 → +5
    <1%: 无人关注 → -3
    >15%: 过度投机 → -8
    """
    turnover = _safe_float(feat, "turnover_rate")
    if turnover is None:
        return 0.0
    if 2.0 <= turnover <= 8.0:
        return 5.0
    if 1.0 <= turnover < 2.0:
        return 0.0
    if 8.0 < turnover <= 15.0:
        return _clamp(-(turnover - 8) / 7 * 5, lo=-5)
    if turnover > 15.0:
        return -8.0
    return -3.0  # < 1%


# ═══════════════════════════════════════════════════════
# 族 4：质量因子（Quality）— 需要额外传入财务数据
# ═══════════════════════════════════════════════════════

_QUALITY_CACHE: dict[str, dict[str, Any]] = {}


def set_quality_cache(code: str, data: dict[str, Any]) -> None:
    """注入财务数据缓存（由回测脚本在评分前调用）。"""
    _QUALITY_CACHE[code] = data


def clear_quality_cache() -> None:
    _QUALITY_CACHE.clear()


def _get_quality(code: str) -> dict[str, Any]:
    return _QUALITY_CACHE.get(code, {})


def factor_profit_positive(feat: dict[str, float | None], code: str = "") -> float:
    """近两季净利润均为正 → +15。"""
    q = _get_quality(code)
    if not q:
        return 0.0
    recent = q.get("recent_profits", [])
    if len(recent) >= 2 and all(p is not None and p > 0 for p in recent[:2]):
        return 15.0
    if len(recent) >= 1 and (recent[0] is None or recent[0] <= 0):
        return -15.0
    return 0.0


def _pctile_to_score(pctile: float | None, max_score: float = 12.0) -> float:
    """板块内百分位 → 得分（0~100 百分位 → [-max_score, +max_score]）。

    在板块内排名越高，得分越高。反向：低于板块中位数的扣分。
    """
    if pctile is None or (isinstance(pctile, float) and math.isnan(pctile)):
        return 0.0
    # 映射：50% → 0, 100% → +max_score, 0% → -max_score
    return _clamp((pctile - 50) / 50 * max_score, -max_score, max_score)


def factor_revenue_growth(feat: dict[str, float | None], code: str = "") -> float:
    """营收增长板块内百分位：在同行中排名越高分越高。

    板块内百分位考虑：科技公司营收增速普遍高、制造业普遍低。
    """
    q = _get_quality(code)
    pctile = q.get("revenue_sector_pctile")
    return _pctile_to_score(pctile, max_score=15.0)


def factor_profit_growth(feat: dict[str, float | None], code: str = "") -> float:
    """利润增长板块内百分位。"""
    q = _get_quality(code)
    pctile = q.get("profit_sector_pctile")
    return _pctile_to_score(pctile, max_score=15.0)


def factor_profit_margin(feat: dict[str, float | None], code: str = "") -> float:
    """净利率板块内百分位。

    科技股：8% 净利率可能在板块内前 20%
    医药股：8% 净利率可能在板块内后 30%
    """
    q = _get_quality(code)
    pctile = q.get("margin_sector_pctile")
    return _pctile_to_score(pctile, max_score=12.0)


def factor_profit_stability(feat: dict[str, float | None], code: str = "") -> float:
    """利润稳定性：近 4 季净利润变异系数越低越稳定。"""
    q = _get_quality(code)
    profits = q.get("recent_profits", [])
    if len(profits) < 4:
        return 0.0
    valid = [p for p in profits[:4] if p is not None]
    if len(valid) < 3:
        return 0.0
    mean_v = sum(valid) / len(valid)
    if abs(mean_v) < 1e-8:
        return 0.0
    variance = sum((p - mean_v) ** 2 for p in valid) / len(valid)
    cv = math.sqrt(variance) / abs(mean_v)  # 变异系数
    if cv < 0.3:
        return 12.0
    if cv < 0.6:
        return _clamp(8.0 - (cv - 0.3) / 0.3 * 8)
    if cv < 1.0:
        return _clamp(0.0 - (cv - 0.6) / 0.4 * 5, lo=-5)
    if cv < 2.0:
        return _clamp(-5.0 - (cv - 1.0) / 1.0 * 5, lo=-10)
    return -10.0


# ═══════════════════════════════════════════════════════
# 族 5：筹码峰因子（Chip Distribution）
# ═══════════════════════════════════════════════════════

def factor_chip_profit_ratio(feat: dict[str, float | None]) -> float:
    """获利比例因子：高获利比例 → 抛压风险（反转视角）。

    获利比例表示有多少比例的筹码处于盈利状态。
    - profit_ratio > 80%: 绝大多数获利，抛压大 → -20 ~ -30
    - profit_ratio 60-80%: 多数获利 → -5 ~ -15
    - profit_ratio 40-60%: 均衡 → 0
    - profit_ratio 20-40%: 多数亏损 → +5 ~ +15
    - profit_ratio < 20%: 深度套牢，抛压枯竭 → +15 ~ +25
    """
    pr = _safe_float(feat, "_profit_ratio")
    if pr is None:
        return 0.0
    if pr < 15:
        return 25.0
    if pr < 20:
        return _clamp(15.0 + (20 - pr) / 5 * 10)
    if pr < 40:
        return _clamp(5.0 + (40 - pr) / 20 * 10)
    if pr <= 60:
        return 0.0
    if pr <= 80:
        return _clamp(-5.0 - (pr - 60) / 20 * 10)
    if pr <= 90:
        return _clamp(-15.0 - (pr - 80) / 10 * 15)
    return -30.0


def factor_chip_concentration(feat: dict[str, float | None]) -> float:
    """筹码集中度因子：集中度高 → 主力控盘，可能突破。

    concentration_90 越低表示筹码越集中（值域 0~1）。
    - < 0.10: 高度集中，强控盘 → +25
    - 0.10 ~ 0.15: 集中 → +15 ~ +25
    - 0.15 ~ 0.30: 较集中 → +5 ~ +15
    - 0.30 ~ 0.50: 轻度集中 → 0 ~ +5
    - 0.50 ~ 0.70: 分散 → -5 ~ 0
    - > 0.70: 高度分散 → -10
    """
    c90 = _safe_float(feat, "_concentration_90")
    if c90 is None:
        return 0.0
    if c90 < 0.10:
        return 25.0
    if c90 < 0.15:
        return _clamp(15.0 + (0.15 - c90) / 0.05 * 10)
    if c90 < 0.30:
        return _clamp(5.0 + (0.30 - c90) / 0.15 * 10)
    if c90 <= 0.50:
        return _clamp(0.0 + (0.50 - c90) / 0.20 * 5)
    if c90 <= 0.70:
        return _clamp(-5.0 + (0.70 - c90) / 0.20 * 5, lo=-5)
    return -10.0


def factor_cost_proximity(feat: dict[str, float | None]) -> float:
    """成本接近度因子：价格接近平均成本 → 支撑/共振区。

    _cost_proximity = (close - avg_cost) / avg_cost * 100。
    - 接近 0（±3%）: 价格在成本线附近 → 强支撑 +10 ~ +15
    - 正向较大（>15%）: 价格远高于成本 → 获利盘抛压 -15
    - 负向较大（<-20%）: 价格远低于成本 → 深度套牢 +10
    """
    cp = _safe_float(feat, "_cost_proximity")
    if cp is None:
        return 0.0
    # 成本附近：共振支撑
    if abs(cp) <= 3:
        return _clamp(15.0 - abs(cp) / 3 * 5, lo=10)
    if abs(cp) <= 8:
        return _clamp(5.0 - (abs(cp) - 3) / 5 * 5, lo=0)
    # 远高于成本
    if cp > 15:
        return -15.0
    if cp > 8:
        return _clamp(-5.0 - (cp - 8) / 7 * 10, lo=-15)
    # 远低于成本
    if cp < -20:
        return 10.0
    if cp < -8:
        return _clamp(5.0 + (abs(cp) - 8) / 12 * 5, hi=10)
    return 0.0


def factor_chip_concentration_70(feat: dict[str, float | None]) -> float:
    """70% 筹码集中度因子（70% 区间更窄，增强信号）。

    与 90% 集中度形成共识：两者同时低时信号更强。
    """
    c70 = _safe_float(feat, "_concentration_70")
    if c70 is None:
        return 0.0
    if c70 < 0.10:
        return 15.0
    if c70 < 0.20:
        return _clamp(5.0 + (0.20 - c70) / 0.10 * 10)
    if c70 <= 0.35:
        return _clamp(0.0 + (0.35 - c70) / 0.15 * 5)
    return -5.0


# ═══════════════════════════════════════════════════════
# 族 6：分数稳定性因子（Score Stability）
# ═══════════════════════════════════════════════════════

def factor_score_stability(feat: dict[str, float | None]) -> float:
    """分数稳定性因子：过去 10 日规则分越稳定，当前信号越可信。

    从 features 中读取预计算的稳定性指标：
    - _stability_std_10d: 近 10 日分数标准差
    - _stability_slope_5d: 近 5 日分数趋势斜率

    设计理念：
    一只票 5 天分数 75→73→74→76→75（std≈1）比 75→60→80→55→70（std≈10）
    更可信——后者说明算法对这只票缺乏共识，多空分歧大。

    阈值经回测校准（见 walk_forward_backtest --enable-stability-filter）。
    """
    std = _safe_float(feat, "_stability_std_10d")
    slope = _safe_float(feat, "_stability_slope_5d")

    score = 0.0

    # ── 分数波动惩罚/奖励 ──
    if std is not None:
        if std < 3.0:
            score += 10.0   # 极稳定 → 加分
        elif std < 7.0:
            score += 3.0    # 稳定 → 小幅加分
        elif std < 12.0:
            score -= 5.0    # 不稳定 → 扣分
        elif std < 18.0:
            score -= 15.0   # 高度不稳定 → 大幅扣分
        else:
            score -= 25.0   # 极端波动 → 严重扣分（几乎排除）

    # ── 分数趋势惩罚 ──
    if slope is not None:
        if slope < -2.0:
            score -= 8.0    # 分数在快速恶化 → 红灯
        elif slope < -1.0:
            score -= 3.0    # 分数在缓慢恶化
        elif slope > 1.5:
            score += 5.0    # 分数在改善 → 信号增强
        elif slope > 0.5:
            score += 2.0    # 分数轻微改善

    return _clamp(score, -25, 15)


# ═══════════════════════════════════════════════════════
# 因子注册表 & 合成
# ═══════════════════════════════════════════════════════

# 纯技术因子（无需财务数据，可从 features dict 直接计算）
TECH_FACTORS: dict[str, FactorFn] = {
    # 族 1：反转
    "ret_20d_reversal": factor_ret_20d_reversal,
    "drawdown_reversal": factor_drawdown_reversal,
    "rsi_oversold": factor_rsi_oversold,
    "kdj_oversold": factor_kdj_oversold,
    "close_vs_ma20": factor_close_vs_ma20,
    "days_since_high": factor_days_since_high,
    "stabilization": factor_stabilization,  # 企稳确认
    "ret_20d_trend": factor_ret_20d_trend,  # 温和动量
    # 族 2：趋势质量
    "volume_health": factor_volume_health,
    "ma_slope_health": factor_ma_slope_health,
    "low_volatility": factor_low_volatility,
    # 族 3：资金流向 & 概念热度
    "volume_direction": factor_volume_direction,
    "concept_heat": factor_concept_heat,
    "concept_diversity": factor_concept_diversity,
    "turnover_attention": factor_turnover_attention,
    # 族 5：筹码峰
    "chip_profit_ratio": factor_chip_profit_ratio,
    "chip_concentration": factor_chip_concentration,
    "cost_proximity": factor_cost_proximity,
    "chip_concentration_70": factor_chip_concentration_70,
    # 族 7：分数稳定性
    "score_stability": factor_score_stability,
    # 族 6：行业/板块动量（板块轮动核心信号）
    "sector_momentum": factor_sector_momentum,
    "industry_leader": factor_industry_leader,
}

ALL_FACTOR_NAMES = list(TECH_FACTORS.keys()) + [
    "profit_positive",
    "revenue_growth",
    "profit_growth",
    "profit_margin",
    "profit_stability",
]


def compute_score_v2(
    feat: dict[str, float | None],
    *,
    factors: list[str] | None = None,
    weights: dict[str, float] | None = None,
    code: str = "",
    base: float = 50.0,
) -> float:
    """v2 综合评分：base + Σ (factor_i × weight_i)。

    Args:
        feat: features dict（来自 latest_features / enrich_bars）
        factors: 启用的因子名列表，默认全部技术因子
        weights: {factor_name: weight}，默认等权 1.0
        code: 股票代码（质量因子用）
        base: 基础分，默认 50
    """
    names = factors or list(TECH_FACTORS.keys())
    wmap = weights or {n: 1.0 for n in names}
    total = float(base)
    for name in names:
        w = wmap.get(name, 1.0)
        if name in TECH_FACTORS:
            total += TECH_FACTORS[name](feat) * w
        elif name == "profit_positive":
            total += factor_profit_positive(feat, code) * w
        elif name == "revenue_growth":
            total += factor_revenue_growth(feat, code) * w
        elif name == "profit_growth":
            total += factor_profit_growth(feat, code) * w
        elif name == "profit_margin":
            total += factor_profit_margin(feat, code) * w
        elif name == "profit_stability":
            total += factor_profit_stability(feat, code) * w
    return round(max(0.0, min(200.0, total)), 1)


# ── 预定义方案 ────────────────────────────────────────

PRESET_SCHEMES: dict[str, tuple[list[str], dict[str, float]]] = {
    # 纯反转：仅使用族 1 因子
    "reversal_only": (
        ["ret_20d_reversal", "drawdown_reversal", "rsi_oversold",
         "kdj_oversold", "close_vs_ma20", "days_since_high"],
        {"ret_20d_reversal": 1.2, "drawdown_reversal": 1.0,
         "rsi_oversold": 0.8, "kdj_oversold": 0.6,
         "close_vs_ma20": 0.8, "days_since_high": 0.6},
    ),
    # 回调买入：中长期强势 + 短期回调（实证：high_60d r=+0.03, kdj_d r=-0.14）
    "pullback_quality": (
        ["ret_20d_trend", "rsi_oversold", "kdj_oversold",
         "days_since_high", "stabilization",
         "volume_health", "ma_slope_health", "low_volatility"],
        {"ret_20d_trend": 0.8,     # 中长趋势向上（加重）
         "rsi_oversold": 0.8,       # 短期超卖
         "kdj_oversold": 1.5,       # 核心：KDJ 超卖（r=-0.14）
         "days_since_high": 0.5,    # 距高点（回调过）
         "stabilization": 1.0,      # 企稳确认（加重）
         "volume_health": 0.5,
         "ma_slope_health": 0.5,
         "low_volatility": 0.6},
    ),
    # 温和动量 + 趋势质量（实证：v1动量r=+0.077 vs v2反转r=-0.07）
    "trend_quality": (
        ["ret_20d_trend", "stabilization",
         "close_vs_ma20", "days_since_high",
         "volume_health", "ma_slope_health", "low_volatility",
         "volume_direction", "turnover_attention"],
        {"ret_20d_trend": 1.5, "stabilization": 1.0,
         "close_vs_ma20": 0.5, "days_since_high": 0.3,
         "volume_health": 0.5, "ma_slope_health": 0.5, "low_volatility": 0.5,
         "volume_direction": 0.5, "turnover_attention": 0.5},
    ),
    # 反转 + 趋势质量
    "reversal_plus_quality": (
        ["ret_20d_reversal", "drawdown_reversal", "rsi_oversold",
         "close_vs_ma20", "days_since_high", "stabilization",
         "volume_health", "ma_slope_health", "low_volatility"],
        {"ret_20d_reversal": 1.0, "drawdown_reversal": 0.8,
         "rsi_oversold": 0.6, "close_vs_ma20": 0.6,
         "days_since_high": 0.5, "stabilization": 1.2,
         "volume_health": 0.4, "ma_slope_health": 0.3, "low_volatility": 0.3},
    ),
    # 全技术因子（族 1+2）
    "all_technical": (
        list(TECH_FACTORS.keys()),
        {k: 0.7 for k in TECH_FACTORS},
    ),
    # 技术反转 + 财务质量（族 1+2+3）—— 板块内百分位版本
    "tech_plus_financial": (
        list(TECH_FACTORS.keys()) + [
            "profit_positive", "revenue_growth", "profit_growth",
            "profit_margin", "profit_stability",
        ],
        {k: 0.5 for k in TECH_FACTORS}
        | {"profit_positive": 0.8, "revenue_growth": 0.8,
           "profit_growth": 0.8, "profit_margin": 0.6,
           "profit_stability": 0.6},
    ),
    # 纯财务质量因子（板块内百分位版本）
    "financial_only": (
        ["profit_positive", "revenue_growth", "profit_growth",
         "profit_margin", "profit_stability"],
        {"profit_positive": 1.0, "revenue_growth": 1.0,
         "profit_growth": 1.0, "profit_margin": 0.8,
         "profit_stability": 0.8},
    ),
    # 技术 + 财务（等权混合）
    "tech_fin_equal": (
        list(TECH_FACTORS.keys()) + [
            "profit_positive", "revenue_growth", "profit_growth",
        ],
        {k: 0.5 for k in TECH_FACTORS}
        | {"profit_positive": 0.5, "revenue_growth": 0.5,
           "profit_growth": 0.5},
    ),
    # ── 新方案：资金流向 + 概念热度 ──
    # 纯资金+概念（不包含反转，测试新因子独立效果）
    "flow_concept_only": (
        ["volume_direction", "concept_heat", "concept_diversity",
         "turnover_attention", "volume_health"],
        {"volume_direction": 1.5, "concept_heat": 1.5,
         "concept_diversity": 0.5, "turnover_attention": 0.8,
         "volume_health": 0.8},
    ),
    # 反转 + 资金流向 + 概念热度（最可能有效的组合）
    "reversal_flow_concept": (
        ["ret_20d_reversal", "drawdown_reversal", "rsi_oversold",
         "close_vs_ma20", "days_since_high",
         "volume_direction", "concept_heat", "concept_diversity",
         "turnover_attention", "volume_health", "low_volatility",
         "sector_momentum", "industry_leader"],
        {"ret_20d_reversal": 1.0, "drawdown_reversal": 0.8,
         "rsi_oversold": 0.5, "close_vs_ma20": 0.5,
         "days_since_high": 0.5,
         "volume_direction": 1.2, "concept_heat": 1.0,
         "concept_diversity": 0.4, "turnover_attention": 0.6,
         "volume_health": 0.6, "low_volatility": 0.3,
         "sector_momentum": 1.8, "industry_leader": 0.8},
    ),
    # ── 板块轮动感知（2026-06-12 新增）──
    # 反转 + 板块动量 + 行业龙头 — 核心思想：压对板块有时比压对个股更重要
    "sector_aware": (
        ["ret_20d_reversal", "drawdown_reversal", "rsi_oversold",
         "close_vs_ma20", "days_since_high", "stabilization",
         "volume_direction", "volume_health", "low_volatility",
         "sector_momentum", "industry_leader",
         "concept_heat", "concept_diversity", "turnover_attention"],
        {"ret_20d_reversal": 1.0, "drawdown_reversal": 0.8,
         "rsi_oversold": 0.5, "close_vs_ma20": 0.5,
         "days_since_high": 0.5, "stabilization": 1.0,
         "volume_direction": 0.8, "volume_health": 0.4,
         "low_volatility": 0.3,
         "sector_momentum": 2.0, "industry_leader": 1.0,
         "concept_heat": 0.8, "concept_diversity": 0.3,
         "turnover_attention": 0.5},
    ),
    # ── 筹码峰方案 (NEW) ──
    # 纯反转 + 筹码峰（测试筹码因子的独立增量效果）
    "chip_reversal": (
        ["ret_20d_reversal", "drawdown_reversal", "rsi_oversold",
         "close_vs_ma20", "days_since_high",
         "chip_profit_ratio", "chip_concentration", "cost_proximity",
         "chip_concentration_70"],
        {"ret_20d_reversal": 1.0, "drawdown_reversal": 0.8,
         "rsi_oversold": 0.5, "close_vs_ma20": 0.5,
         "days_since_high": 0.5,
         "chip_profit_ratio": 1.2, "chip_concentration": 1.0,
         "cost_proximity": 1.0, "chip_concentration_70": 0.5},
    ),
    # 全部技术因子 + 筹码峰
    "full_tech_plus_chip": (
        list(TECH_FACTORS.keys()),
        {k: 0.35 for k in TECH_FACTORS},
    ),
}
