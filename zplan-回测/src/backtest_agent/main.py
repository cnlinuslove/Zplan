from __future__ import annotations

import argparse
import logging

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.market import get_bars, resolve_ts_code
from zplan_shared.models import init_db


logger = logging.getLogger(__name__)


def run_backtest_agent(*, ts_code: str = "000001") -> dict:
    """回测 Agent：占位统计单票日线条数（待接入回测引擎）。"""
    init_db()
    code = resolve_ts_code(ts_code)
    df = get_bars(code)
    if df.empty:
        return {
            "ok": True,
            "agent": "backtest",
            "zplan_root": str(ZPLAN_ROOT),
            "ts_code": code,
            "bars": 0,
            "from": None,
            "to": None,
        }
    return {
        "ok": True,
        "agent": "backtest",
        "zplan_root": str(ZPLAN_ROOT),
        "ts_code": code,
        "bars": len(df),
        "from": str(df.index.min()),
        "to": str(df.index.max()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan 回测 Agent")
    parser.add_argument("--code", default="000001", help="股票代码")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_backtest_agent(ts_code=args.code)
    logger.info("完成: %s", result)


if __name__ == "__main__":
    main()
