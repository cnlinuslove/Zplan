#!/usr/bin/env python3
"""
历史日线回填脚本：对 ``daily_prices`` 缺失历史区间的标的，从 AkShare 拉取更早的日线。

用法::

    # 仅查看哪些标的缺历史数据
    .venv/bin/python scripts/backfill_history.py --dry-run

    # 小批测试（10 只，2 进程）
    .venv/bin/python scripts/backfill_history.py --limit 10 --workers 2

    # 全量回填（默认 6 进程，目标回填至 3 年前）
    .venv/bin/python scripts/backfill_history.py --workers 6

    # 自定义目标起始日期
    .venv/bin/python scripts/backfill_history.py --target-start 20220101 --workers 8

设计：
- 复用 ``fetch_daily_bars`` + ``upsert_daily_prices``，ON CONFLICT 保证幂等。
- 多进程并行（ProcessPoolExecutor），每进程内串行 + throttle，避免 AkShare 限流。
- 可随时 Ctrl+C 中断，已写入数据不丢失。
"""

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# 确保 zplan-共享 在 sys.path 中
_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
_SHARED = _PROJECT.parent / "zplan-共享" / "src"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

os.environ.setdefault("ZPLAN_ROOT", str(_PROJECT.parent / "zplan-资讯"))

from zplan_shared.config import (
    AKSHARE_RATE_LIMIT_SECONDS,
    ZPLAN_ROOT,
)
from zplan_shared.models import DailyPrice, StockList, SessionLocal, init_db
from zplan_shared.etl_akshare import (
    configure_akshare_http,
    fetch_daily_bars,
    upsert_daily_prices,
    _db_write_lock,
    _enrich_daily_derived_fields,
)

LOG_DIR = ZPLAN_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "backfill_history.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("backfill_history")

# 默认回填起始日：3 年前
DEFAULT_TARGET_START = (pd.Timestamp.today() - pd.Timedelta(days=3 * 365)).strftime("%Y%m%d")


def _get_earliest_date(ts_code: str) -> date | None:
    """查询某只股票在 daily_prices 中的最早交易日。"""
    from sqlalchemy import func

    with SessionLocal() as session:
        result = (
            session.query(func.min(DailyPrice.trade_date))
            .filter(DailyPrice.ts_code == ts_code)
            .scalar()
        )
    return result


def _find_stocks_needing_backfill(target_start: str) -> list[dict]:
    """
    找出需要回填的股票列表。

    Returns:
        list of dicts: ``[{ts_code, name, earliest_date, gap_days}, ...]``
    """
    target_dt = pd.Timestamp(target_start).date()
    today = date.today()

    with SessionLocal() as session:
        stocks = (
            session.query(StockList.ts_code, StockList.name)
            .order_by(StockList.ts_code)
            .all()
        )

    need_backfill = []
    for ts_code, name in stocks:
        earliest = _get_earliest_date(ts_code)
        if earliest is None:
            # 完全没有数据的股票（不应该很多）
            need_backfill.append(
                {
                    "ts_code": ts_code,
                    "name": name or "",
                    "earliest_date": None,
                    "gap_days": (today - target_dt).days,
                }
            )
        elif earliest > target_dt:
            gap = (earliest - target_dt).days
            if gap >= 5:  # 至少缺 5 个自然日才回填（过滤掉一两天的小缺口）
                need_backfill.append(
                    {
                        "ts_code": ts_code,
                        "name": name or "",
                        "earliest_date": earliest,
                        "gap_days": gap,
                    }
                )

    need_backfill.sort(key=lambda x: x["gap_days"], reverse=True)
    return need_backfill


def _backfill_one_symbol(args: tuple[str, str, float]) -> tuple[str, int, bool, str]:
    """
    单票回填（进程内调用，避免 AkShare/mini_racer 跨线程问题）。

    Args:
        args: (ts_code, target_start, interval)

    Returns:
        (ts_code, rows_upserted, ok, error_msg)
    """
    from zplan_shared.market import resolve_ts_code

    ts_code, target_start, interval = args

    configure_akshare_http()

    try:
        symbol = resolve_ts_code(ts_code)
        price_df, source = fetch_daily_bars(symbol=symbol, start_date=target_start)

        if price_df.empty:
            return ts_code, 0, True, "empty"

        # 衍生字段补全（新浪/TX 可能缺 pct_chg、amplitude 等）
        price_df = _enrich_daily_derived_fields(price_df)

        with _db_write_lock:
            upsert_n = upsert_daily_prices(ts_code, price_df, source=source)

        time.sleep(interval)
        return ts_code, upsert_n, True, ""

    except Exception as exc:
        logger.warning("[WARN] 回填 %s 失败: %s", ts_code, exc)
        time.sleep(max(interval, 2.0))
        return ts_code, 0, False, str(exc)[:200]


