"""A+H 跨市场联动分析 — 溢价、比价、领先滞后。

核心能力：
1. A+H 股配对 — 从 AkShare AH 名称列表 + 库内名称匹配
2. AH 溢价率计算 — 基于库内日线收盘价
3. 溢价中枢偏离信号 — 溢价率偏离 20 日均值超过阈值
4. 领先滞后分析 — A 股 vs H 股日收益率交叉相关
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.market import DEFAULT_ADJUST_TYPE, get_bars, latest_trade_date
from zplan_shared.models import AhCrossRef, DailyPrice, SessionLocal, StockList, init_db

logger = logging.getLogger(__name__)


# ── A+H 配对 ──────────────────────────────────────────────────────


def _normalize_name(n: str) -> str:
    """去掉括号内容、空格、英文，供名称匹配。"""
    # 去全角/半角括号内容、英文字母、空格、横线
    s = re.sub(r'[（(][^)）]*[)）]', '', str(n))
    s = re.sub(r'[A-Za-zＡ-Ｚａ-ｚ]', '', s)
    s = s.replace(' ', '').replace('-', '').replace('·', '')
    return s.strip()


def build_ah_pairs(force_refresh: bool = False) -> int:
    """从 AkShare AH 列表 + 库内名称匹配，写入 ``ah_cross_ref``。

    返回新写入的配对数量。
    """
    init_db()

    # 已有配对
    if not force_refresh:
        with SessionLocal() as session:
            existing = session.execute(
                select(func.count(AhCrossRef.id))
            ).scalar()
        if existing and existing >= 100:
            logger.info("[INFO] AH 配对已有 %s 对，跳过（force_refresh=True 可强制刷新）", existing)
            return 0

    # 获取 AkShare AH 名称列表（港股侧）
    try:
        import akshare as ak
        ah_df = ak.stock_zh_ah_name()
    except Exception as exc:
        logger.warning("[WARN] AkShare AH 名称列表获取失败: %s，使用库内名称匹配", exc)
        return _build_ah_pairs_from_db()

    return _build_ah_pairs_from_akshare(ah_df)


def _build_ah_pairs_from_akshare(ah_df: pd.DataFrame) -> int:
    """从 AkShare AH 列表 + DB 名称反查 A 股代码。"""
    with SessionLocal() as session:
        a_rows = session.execute(
            select(StockList.ts_code, StockList.name).where(StockList.market == "a")
        ).all()
        hk_rows = session.execute(
            select(StockList.ts_code, StockList.name).where(StockList.market == "hk")
        ).all()

    a_map = {_normalize_name(r[1]): r[0] for r in a_rows}
    hk_map = {_normalize_name(r[1]): r[0] for r in hk_rows}
    a_name_map = {_normalize_name(r[1]): r[1] for r in a_rows}
    hk_name_map = {_normalize_name(r[1]): r[1] for r in hk_rows}

    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _, row in ah_df.iterrows():
        hk_code = str(row["代码"]).zfill(5)
        n = _normalize_name(row["名称"])
        a_code = a_map.get(n)
        if not a_code:
            continue
        hk_code_db = hk_map.get(n, hk_code)
        key = (a_code, hk_code_db)
        if key in seen:
            continue
        seen.add(key)
        pairs.append({
            "a_code": a_code,
            "a_name": a_name_map.get(n, row["名称"]),
            "hk_code": hk_code_db,
            "hk_name": hk_name_map.get(n, row["名称"]),
        })

    return _upsert_ah_pairs(pairs)


def _build_ah_pairs_from_db() -> int:
    """纯库内名称匹配（不依赖 AkShare）。"""
    with SessionLocal() as session:
        a_rows = session.execute(
            select(StockList.ts_code, StockList.name).where(StockList.market == "a")
        ).all()
        hk_rows = session.execute(
            select(StockList.ts_code, StockList.name).where(StockList.market == "hk")
        ).all()

    a_map = {_normalize_name(r[1]): (r[0], r[1]) for r in a_rows}
    hk_map = {_normalize_name(r[1]): (r[0], r[1]) for r in hk_rows}

    common = set(a_map.keys()) & set(hk_map.keys())
    pairs = []
    for n in common:
        a_code, a_name = a_map[n]
        hk_code, hk_name = hk_map[n]
        pairs.append({
            "a_code": a_code, "a_name": a_name,
            "hk_code": hk_code, "hk_name": hk_name,
        })

    return _upsert_ah_pairs(pairs)


def _upsert_ah_pairs(pairs: list[dict]) -> int:
    if not pairs:
        return 0
    with SessionLocal() as session:
        for p in pairs:
            stmt = insert(AhCrossRef).values(**p)
            stmt = stmt.on_conflict_do_update(
                index_elements=["a_code", "hk_code"],
                set_={"a_name": stmt.excluded.a_name, "hk_name": stmt.excluded.hk_name},
            )
            session.execute(stmt)
        session.commit()
    logger.info("[INFO] AH 配对写入 %s 对", len(pairs))
    return len(pairs)


# ── AH 溢价率 ──────────────────────────────────────────────────────


def compute_ah_premium(as_of: date | None = None) -> pd.DataFrame:
    """计算所有 A+H 对的当日溢价率。

    AH 溢价率 (%) = (H股收盘价 / A股收盘价 - 1) × 100
    正 = H 股相对溢价，负 = A 股相对溢价（常见）。
    """
    init_db()
    trade_date = as_of or latest_trade_date(market="a")
    if trade_date is None:
        return pd.DataFrame()

    with SessionLocal() as session:
        pairs = session.execute(
            select(AhCrossRef.a_code, AhCrossRef.hk_code, AhCrossRef.a_name, AhCrossRef.hk_name)
        ).all()

    rows = []
    for a_code, hk_code, a_name, hk_name in pairs:
        a_close = _close_on(a_code, trade_date, market="a")
        hk_close = _close_on(hk_code, trade_date, market="hk")
        if a_close and hk_close:
            premium = round((hk_close / a_close - 1) * 100, 2)
            rows.append({
                "a_code": a_code, "a_name": a_name,
                "hk_code": hk_code, "hk_name": hk_name,
                "a_close": a_close, "hk_close": hk_close,
                "ah_premium_pct": premium,
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("ah_premium_pct", ascending=True)
    return df


def _close_on(ts_code: str, trade_date: date, market: str) -> float | None:
    """取某日收盘价。"""
    with SessionLocal() as session:
        row = session.execute(
            select(DailyPrice.close).where(
                DailyPrice.ts_code == ts_code,
                DailyPrice.trade_date == trade_date,
                DailyPrice.market == market,
            )
        ).scalar_one_or_none()
    return float(row) if row else None


def update_ah_premium_snapshot(as_of: date | None = None) -> int:
    """计算并回写 AH 溢价到 ``ah_cross_ref``。"""
    df = compute_ah_premium(as_of)
    if df.empty:
        return 0

    trade_date = as_of or latest_trade_date(market="a")
    with SessionLocal() as session:
        n = 0
        for _, row in df.iterrows():
            session.execute(
                AhCrossRef.__table__.update()
                .where(AhCrossRef.a_code == row["a_code"])
                .where(AhCrossRef.hk_code == row["hk_code"])
                .values(
                    ah_premium_pct=row["ah_premium_pct"],
                    a_close=row["a_close"],
                    hk_close=row["hk_close"],
                    premium_as_of=trade_date,
                )
            )
            n += 1
        session.commit()
    logger.info("[INFO] AH 溢价更新 %s 对 @ %s", n, trade_date)
    return n


# ── 溢价信号 ────────────────────────────────────────────────────────


def premium_dislocation_signals(
    *,
    z_threshold: float = 1.5,
) -> pd.DataFrame:
    """溢价偏离信号：当前溢价率偏离历史均值超过 N 倍标准差。

    返回 DataFrame，含当前溢价、均值、标准差、z-score 和信号方向。
    """
    df = compute_ah_premium()
    if df.empty:
        return df

    signals = []
    for _, row in df.iterrows():
        hist = _premium_history(row["a_code"], row["hk_code"], lookback=60)
        if len(hist) < 20:
            continue
        mean = np.mean(hist)
        std = np.std(hist) or 0.01
        z = (row["ah_premium_pct"] - mean) / std
        if abs(z) >= z_threshold:
            direction = "H股相对高估" if z > 0 else "A股相对高估"
            signals.append({
                **row.to_dict(),
                "premium_mean_60d": round(float(mean), 2),
                "premium_std_60d": round(float(std), 2),
                "z_score": round(float(z), 2),
                "signal": f"{direction}（z={z:.1f}）",
            })

    return pd.DataFrame(signals).sort_values("z_score", key=abs, ascending=False)


def _premium_history(a_code: str, hk_code: str, lookback: int = 60) -> list[float]:
    """计算历史溢价率序列。"""
    end = latest_trade_date(market="a")
    if end is None:
        return []
    start = end - timedelta(days=lookback * 2)

    a_bars = get_bars(a_code, start=start, end=end, market="a")
    hk_bars = get_bars(hk_code, start=start, end=end, market="hk")
    if a_bars.empty or hk_bars.empty:
        return []

    # 对齐交易日
    common = a_bars.index.intersection(hk_bars.index)
    if len(common) < 10:
        return []

    a_close = a_bars.loc[common, "close"]
    hk_close = hk_bars.loc[common, "close"]
    premium = (hk_close / a_close - 1) * 100
    return premium.tail(lookback).tolist()


# ── 领先滞后分析 ────────────────────────────────────────────────────


def cross_correlation(
    a_code: str,
    hk_code: str,
    *,
    lookback: int = 90,
    max_lag: int = 5,
) -> dict[str, Any]:
    """A 股与 H 股日收益率的交叉相关，检测领先滞后关系。

    返回最大相关滞后天数及该滞后下的相关系数。
    """
    end = latest_trade_date(market="a")
    if end is None:
        return {}
    start = end - timedelta(days=lookback * 2)

    a_bars = get_bars(a_code, start=start, end=end, market="a")
    hk_bars = get_bars(hk_code, start=start, end=end, market="hk")

    if a_bars.empty or hk_bars.empty:
        return {}

    common = a_bars.index.intersection(hk_bars.index)
    if len(common) < 30:
        return {}

    a_ret = a_bars.loc[common, "close"].pct_change().dropna()
    hk_ret = hk_bars.loc[common, "close"].pct_change().dropna()
    common = a_ret.index.intersection(hk_ret.index)
    a_ret = a_ret.loc[common].tail(lookback)
    hk_ret = hk_ret.loc[common].tail(lookback)

    if len(a_ret) < 30:
        return {}

    # lag > 0: A 股领先 H 股；lag < 0: H 股领先 A 股
    correlations = {}
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            corr = a_ret.iloc[:-lag].corr(hk_ret.iloc[lag:])
        elif lag < 0:
            corr = a_ret.iloc[-lag:].corr(hk_ret.iloc[:lag])
        else:
            corr = a_ret.corr(hk_ret)
        correlations[lag] = round(float(corr) if not np.isnan(corr) else 0, 4)

    best_lag = max(correlations, key=lambda k: abs(correlations[k]))
    best_corr = correlations[best_lag]
    if best_lag > 0:
        lead = f"A 股领先 H 股 {best_lag} 天"
    elif best_lag < 0:
        lead = f"H 股领先 A 股 {abs(best_lag)} 天"
    else:
        lead = "同步"

    return {
        "a_code": a_code,
        "hk_code": hk_code,
        "lookback_days": lookback,
        "best_lag": best_lag,
        "best_corr": best_corr,
        "lead_relation": lead,
        "corr_by_lag": correlations,
        "sample_days": len(a_ret),
    }


def cross_market_lead_lag_scan(
    *,
    min_corr: float = 0.3,
    max_lag: int = 5,
) -> pd.DataFrame:
    """全量 A+H 对领先滞后扫描。"""
    with SessionLocal() as session:
        pairs = session.execute(
            select(AhCrossRef.a_code, AhCrossRef.hk_code, AhCrossRef.a_name)
        ).all()

    results = []
    for a_code, hk_code, a_name in pairs:
        try:
            r = cross_correlation(a_code, hk_code, max_lag=max_lag)
            if r and abs(r.get("best_corr", 0)) >= min_corr:
                results.append(r)
        except Exception:
            pass

    return pd.DataFrame(results).sort_values("best_corr", key=abs, ascending=False)


# ── 一键同步 ──────────────────────────────────────────────────────


def sync_ah_all(as_of: date | None = None) -> dict:
    """一键同步：配对 + 溢价 + 信号 → 返回汇总字典。"""
    n_pairs = build_ah_pairs()
    n_premium = update_ah_premium_snapshot(as_of)
    dislocation = premium_dislocation_signals()
    return {
        "pairs": n_pairs,
        "premium_updated": n_premium,
        "dislocation_signals": len(dislocation),
        "top_dislocation": dislocation.head(5)[["a_code", "hk_code", "ah_premium_pct", "z_score", "signal"]].to_dict("records") if not dislocation.empty else [],
    }
