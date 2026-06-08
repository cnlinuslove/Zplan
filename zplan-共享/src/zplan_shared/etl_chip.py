"""Phase CYQ：筹码峰日频数据 ETL（纯 Python 计算，无网络依赖）。

算法源自东方财富 CYQCalculator（移动成本分布），用库内 ``daily_prices``
的日线 OHLCV + 换手率直接计算，不依赖 ``ak.stock_cyq_em()`` 的 push2his API。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.config import CYQ_MAX_STALE_DAYS, CYQ_RATE_LIMIT, CYQ_WORKERS
from zplan_shared.market import latest_trade_date
from zplan_shared.models import DailyChip, DailyPrice, SessionLocal, StockList, init_db

logger = logging.getLogger(__name__)

SOURCE = "zplan_cyq_local"
_UPSERT_BATCH = 100
_MAX_RETRIES = 3

# ── CYQ 算法参数 ──
_CYQ_FACTOR = 150       # 价格分桶数（东财默认）
_CYQ_RANGE = 120         # 回溯 K 线根数
_CYQ_OUTPUT_DAYS = 90    # 输出最近 N 天


# ═══════════════════════════════════════════════════════
# 纯 Python CYQ 计算
# ═══════════════════════════════════════════════════════

def _compute_cyq_for_stock(
    bars: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对单只股票的 K 线序列计算逐日筹码分布。

    Args:
        bars: 按 trade_date 升序排列的日线记录列表，
              每条需包含 open, close, high, low, turnover_rate。

    Returns:
        逐日筹码指标列表（最近 ``_CYQ_OUTPUT_DAYS`` 天）。
    """
    if len(bars) < 5:
        return []

    results: list[dict[str, Any]] = []
    factor = _CYQ_FACTOR
    lookback = _CYQ_RANGE

    for idx in range(len(bars)):
        # 取 idx 及之前的 K 线（最多 lookback 根）
        start = max(0, idx - lookback + 1)
        window = bars[start : idx + 1]
        if len(window) < 2:
            continue

        # 确定价格区间
        highs = [b["high"] for b in window]
        lows = [b["low"] for b in window]
        max_price = max(highs)
        min_price = min(lows)
        if max_price <= min_price:
            continue

        accuracy = max(0.01, (max_price - min_price) / (factor - 1))

        # 筹码分布数组（150 个价格桶）
        xdata = np.zeros(factor, dtype=np.float64)

        for bar in window:
            o = bar["open"]
            c = bar["close"]
            h = bar["high"]
            l = bar["low"]
            tr = bar.get("turnover_rate", 0) or 0
            turnover_rate = min(1.0, tr / 100.0)

            avg = (o + c + h + l) / 4.0

            H_idx = int((h - min_price) / accuracy)
            L_idx = int(np.ceil((l - min_price) / accuracy))

            # 一字板时 G 点特殊处理
            if abs(h - l) < 1e-8:
                g_point = (float(factor - 1), int((avg - min_price) / accuracy))
            else:
                g_point = (2.0 / (h - l), int((avg - min_price) / accuracy))

            # 衰减：每天已有筹码按换手率流失
            xdata *= (1.0 - turnover_rate)

            if abs(h - l) < 1e-8:
                # 一字板：矩形分布
                gi = max(0, min(factor - 1, g_point[1]))
                xdata[gi] += g_point[0] * turnover_rate / 2.0
            else:
                # 三角分布
                for j in range(max(0, L_idx), min(factor, H_idx + 1)):
                    cur_price = min_price + accuracy * j
                    if cur_price <= avg:
                        if abs(avg - l) < 1e-8:
                            xdata[j] += g_point[0] * turnover_rate
                        else:
                            xdata[j] += (cur_price - l) / (avg - l) * g_point[0] * turnover_rate
                    else:
                        if abs(h - avg) < 1e-8:
                            xdata[j] += g_point[0] * turnover_rate
                        else:
                            xdata[j] += (h - cur_price) / (h - avg) * g_point[0] * turnover_rate

        total_chips = xdata.sum()
        if total_chips <= 0:
            continue

        current_price = bars[idx]["close"]

        # 获利比例
        below = 0.0
        for i in range(factor):
            if current_price >= min_price + i * accuracy:
                below += xdata[i]
        profit_ratio = (below / total_chips) * 100.0 if total_chips > 0 else 0.0

        # 平均成本（50% 筹码位置）
        avg_cost = _cost_at_chip(xdata, min_price, accuracy, factor, total_chips * 0.5)

        # 90% / 70% 集中度
        pct_90 = _percent_chips(xdata, min_price, accuracy, factor, total_chips, 0.9)
        pct_70 = _percent_chips(xdata, min_price, accuracy, factor, total_chips, 0.7)

        results.append(
            {
                "trade_date": bars[idx]["trade_date"],
                "profit_ratio": round(profit_ratio, 2),
                "avg_cost": round(avg_cost, 4) if avg_cost is not None else None,
                "cost_90_low": round(pct_90[0], 4) if pct_90[0] is not None else None,
                "cost_90_high": round(pct_90[1], 4) if pct_90[1] is not None else None,
                "concentration_90": round(pct_90[2], 6) if pct_90[2] is not None else None,
                "cost_70_low": round(pct_70[0], 4) if pct_70[0] is not None else None,
                "cost_70_high": round(pct_70[1], 4) if pct_70[1] is not None else None,
                "concentration_70": round(pct_70[2], 6) if pct_70[2] is not None else None,
            }
        )

    # 只返回最近 N 天
    return results[-_CYQ_OUTPUT_DAYS:] if len(results) > _CYQ_OUTPUT_DAYS else results


