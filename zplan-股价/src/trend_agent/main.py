from __future__ import annotations

import argparse
import logging

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.data_sources import daily_provider_label, daily_source_tag
from zplan_shared.etl_akshare import (
    clear_demo_market_data,
    run_a1_update,
    run_catchup_panel_update,
    run_incremental_update,
    run_post_incremental_catchup,
)
from zplan_shared.etl_financial import run_financial_indicators_update
from zplan_shared.etl_snapshot import run_daily_snapshot_update


logger = logging.getLogger(__name__)


def run_trend_agent(
    *,
    limit: int | None = None,
    init: bool = False,
    a1: bool = False,
    recent_days: int | None = None,
    skip_intraday: bool = False,
    realign_source: bool = False,
    snapshot: bool = False,
    financial: bool = False,
    enrich_daily: bool = False,
    catchup_panel: bool = False,
    catchup_workers: int | None = None,
    market: str = "a",
) -> dict:
    """股价 Agent：同步 A 股 / 港股日线到共享库 ``daily_prices``。"""
    removed = 0
    a1_stats: dict | None = None
    snapshot_stats: dict | None = None
    financial_stats: dict | None = None
    enrich_rows = 0
    inc_stats: dict | None = None
    catchup_stats: dict | None = None
    post_catchup_stats: dict | None = None

    if market == "hk":
        # ── 港股分支 ──
        from zplan_shared.etl_akshare_hk import run_hk_a1_update, run_hk_incremental_update

        if a1:
            a1_stats = run_hk_a1_update(limit=limit, skip_intraday=skip_intraday)
        else:
            inc_stats = run_hk_incremental_update(limit=limit)
        if snapshot:
            from zplan_shared.etl_snapshot_hk import run_hk_snapshot_update
            snapshot_stats = run_hk_snapshot_update(limit=limit)
        return {
            "ok": True,
            "agent": "trend",
            "market": "hk",
            "zplan_root": str(ZPLAN_ROOT),
            "limit": limit,
            "a1_stats": a1_stats,
            "incremental_stats": inc_stats,
            "snapshot_stats": snapshot_stats,
        }

    # ── A 股分支（原有逻辑）──
    if catchup_panel:
        catchup_stats = run_catchup_panel_update(limit=limit, workers=catchup_workers)
    elif a1:
        a1_stats = run_a1_update(
            limit=limit,
            skip_intraday=skip_intraday,
            clear_demo=init,
            realign_source=realign_source,
        )
    elif not (snapshot or financial or enrich_daily) and not catchup_panel:
        removed = clear_demo_market_data() if init else 0
        if init and removed:
            logger.info("已清除演示行情 %s 条（source=demo_seed）", removed)
        if init and recent_days is None:
            recent_days = 120
        inc_stats = run_incremental_update(limit=limit, recent_days=recent_days)
        post_catchup_stats = run_post_incremental_catchup(
            limit=limit, workers=catchup_workers
        )
    elif init:
        removed = clear_demo_market_data()
        if removed:
            logger.info("已清除演示行情 %s 条（source=demo_seed）", removed)
    if enrich_daily:
        import subprocess
        import sys
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "scripts" / "enrich_daily_fields.py"
        subprocess.check_call(
            [sys.executable, str(script)] + ([f"--limit={limit}"] if limit else []),
        )
        enrich_rows = -1
    if snapshot:
        snapshot_stats = run_daily_snapshot_update(limit=limit)
    if financial:
        financial_stats = run_financial_indicators_update(limit=limit)
    return {
        "ok": True,
        "agent": "trend",
        "market": "a",
        "zplan_root": str(ZPLAN_ROOT),
        "limit": limit,
        "init": init,
        "a1": a1,
        "demo_rows_removed": removed,
        "recent_days": recent_days,
        "daily_source": daily_source_tag(),
        "daily_provider": daily_provider_label(),
        "skip_intraday": skip_intraday,
        "realign_source": realign_source,
        "a1_stats": a1_stats,
        "incremental_stats": inc_stats,
        "post_catchup_stats": post_catchup_stats,
        "catchup_panel_stats": catchup_stats,
        "snapshot_stats": snapshot_stats,
        "financial_stats": financial_stats,
        "enrich_daily": enrich_daily,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan 股价 Agent（A 股 + 港股）")
    parser.add_argument(
        "--market",
        type=str,
        default="a",
        choices=("a", "hk"),
        help="目标市场：a=A 股（默认），hk=港股",
    )
    parser.add_argument(
        "--a1",
        action="store_true",
        help="Phase A.1：全市场日线 + 近两周分时",
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
        "--realign-source",
        action="store_true",
        help="A.1：对 source 与 AKSHARE_DAILY_PROVIDER 不一致的标的重拉日线",
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
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Phase B：全市场估值截面 daily_snapshot",
    )
    parser.add_argument(
        "--financial",
        action="store_true",
        help="Phase D：季报 financial_indicators ETL",
    )
    parser.add_argument(
        "--enrich-daily",
        action="store_true",
        help="回填涨跌幅/振幅等衍生量价字段",
    )
    parser.add_argument(
        "--catch-up-panel",
        action="store_true",
        help="仅补齐缺最新交易日截面的股票（选股 init-rule 前推荐）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="截面补齐并行线程数（默认 6，环境变量 CATCHUP_PANEL_WORKERS）",
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
        realign_source=args.realign_source,
        snapshot=args.snapshot,
        financial=args.financial,
        enrich_daily=args.enrich_daily,
        catchup_panel=args.catch_up_panel,
        catchup_workers=args.workers,
        market=args.market,
    )
    logger.info("完成: %s", result)


if __name__ == "__main__":
    main()
