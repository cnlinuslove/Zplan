#!/usr/bin/env python3
"""导出全市场概念题材 CSV（需先 sync_stock_concepts）。"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.market import get_concepts_panel
from zplan_shared.models import SessionLocal, StockList, init_db
from sqlalchemy import select


def main() -> None:
    parser = argparse.ArgumentParser(description="导出概念题材 CSV")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ZPLAN_ROOT / "exports" / "stock_concepts.csv",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="长表：每行 (ts_code, concept_name, board_kind)",
    )
    args = parser.parse_args()
    init_db()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    if args.detail:
        from zplan_shared.models import StockConceptMember

        with SessionLocal() as db:
            rows = db.execute(
                select(
                    StockConceptMember.ts_code,
                    StockConceptMember.concept_name,
                    StockConceptMember.name,
                ).order_by(StockConceptMember.ts_code, StockConceptMember.concept_name)
            ).all()
            names = {
                r[0]: r[1]
                for r in db.execute(select(StockList.ts_code, StockList.name))
            }
        df = pd.DataFrame(rows, columns=["ts_code", "concept_name", "name"])
        df["name"] = df["name"].fillna(df["ts_code"].map(names))
    else:
        panel = get_concepts_panel()
        with SessionLocal() as db:
            names = {
                r[0]: r[1]
                for r in db.execute(select(StockList.ts_code, StockList.name))
            }
        panel.insert(1, "name", panel["ts_code"].map(names))
        df = panel

    df.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"已写入 {args.output}（{len(df)} 行）")


if __name__ == "__main__":
    main()