def _cost_at_chip(
    xdata: np.ndarray, min_price: float, accuracy: float, factor: int, target: float
) -> float | None:
    """获取堆叠筹码达到 ``target`` 时的成本价格。"""
    total = 0.0
    for i in range(factor):
        total += xdata[i]
        if total >= target:
            return min_price + i * accuracy
    return None


def _percent_chips(
    xdata: np.ndarray,
    min_price: float,
    accuracy: float,
    factor: int,
    total: float,
    percent: float,
) -> tuple[float | None, float | None, float | None]:
    """计算指定百分比的筹码区间和集中度。"""
    ps_low = (1 - percent) / 2
    ps_high = (1 + percent) / 2
    low_price = _cost_at_chip(xdata, min_price, accuracy, factor, total * ps_low)
    high_price = _cost_at_chip(xdata, min_price, accuracy, factor, total * ps_high)
    if low_price is not None and high_price is not None and (low_price + high_price) > 0:
        concentration = (high_price - low_price) / (low_price + high_price)
    else:
        concentration = None
    return low_price, high_price, concentration


# ═══════════════════════════════════════════════════════
# Worker：从库内日线计算筹码分布
# ═══════════════════════════════════════════════════════

def _fetch_one_worker(symbols: list[str], adjust: str = "qfq") -> tuple[list[dict], int]:
    """Worker 进程：从 ``daily_prices`` 读取 K 线，本地计算筹码分布。

    **无网络依赖** —— 全部计算基于已入库的日线数据。
    """
    from zplan_shared.db_engine import build_engine as _build_eng

    engine = _build_eng()
    rows: list[dict] = []
    fail_count = 0

    for idx, symbol in enumerate(symbols, 1):
        try:
            # 从库内读取该股票的日线数据
            df = pd.read_sql_query(
                text(
                    "SELECT trade_date, open, high, low, close, turnover_rate "
                    "FROM daily_prices "
                    "WHERE ts_code = :code AND market = 'a' "
                    "ORDER BY trade_date ASC"
                ),
                engine,
                params={"code": symbol},
            )
        except Exception:
            fail_count += 1
            continue

        if df.empty or len(df) < 5:
            fail_count += 1
            continue

        # 转为 dict 列表
        bars = df.to_dict(orient="records")
        # trade_date 转为 date 对象
        for b in bars:
            if isinstance(b["trade_date"], str):
                b["trade_date"] = pd.to_datetime(b["trade_date"]).date()

        try:
            chip_rows = _compute_cyq_for_stock(bars)
        except Exception:
            logger.debug("cyq compute failed for %s", symbol, exc_info=True)
            fail_count += 1
            continue

        for cr in chip_rows:
            cr["ts_code"] = symbol
            cr["market"] = "a"
        rows.extend(chip_rows)

        if (idx + 1) % 50 == 0:
            logger.info("[Worker %s] %s/%s", os.getpid(), idx + 1, len(symbols))

    return rows, fail_count


# ═══════════════════════════════════════════════════════
# 增量检测 & Upsert
# ═══════════════════════════════════════════════════════

