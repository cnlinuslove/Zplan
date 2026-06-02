#!/usr/bin/env python3
"""P0 数据：stock_list 元数据回填 + news_stock_link 补链 + 质量报表。"""
from __future__ import annotations

import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan P0 数据回填")
    parser.add_argument("--meta-only", action="store_true", help="仅回填 industry/listing_date")
    parser.add_argument("--link-only", action="store_true", help="仅补 news_stock_link")
    parser.add_argument("--force-meta", action="store_true", help="覆盖已有 industry/listing_date")
    parser.add_argument(
        "--exchange-meta-only",
        action="store_true",
        help="元数据仅用沪深京交易所列表，不拉东财行业板块（网络受限时）",
    )
    parser.add_argument("--link-hours", type=int, default=168)
    parser.add_argument(
        "--link-relink",
        action="store_true",
        help="重扫窗口内全部新闻（应用新关联规则，非仅未关联）",
    )
    parser.add_argument("--link-limit", type=int, default=3000, help="每表最多处理条数")
    args = parser.parse_args()

    out: dict = {}

    if not args.link_only:
        from zplan_shared.stock_meta_etl import backfill_stock_list_meta

        out["stock_meta"] = backfill_stock_list_meta(
            only_missing=not args.force_meta,
            include_em_industry=not args.exchange_meta_only,
        )

    if not args.meta_only:
        from zplan_shared.news_linker import link_recent_news, news_link_coverage_stats

        out["news_link"] = link_recent_news(
            hours=args.link_hours,
            limit_per_table=args.link_limit,
            relink=args.link_relink,
        )
        out["coverage_48h"] = news_link_coverage_stats(hours=48)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
