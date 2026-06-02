#!/usr/bin/env python3
"""全市场同步个股概念/题材到 stock_concept（东财 F10）。"""
from __future__ import annotations

import argparse
import logging
import sys

from zplan_shared.stock_concept_etl import backfill_stock_concepts


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 A 股概念题材")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 只（调试）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="重拉已有概念数据的股票（默认只补缺失）",
    )
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="写入全部 ssbk（含行业/地域/指数标签）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.all_kinds:
        import os

        os.environ["STOCK_CONCEPT_ALL_KINDS"] = "true"
    stats = backfill_stock_concepts(
        limit=args.limit,
        only_missing=not args.force,
    )
    print(stats)
    if stats.get("fail"):
        sys.exit(1)


if __name__ == "__main__":
    main()
