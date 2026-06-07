"""相似历史形态搜索：在当前股票的走势明确（偏多/偏空）时，
从历史数据中寻找技术特征最相似的 Top-N 匹配，展示其后市表现，
增强预测的说服力。

思路：
1. 从目标股票的 K 线计算 60 日特征向量（标准化）
2. 在 daily_prices 中搜索历史上特征相似的片段
3. 对每个匹配计算 20 日 forward return
4. 汇总统计：胜率、平均收益、偏多/偏空判断
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from zplan_shared.features import enrich_bars, latest_features
from zplan_shared.market import get_bars, resolve_ts_code
from zplan_shared.models import SessionLocal, init_db

logger = logging.getLogger(__name__)

# ── 相似度特征向量（与 chart_viz 的标注维度对齐）──────────────────
FEATURE_KEYS = [
    # 趋势位置（相对 MA20 / 60日高点 / 20日回撤）
    "close_vs_ma20",
    "ma20_slope_5d",
    "high_60d_pct",
    "drawdown_20d_pct",
    # 动量
    "ret_5d",
    "ret_20d",
    "ret_60d",
    # 震荡
    "rsi14",
    "kdj_k",
    "kdj_d",
    # 量价
    "vol_ratio20",
    "atr_pct",
    # MACD（标准化到价格百分比）
    "macd_dif_pct",
    "macd_hist_pct",
]


def _feature_vector(features: dict[str, float | None], close: float | None) -> np.ndarray:
    """从特征快照中提取归一化向量。缺失值填 0（中性）。"""
    vec = np.zeros(len(FEATURE_KEYS), dtype=np.float64)
    if close is None or close <= 0:
        return vec
    for i, key in enumerate(FEATURE_KEYS):
        v = features.get(key)
        if v is None or not np.isfinite(v):
            continue
        if key == "macd_dif_pct":
            raw = features.get("macd_dif")
            if raw is not None and np.isfinite(raw):
                v = raw / close * 100
            else:
                continue
        elif key == "macd_hist_pct":
            raw = features.get("macd_hist")
            if raw is not None and np.isfinite(raw):
                v = raw / close * 100
            else:
                continue
        vec[i] = float(v)
    return vec


# ── 归一化用的统计量（基于全市场样本估算的合理范围）─────────────
# 用于 MinMax 缩放到 [0,1]
_FEATURE_RANGES: dict[str, tuple[float, float]] = {
    "close_vs_ma20": (-20, 20),
    "ma20_slope_5d": (-10, 10),
    "high_60d_pct": (50, 100),
    "drawdown_20d_pct": (-30, 5),
    "ret_5d": (-15, 15),
    "ret_20d": (-40, 40),
    "ret_60d": (-60, 60),
    "rsi14": (20, 80),
    "kdj_k": (0, 100),
    "kdj_d": (0, 100),
    "vol_ratio20": (0.3, 3.0),
    "atr_pct": (0.5, 8.0),
    "macd_dif_pct": (-10, 10),
    "macd_hist_pct": (-5, 5),
}


def _normalize(vec: np.ndarray) -> np.ndarray:
    """MinMax 归一化到 [0,1]，超界截断。"""
    out = vec.copy()
    for i, key in enumerate(FEATURE_KEYS):
        lo, hi = _FEATURE_RANGES.get(key, (-10, 10))
        v = np.clip(out[i], lo, hi)
        out[i] = (v - lo) / (hi - lo) if hi > lo else 0.5
    return out


def _euclidean_distance(a: np.ndarray, b: np.ndarray) -> float:
    """归一化向量的欧氏距离 → 转为相似度 0-1（1=完全相同）。"""
    d = float(np.sqrt(np.sum((a - b) ** 2)))
    max_d = np.sqrt(len(FEATURE_KEYS))  # 全 0 vs 全 1
    return 1.0 - d / max_d


# ── 主入口 ──────────────────────────────────────────────────────


def find_similar_patterns(
    ts_code: str,
    *,
    as_of: str | date | None = None,
    top_k: int = 5,
    lookback_years: int = 2,
    market: str = "a",
    min_similarity: float = 0.55,
) -> dict[str, Any]:
    """搜索与目标股票当前技术形态最相似的历史片段。

    返回::

        {
            "matches": [
                {
                    "ts_code": "000002.SZ",
                    "name": "万科A",
                    "match_date": "2025-03-15",
                    "similarity": 0.87,
                    "forward_return_20d": 12.5,
                    "forward_max_gain": 15.2,
                    "forward_max_loss": -3.1,
                },
                ...
            ],
            "summary": {
                "total": 5,
                "win_count": 3,
                "win_rate": 0.6,
                "avg_return_20d": 8.2,
                "best_return": 15.2,
                "worst_return": -5.0,
                "verdict": "偏多",
            },
            "target": {
                "ts_code": "000001.SZ",
                "name": "平安银行",
                "as_of": "2026-06-05",
                "feature_vector": [...],
            },
        }
    """
    init_db()
    code = resolve_ts_code(ts_code)

    # 1. 获取目标股票 K 线并计算特征
    bars = get_bars(code, market=market)
    if bars.empty or len(bars) < 60:
        return {"matches": [], "summary": _empty_summary(), "target": _target_info(code, None)}

    enriched = enrich_bars(bars)
    target_date = _resolve_as_of(as_of, enriched)
    if target_date is None:
        return {"matches": [], "summary": _empty_summary(), "target": _target_info(code, None)}

    # 裁到 target_date（index 类型为 datetime.date）
    target_dt = date.fromisoformat(target_date)
    target_bars = enriched.loc[:target_dt]
    if len(target_bars) < 60:
        return {"matches": [], "summary": _empty_summary(), "target": _target_info(code, None)}

    target_features = latest_features(target_bars)
    target_close = float(target_bars["close"].iloc[-1])
    target_vec = _normalize(_feature_vector(target_features, target_close))

    # 2. 从 daily_prices 搜索候选（SQL 粗筛）
    candidates = _search_candidates_sql(
        code=code,
        target_date=target_date,
        target_features=target_features,
        target_close=target_close,
        lookback_years=lookback_years,
        market=market,
    )

    if not candidates:
        return {
            "matches": [],
            "summary": _empty_summary(),
            "target": _target_info(code, target_date),
        }

    # 3. 对候选计算特征向量 + 相似度 → Top-K
    matches = _rank_and_filter(
        candidates=candidates,
        target_vec=target_vec,
        target_date=target_date,
        top_k=top_k,
        min_similarity=min_similarity,
        market=market,
    )

    # 4. 计算 forward returns
    matches = _compute_forward_returns(matches, market=market)

    # 5. 汇总
    summary = _summarize(matches)

    return {
        "matches": matches,
        "summary": summary,
        "target": _target_info(code, target_date),
    }


# ── 内部实现 ────────────────────────────────────────────────────


def _resolve_as_of(as_of: str | date | None, enriched: pd.DataFrame) -> str | None:
    """确定截止日期。"""
    if as_of is not None:
        d = str(as_of)[:10]
        if d in enriched.index:
            return d
    # 用最后一天
    last = str(enriched.index[-1])[:10]
    return last


def _target_info(code: str, target_date: str | None) -> dict[str, Any]:
    from zplan_shared.models import StockList

    name = None
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT name FROM stock_list WHERE ts_code = :c"),
            {"c": code},
        ).scalar()
        name = row
    return {"ts_code": code, "name": name, "as_of": target_date}


def _empty_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "win_count": 0,
        "win_rate": 0.0,
        "avg_return_20d": 0.0,
        "best_return": 0.0,
        "worst_return": 0.0,
        "verdict": "数据不足",
    }


def _search_candidates_sql(
    *,
    code: str,
    target_date: str,
    target_features: dict[str, float | None],
    target_close: float,
    lookback_years: int,
    market: str,
) -> list[dict[str, Any]]:
    """SQL 层粗筛：从 daily_prices 找历史上具有相近价格形态的候选。

    策略：
    1. 用 daily_prices 计算每只股票在每个交易日的 ret_20d（收盘价 20 日涨跌幅）
    2. 筛选 ret_20d 同号且幅度相近（±10%）的候选
    3. 排除目标日期 30 天内（确保有 forward return 数据）
    4. 限制候选数量 → 后期精细计算
    """
    ret_20d = target_features.get("ret_20d") or 0
    target_dt = date.fromisoformat(target_date)
    start_date = (target_dt - timedelta(days=lookback_years * 365)).isoformat()
    # 排除最近 30 天，确保候选有后市数据可查
    exclude_after = (target_dt - timedelta(days=30)).isoformat()

    candidates: list[dict[str, Any]] = []
    with SessionLocal() as s:
        # 从 daily_prices 用窗口函数计算 ret_20d
        # 按 ts_code 分区，按 trade_date 排序，计算 20 日涨幅
        rows = s.execute(
            text(
                """WITH ranked AS (
                    SELECT
                        ts_code,
                        trade_date,
                        close,
                        LAG(close, 20) OVER (
                            PARTITION BY ts_code ORDER BY trade_date
                        ) AS close_20d_ago
                    FROM daily_prices
                    WHERE market = :market
                      AND ts_code != :code
                      AND trade_date >= :start_date
                      AND trade_date <= :exclude_after
                )
                SELECT
                    ts_code,
                    trade_date,
                    (close / close_20d_ago - 1) * 100 AS ret_20d_calc
                FROM ranked
                WHERE close_20d_ago IS NOT NULL
                  -- 同号 + 幅度在 ±12% 范围内
                  AND ret_20d_calc * :ret_sign >= 0
                  AND ABS(ret_20d_calc - :ret_20d) <= 12
                ORDER BY ABS(ret_20d_calc - :ret_20d) ASC
                LIMIT 2000"""
            ),
            {
                "market": market,
                "code": code,
                "start_date": start_date,
                "exclude_after": exclude_after,
                "ret_20d": ret_20d,
                "ret_sign": 1 if ret_20d >= 0 else -1,
            },
        ).fetchall()

    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r[0], str(r[1]))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"ts_code": r[0], "trade_date": str(r[1])})

    logger.info(
        "相似形态粗筛：%d 候选（目标 %s, ret_20d=%+.1f%%, 回溯至 %s）",
        len(candidates),
        code,
        ret_20d,
        start_date,
    )
    return candidates


def _rank_and_filter(
    *,
    candidates: list[dict[str, Any]],
    target_vec: np.ndarray,
    target_date: str,
    top_k: int,
    min_similarity: float,
    market: str,
) -> list[dict[str, Any]]:
    """对候选计算完整特征向量，按相似度排序取 Top-K。"""
    import heapq

    heap: list[tuple[float, int, dict[str, Any]]] = []  # (neg_sim, seq, match)
    seq = 0

    # 批量获取 K 线以减少 DB 查询
    unique_codes = list(dict.fromkeys(c["ts_code"] for c in candidates))

    for c_data in candidates[:500]:  # 限制精细计算数量
        ts = c_data["ts_code"]
        td = c_data["trade_date"]
        try:
            bars = get_bars(ts, end=td, market=market)
            if bars.empty or len(bars) < 60:
                continue
            enriched = enrich_bars(bars)
            enriched_sorted = enriched.sort_index()
            # 确保不超出 match_date
            td_dt = date.fromisoformat(td)
            mask = enriched_sorted.index <= td_dt
            segment = enriched_sorted.loc[mask]
            if len(segment) < 60:
                continue
            features = latest_features(segment)
            close = float(segment["close"].iloc[-1])
            vec = _normalize(_feature_vector(features, close))
            sim = _euclidean_distance(target_vec, vec)
            if sim < min_similarity:
                continue

            match = {
                "ts_code": ts,
                "match_date": td,
                "similarity": round(sim, 4),
                "forward_return_20d": 0.0,
                "forward_max_gain": 0.0,
                "forward_max_loss": 0.0,
            }
            heapq.heappush(heap, (-sim, seq, match))
            seq += 1
        except Exception:
            continue

    matches = []
    while heap and len(matches) < top_k:
        neg_sim, _, m = heapq.heappop(heap)
        m["similarity"] = round(-neg_sim, 4)
        matches.append(m)

    # 按匹配日期降序
    matches.sort(key=lambda x: x["match_date"], reverse=True)
    return matches


def _compute_forward_returns(
    matches: list[dict[str, Any]],
    *,
    market: str = "a",
    horizon_days: int = 20,
) -> list[dict[str, Any]]:
    """计算每个匹配的后市表现。"""
    for m in matches:
        try:
            match_date_str = m["match_date"]
            match_dt = date.fromisoformat(match_date_str)

            # 获取 match_date 之后的数据
            bars = get_bars(m["ts_code"], start=match_date_str, market=market)
            if bars.empty or len(bars) < 2:
                continue

            # trade_date 是 index，找到 match_date 的位置
            bars_sorted = bars.sort_index()
            # 找到大于等于 match_date 的第一个位置
            idx_arr = bars_sorted.index
            pos_list = [i for i, d in enumerate(idx_arr) if d >= match_dt]
            if not pos_list:
                continue
            pos = pos_list[0]
            match_close = float(bars_sorted["close"].iloc[pos])

            # 取之后的 horizon_days 根 K 线
            future = bars_sorted.iloc[pos + 1 : pos + 1 + horizon_days]
            if future.empty:
                continue

            forward_close = float(future["close"].iloc[-1])
            forward_high = float(future["high"].max())
            forward_low = float(future["low"].min())

            m["forward_return_20d"] = round((forward_close / match_close - 1) * 100, 2)
            m["forward_max_gain"] = round((forward_high / match_close - 1) * 100, 2)
            m["forward_max_loss"] = round((forward_low / match_close - 1) * 100, 2)
        except Exception:
            continue

    # 补充股票名称
    if matches:
        codes = [m["ts_code"] for m in matches]
        with SessionLocal() as s:
            placeholders = ",".join(f":c{i}" for i in range(len(codes)))
            params = {f"c{i}": c for i, c in enumerate(codes)}
            rows = s.execute(
                text(
                    f"SELECT ts_code, name FROM stock_list WHERE ts_code IN ({placeholders})"
                ),
                params,
            ).fetchall()
        name_map = {r[0]: r[1] for r in rows}
        for m in matches:
            m["name"] = name_map.get(m["ts_code"])

    return matches


def _summarize(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总统计。"""
    if not matches:
        return _empty_summary()

    returns = [m.get("forward_return_20d", 0) or 0 for m in matches]
    win_count = sum(1 for r in returns if r > 0)
    total = len(returns)

    avg_ret = float(np.mean(returns)) if returns else 0.0
    best = float(np.max(returns)) if returns else 0.0
    worst = float(np.min(returns)) if returns else 0.0

    # 判断偏多/偏空
    if win_count / total >= 0.6 and avg_ret > 3:
        verdict = "偏多"
    elif win_count / total <= 0.4 and avg_ret < -3:
        verdict = "偏空"
    else:
        verdict = "中性"

    return {
        "total": total,
        "win_count": win_count,
        "win_rate": round(win_count / total, 2) if total else 0,
        "avg_return_20d": round(avg_ret, 2),
        "best_return": round(best, 2),
        "worst_return": round(worst, 2),
        "verdict": verdict,
    }