def _backfill_worker(args: tuple[str, str, float]) -> tuple[str, int, bool, str]:
    return _backfill_one_symbol(args)


def run_backfill(
    target_start: str = DEFAULT_TARGET_START,
    workers: int = 6,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """
    执行历史日线回填。

    Args:
        target_start: 目标起始日期（YYYYMMDD），回填此日期之后的数据。
        workers: 并行进程数（1~16）。
        limit: 仅处理前 N 只股票（调试用）。
        dry_run: 仅列出需要回填的股票，不实际拉取。
    """
    configure_akshare_http()
    init_db()

    logger.info("扫描需要回填的股票（目标起始日: %s）...", target_start)
    stocks = _find_stocks_needing_backfill(target_start)

    if limit:
        stocks = stocks[:limit]

    total_gap_stocks = len(stocks)
    if total_gap_stocks == 0:
        logger.info("所有股票数据已覆盖至 %s，无需回填。", target_start)
        return {"target_start": target_start, "queued": 0, "updated": 0, "failed": 0, "rows": 0}

    total_gap_days = sum(s["gap_days"] for s in stocks)
    no_data_count = sum(1 for s in stocks if s["earliest_date"] is None)
    min_gap = min(s["gap_days"] for s in stocks)
    max_gap = max(s["gap_days"] for s in stocks)

    logger.info(
        "需回填 %s 只股票（无数据: %s），缺口范围 %s~%s 天，合计 %s 天",
        total_gap_stocks,
        no_data_count,
        min_gap,
        max_gap,
        total_gap_days,
    )

    if dry_run:
        logger.info("=== DRY RUN 模式，不拉取数据 ===")
        for i, s in enumerate(stocks[:50], 1):
            earliest_str = s["earliest_date"].isoformat() if s["earliest_date"] else "无数据"
            logger.info(
                "  %s. %s %s: 最早=%s, 缺口=%s天",
                i,
                s["ts_code"],
                s["name"],
                earliest_str,
                s["gap_days"],
            )
        if len(stocks) > 50:
            logger.info("  ... 及 %s 只（省略）", len(stocks) - 50)
        return {
            "target_start": target_start,
            "queued": total_gap_stocks,
            "updated": 0,
            "failed": 0,
            "rows": 0,
            "dry_run": True,
        }

    n_workers = max(1, min(workers, 16))
    base_interval = float(AKSHARE_RATE_LIMIT_SECONDS)
    interval = max(0.35, base_interval / n_workers)

    logger.info(
        "开始回填：workers=%s, interval=%.2fs/票, 共 %s 只",
        n_workers,
        interval,
        total_gap_stocks,
    )

    updated = 0
    failed = 0
    total_rows = 0
    done = 0
    start_time = time.monotonic()

    # 准备 worker 参数
    tasks = [(s["ts_code"], target_start, interval) for s in stocks]

    try:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_backfill_worker, task): task[0] for task in tasks}
            for fut in as_completed(futures):
                done += 1
                ts_code, upsert_n, ok, err_msg = fut.result()
                if ok:
                    updated += 1
                    total_rows += upsert_n
                else:
                    failed += 1

                if done % 100 == 0 or done == total_gap_stocks:
                    elapsed = time.monotonic() - start_time
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total_gap_stocks - done) / rate if rate > 0 else 0
                    logger.info(
                        "[进度] %s/%s ok=%s fail=%s rows=%s elapsed=%.0fs rate=%.1f/s ETA=%.0fs",
                        done,
                        total_gap_stocks,
                        updated,
                        failed,
                        total_rows,
                        elapsed,
                        rate,
                        eta,
                    )
    except KeyboardInterrupt:
        logger.info("用户中断，已写入 %s 行（%s 只完成），可安全重跑", total_rows, updated)

    stats = {
        "target_start": target_start,
        "queued": total_gap_stocks,
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "elapsed_s": time.monotonic() - start_time,
    }
    logger.info("回填完成: %s", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="历史日线回填：补齐 daily_prices 缺失的早期数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--target-start",
        default=DEFAULT_TARGET_START,
        help=f"回填目标起始日期 YYYYMMDD（默认: {DEFAULT_TARGET_START}，即 3 年前）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("BACKFILL_WORKERS", "6")),
        help="并行进程数（1~16，默认 6）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅处理前 N 只股票（调试用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅扫描并显示需要回填的股票，不实际拉取",
    )
    args = parser.parse_args()

    try:
        stats = run_backfill(
            target_start=args.target_start,
            workers=args.workers,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        print("\n统计:", stats)
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)


if __name__ == "__main__":
    main()
