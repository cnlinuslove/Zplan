#!/usr/bin/env python3
"""无网络时写入少量演示日线，便于验证选股/回测 Agent。有网后请用 zplan-股价 正式同步覆盖。"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.dialects.sqlite import insert

from zplan_shared.models import DailyPrice, SessionLocal, StockList, init_db

DEMO_CODES = (
    ("000001", "平安银行"),
    ("600519", "贵州茅台"),
    ("300750", "宁德时代"),
)


def main() -> None:
    init_db()
    base = date(2025, 5, 6)
    with SessionLocal() as session:
        for ts_code, name in DEMO_CODES:
            session.execute(
                insert(StockList)
                .values(ts_code=ts_code, name=name, industry=None, listing_date=None)
                .on_conflict_do_update(
                    index_elements=[StockList.ts_code],
                    set_={"name": name},
                )
            )
        rows = []
        now = datetime.now(UTC).replace(tzinfo=None)
        for i, (ts_code, _) in enumerate(DEMO_CODES):
            for d in range(5):
                trade_date = base + timedelta(days=d)
                close = 10.0 + i * 50 + d * 0.5
                rows.append(
                    {
                        "ts_code": ts_code,
                        "trade_date": trade_date,
                        "open": close - 0.2,
                        "high": close + 0.3,
                        "low": close - 0.4,
                        "close": close,
                        "volume": 1_000_000 + d * 1000,
                        "amount": close * 1_000_000,
                        "amplitude": 1.5 + d * 0.1,
                        "pct_chg": 0.5 + d * 0.2,
                        "change_amt": 0.05 * d,
                        "turnover_rate": 0.8 + d * 0.05,
                        "adjust_type": "qfq",
                        "source": "demo_seed",
                        "ingested_at": now,
                    }
                )
        stmt = insert(DailyPrice).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ts_code", "trade_date"],
            set_={c: stmt.excluded[c] for c in rows[0] if c not in ("ts_code", "trade_date")},
        )
        session.execute(stmt)
        session.commit()
    print(f"[OK] 演示数据已写入 {len(rows)} 条日线（{len(DEMO_CODES)} 只股票）")


if __name__ == "__main__":
    main()
