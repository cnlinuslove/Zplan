#!/usr/bin/env python3
"""回填 daily_prices 涨跌幅/涨跌额/振幅（默认仅近端若干日，供日更加速）。"""
from __future__ import annotations

import argparse
import logging
import os

import pandas as pd
from sqlalchemy import func, select, update

from zplan_shared.etl_akshare import _enrich_daily_derived_fields
from zplan_shared.market import latest_trade_date
from zplan_shared.models import DailyPrice, SessionLocal, init_db

logger = logging.getLogger(__name__)
_BATCH = 500
_DEFAULT_RECENT = int(os.getenv("ENRICH_RECENT_CALENDAR_DAYS", "8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 只股票")
    parser.add_argument(
        "--recent-days",
        type=int,
        default=_DEFAULT_RECENT,
        help="仅处理最近 N 个自然日内的 K 线（日更默认 8）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="全历史回填（慢，仅首次或修复时用）",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()

    cutoff = None
    if not args.full:
        end = latest_trade_date()
        if end is None:
            logger.warning("无日线，跳过 enrich")
            return
        cutoff = end - pd.Timedelta(days=args.recent_days)

    with SessionLocal() as session:
        codes = session.execute(select(DailyPrice.ts_code).distinct()).scalars().all()
    codes = sorted(set(codes))
    if args.limit:
        codes = codes[: args.limit]

    updated_rows = 0
    for idx, code in enumerate(codes, 1):
        with SessionLocal() as session:
            stmt = (
                select(DailyPrice)
                .where(DailyPrice.ts_code == code)
                .order_by(DailyPrice.trade_date)
            )
            if cutoff is not None:
                stmt = stmt.where(DailyPrice.trade_date >= cutoff.date())
            rows = session.execute(stmt).scalars().all()
            if not rows:
                continue
            df = pd.DataFrame(
                [
                    {
                        "日期": r.trade_date,
                        "开盘": r.open,
                        "最高": r.high,
                        "最低": r.low,
                        "收盘": r.close,
                        "涨跌幅": r.pct_chg,
                        "涨跌额": r.change_amt,
                        "振幅": r.amplitude,
                    }
                    for r in rows
                ]
            )
            enriched = _enrich_daily_derived_fields(df)
            for i, row in enumerate(rows):
                er = enriched.iloc[i]
                row.pct_chg = float(er["涨跌幅"]) if pd.notna(er["涨跌幅"]) else None
                row.change_amt = float(er["涨跌额"]) if pd.notna(er["涨跌额"]) else None
                row.amplitude = float(er["振幅"]) if pd.notna(er["振幅"]) else None
                updated_rows += 1
            session.commit()
        if idx % 200 == 0:
            logger.info("[%s/%s] 已处理", idx, len(codes))

    logger.info(
        "回填完成，更新 %s 行（mode=%s）",
        updated_rows,
        "full" if args.full else f"recent-{args.recent_days}d",
    )


if __name__ == "__main__":
    main()
