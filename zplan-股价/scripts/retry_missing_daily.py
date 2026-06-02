#!/usr/bin/env python3
"""补拉尚无当前配置日线源（如 akshare_sina）的股票。"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

import pandas as pd

from zplan_shared.config import DAILY_BOOTSTRAP_CALENDAR_DAYS, ZPLAN_ROOT
from zplan_shared.data_sources import daily_source_tag
from zplan_shared.etl_akshare import (
    circuit_breaker,
    fetch_daily_bars,
    throttle,
    upsert_daily_prices,
)
from zplan_shared.models import init_db

logger = logging.getLogger(__name__)


def _missing_symbols() -> list[str]:
    init_db()
    expected = daily_source_tag()
    conn = sqlite3.connect(ZPLAN_ROOT / "zplan.db")
    listed = [r[0] for r in conn.execute("SELECT ts_code FROM stock_list")]
    have = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT ts_code FROM daily_prices WHERE source = ?",
            (expected,),
        )
    }
    conn.close()
    return sorted(set(listed) - have)


def main() -> None:
    parser = argparse.ArgumentParser(description="补拉缺失日线")
    parser.add_argument(
        "--file",
        help="每行一个 ts_code；默认自动找 stock_list 中缺当前 source 的票",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出待补拉数量（供 keep_alive 判断，不实际请求）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="任一失败则以非零退出（默认：仅全部失败才退出，不阻断日更流水线）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            symbols = [line.strip() for line in fh if line.strip()]
    else:
        symbols = _missing_symbols()

    if args.dry_run:
        print(len(symbols))
        return

    if not symbols:
        print("无需补拉。")
        return

    start_date = (
        pd.Timestamp.today() - pd.Timedelta(days=DAILY_BOOTSTRAP_CALENDAR_DAYS)
    ).strftime("%Y%m%d")
    tag = daily_source_tag()
    ok, fail = 0, 0
    logger.info("补拉 %s 只，source=%s，自 %s", len(symbols), tag, start_date)

    for idx, symbol in enumerate(symbols, 1):
        logger.info("[%s/%s] %s", idx, len(symbols), symbol)
        try:
            df, source = fetch_daily_bars(symbol, start_date)
            n = upsert_daily_prices(symbol, df, source=source)
            circuit_breaker.record_success()
            logger.info("%s 更新 %s 条 (%s)", symbol, n, source)
            ok += 1
        except Exception as exc:
            circuit_breaker.record_failure()
            logger.warning("%s 失败: %s", symbol, exc)
            fail += 1
            throttle(8)
        else:
            throttle()

    logger.info("补拉完成 ok=%s fail=%s", ok, fail)
    if fail and (args.strict or ok == 0):
        sys.exit(1)
    if fail:
        logger.warning("部分补拉失败，继续日更后续步骤（可用 --strict 改为失败即退出）")


if __name__ == "__main__":
    main()
