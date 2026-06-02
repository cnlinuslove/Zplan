#!/usr/bin/env python3
"""物化最新交易日技术指标快照 → daily_features。"""
from __future__ import annotations

import argparse
import logging

from zplan_shared.etl_daily_features import count_features_coverage, run_daily_features_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="技术指标日频快照 ETL")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--calendar-days", type=int, default=150)
    parser.add_argument("--min-bars", type=int, default=None)
    args = parser.parse_args()
    stats = run_daily_features_update(
        limit=args.limit,
        calendar_days=args.calendar_days,
        min_bars=args.min_bars,
    )
    feat_n, bar_n = count_features_coverage()
    print(f"覆盖率: daily_features={feat_n} / 当日日线={bar_n}")
    print(stats)


if __name__ == "__main__":
    main()
