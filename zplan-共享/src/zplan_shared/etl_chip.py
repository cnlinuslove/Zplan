"""Phase CYQ：筹码峰日频数据 ETL（东方财富，逐票拉取，多进程）。

数据源：``ak.stock_cyq_em(symbol, adjust="qfq")``，每票返回 ~90 日
筹码分布序列。``py_mini_racer`` 非线程安全，采用多进程并行。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.config import CYQ_MAX_STALE_DAYS, CYQ_RATE_LIMIT, CYQ_WORKERS
from zplan_shared.http_client import configure_akshare_http
from zplan_shared.market import latest_trade_date
from zplan_shared.models import DailyChip, SessionLocal, StockList, init_db

logger = logging.getLogger(__name__)

SOURCE = "akshare_cyq_em"
_UPSERT_BATCH = 100
_MAX_RETRIES = 3


# ── worker 进程 ────────────────────────────────────────────


def _fetch_one_worker(symbols: list[str], adjust: str = "qfq") -> tuple[list[dict], int]:
    """Worker 进程入口：独立 akshare 实例 + 独立限流。

    每个进程调用 ``configure_akshare_http()`` 以 patch requests，
    确保东财直连路由生效（含代理 fallback）。
    单票失败时最多重试 ``_MAX_RETRIES`` 次（指数退避）。
    """
    import akshare as ak

    configure_akshare_http()
    rows: list[dict] = []
    fail_count = 0
    for idx, symbol in enumerate(symbols, 1):
        df = None
        for attempt in range(_MAX_RETRIES):
            try:
                df = ak.stock_cyq_em(symbol=symbol, adjust=adjust)
                break
            except Exception:
                if attempt < _MAX_RETRIES - 1:
                    delay = (2 ** attempt) + random.uniform(0.5, 1.5)
                    time.sleep(delay)
                    continue
                logger.debug("cyq fetch failed for %s after %d retries", symbol, _MAX_RETRIES, exc_info=True)
        if df is None or df.empty:
            fail_count += 1
            time.sleep(CYQ_RATE_LIMIT)
            continue
        for _, r in df.iterrows():
            rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": _parse_date(r.get("日期")),
                    "profit_ratio": _safe_float(r.get("获利比例")),
                    "avg_cost": _safe_float(r.get("平均成本")),
                    "cost_90_low": _safe_float(r.get("90成本-低")),
                    "cost_90_high": _safe_float(r.get("90成本-高")),
                    "concentration_90": _safe_float(r.get("90集中度")),
                    "cost_70_low": _safe_float(r.get("70成本-低")),
                    "cost_70_high": _safe_float(r.get("70成本-高")),
                    "concentration_70": _safe_float(r.get("70集中度")),
                    "market": "a",
                }
            )
        if (idx + 1) % 50 == 0:
            logger.info("[Worker %s] %s/%s", os.getpid(), idx + 1, len(symbols))
        time.sleep(CYQ_RATE_LIMIT)
    return rows, fail_count


# ── 增量检测 ──────────────────────────────────────────────


def _get_stale_symbols() -> list[str]:
    """返回需要更新的符号列表。

    逻辑：查每票在 ``daily_chip`` 的最新 ``trade_date``，
    若不存在或早于 ``CYQ_MAX_STALE_DAYS`` 天前则加入更新列表。
    """
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
        # SQLite 可能返回字符串，统一转 date
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


# ── 批量 upsert ───────────────────────────────────────────


def upsert_daily_chip(rows: list[dict]) -> int:
    """批量 upsert ``daily_chip`` 行，冲突时更新除 PK 外的全部字段。"""
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


# ── 主入口 ─────────────────────────────────────────────────


def run_daily_chip_update(
    *,
    limit: int | None = None,
    force_all: bool = False,
) -> dict[str, int | str | bool]:
    """全市场筹码峰 ETL：多进程拉取 + 批量 upsert。

    首次运行约 40 分钟（5000 只 × ~2s / 6 进程）；
    增量仅拉取过时标的（~5 分钟）。

    Args:
        limit: 限制标的数量（调试用）。
        force_all: 为 True 时忽略增量逻辑，拉取全部标的。
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
    logger.info("筹码峰 ETL 开始: %s 只, %s 进程", len(symbols), n_workers)
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
        "筹码峰 ETL 完成: %s 只 → %s 行 (fail=%s), %s 秒",
        len(symbols),
        n,
        total_fail,
        round(elapsed, 1),
    )
    return stats


# ── 工具函数 ──────────────────────────────────────────────


def _chunk_list(lst: list, n: int) -> list[list]:
    """均匀分片。"""
    k, m = divmod(len(lst), n)
    chunks = []
    for i in range(n):
        start = i * k + min(i, m)
        end = (i + 1) * k + min(i + 1, m)
        chunks.append(lst[start:end])
    return chunks


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None