def _get_stale_symbols() -> list[str]:
    """返回需要更新的符号列表。"""
    init_db()
    cutoff = date.today() - timedelta(days=CYQ_MAX_STALE_DAYS)
    with SessionLocal() as session:
        all_symbols = [
            r[0]
            for r in session.execute(
                select(StockList.ts_code).where(StockList.market == "a")
            ).all()
        ]
        if not all_symbols:
            logger.warning("筹码峰 ETL: stock_list 中无 A 股标的")
            return []

        stmt = text(
            "SELECT ts_code, MAX(trade_date) FROM daily_chip "
            "WHERE market = 'a' GROUP BY ts_code"
        )
        raw_dates = dict(session.execute(stmt).fetchall())
        max_dates: dict[str, date | None] = {}
        for code, val in raw_dates.items():
            if val is None:
                max_dates[code] = None
            elif isinstance(val, date):
                max_dates[code] = val
            else:
                try:
                    max_dates[code] = pd.to_datetime(val).date()
                except Exception:
                    max_dates[code] = None

    stale = [
        s
        for s in all_symbols
        if s not in max_dates or max_dates[s] is None or max_dates[s] < cutoff
    ]
    logger.info(
        "筹码峰: %s 只中 %s 只需更新 (cutoff=%s)",
        len(all_symbols),
        len(stale),
        cutoff,
    )
    return stale


def upsert_daily_chip(rows: list[dict]) -> int:
    """批量 upsert ``daily_chip`` 行。"""
    if not rows:
        return 0
    ingested_at = datetime.utcnow()
    for r in rows:
        r["ingested_at"] = ingested_at

    update_cols = [
        "profit_ratio",
        "avg_cost",
        "cost_90_low",
        "cost_90_high",
        "concentration_90",
        "cost_70_low",
        "cost_70_high",
        "concentration_70",
        "ingested_at",
    ]
    total = 0
    with SessionLocal() as session:
        for i in range(0, len(rows), _UPSERT_BATCH):
            chunk = rows[i : i + _UPSERT_BATCH]
            stmt = insert(DailyChip).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "trade_date", "market"],
                set_={col: getattr(stmt.excluded, col) for col in update_cols},
            )
            session.execute(stmt)
            total += len(chunk)
        session.commit()
    return total


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

def run_daily_chip_update(
    *,
    limit: int | None = None,
    force_all: bool = False,
) -> dict[str, int | str | bool]:
    """全市场筹码峰 ETL（纯本地计算，无网络依赖）。

    首次运行约 10-20 分钟（5000 只 × 本地计算 / 6 进程）；
    增量仅处理数据过时的标的。
    """
    init_db()
    if force_all:
        with SessionLocal() as session:
            symbols = [
                r[0]
                for r in session.execute(
                    select(StockList.ts_code).where(StockList.market == "a")
                ).all()
            ]
    else:
        symbols = _get_stale_symbols()

    if limit:
        symbols = symbols[:limit]

    if not symbols:
        return {"ok": 0, "fail": 0, "rows": 0, "source": SOURCE, "skipped": True}

    n_workers = min(CYQ_WORKERS, len(symbols))
    chunks = _chunk_list(symbols, n_workers)

    all_rows: list[dict] = []
    total_fail = 0
    logger.info("筹码峰 ETL 开始: %s 只, %s 进程 (本地计算)", len(symbols), n_workers)
    t0 = time.monotonic()
    with multiprocessing.Pool(processes=n_workers, maxtasksperchild=1) as pool:
        results = pool.map(_fetch_one_worker, chunks)

    for chunk_result in results:
        if isinstance(chunk_result, tuple):
            rows_part, fail_part = chunk_result
            all_rows.extend(rows_part)
            total_fail += fail_part
        else:
            all_rows.extend(chunk_result)

    n = upsert_daily_chip(all_rows)
    elapsed = time.monotonic() - t0
    stats: dict[str, int | str | bool] = {
        "ok": len(symbols) - total_fail,
        "fail": total_fail,
        "rows": n,
        "source": SOURCE,
        "elapsed_s": round(elapsed, 1),
    }
    logger.info(
        "筹码峰 ETL 完成: %s 只 → %s 行 (fail=%s), %.1f 秒",
        len(symbols),
        n,
        total_fail,
        elapsed,
    )
    return stats


def _chunk_list(lst: list, n: int) -> list[list]:
    """均匀分片。"""
    k, m = divmod(len(lst), n)
    chunks = []
    for i in range(n):
        s = i * k + min(i, m)
        e = (i + 1) * k + min(i + 1, m)
        chunks.append(lst[s:e])
    return chunks
