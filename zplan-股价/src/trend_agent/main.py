from __future__ import annotations

import argparse
import logging

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.etl_akshare import (
    clear_demo_market_data,
    run_a1_update,
    run_incremental_update,
)


logger = logging.getLogger(__name__)


def run_trend_agent(
    *,
    limit: int | None = None,
    init: bool = False,
    a1: bool = False,
    recent_days: int | None = None,
    prefer_tx: bool = False,
    skip_intraday: bool = False,
) -> dict:
    """股价 Agent：同步 A 股日线到共享库 ``daily_prices``。"""
    removed = 0
    if a1:
        run_a1_update(limit=limit, skip_intraday=skip_intraday, clear_demo=init)
    else:
        removed = clear_demo_market_data() if init else 0
        if init and removed:
            logger.info("已清除演示行情 %s 条（source=demo_seed）", removed)
        if init and recent_days is None:
            recent_days = 120
        if init:
            prefer_tx = True
        run_incremental_update(
            limit=limit, recent_days=recent_days, prefer_tx=prefer_tx
        )
    return {
        "ok": True,
        "agent": "trend",
        "zplan_root": str(ZPLAN_ROOT),
        "limit": limit,
        "init": init,
        "a1": a1,
        "demo_rows_removed": removed,
        "recent_days": recent_days,
        "prefer_tx": prefer_tx,
        "skip_intraday": skip_intraday,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan 股价 Agent")
    parser.add_argument(
        "--a1",
        action="store_true",
        help="Phase A.1：东财全市场日线 + 近两周分时(1min/5min)",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="清除 demo_seed；配合 --a1 或烟测拉取",
    )
    parser.add_argument(
        "--skip-intraday",
        action="store_true",
        help="A.1 时跳过分时（仅日线）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅更新前 N 只股票（调试用）",
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=None,
        metavar="N",
        help="无库内数据时只拉最近 N 天（--init 默认 120）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_trend_agent(
        limit=args.limit,
        init=args.init,
        a1=args.a1,
        recent_days=args.recent_days,
        skip_intraday=args.skip_intraday,
    )
    logger.info("完成: %s", result)


if __name__ == "__main__":
    main()
