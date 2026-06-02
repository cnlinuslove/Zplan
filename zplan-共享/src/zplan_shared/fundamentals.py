"""基本面只读 API（Phase B / D）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import func, select

from zplan_shared.market import latest_trade_date
from zplan_shared.models import DailySnapshot, FinancialIndicator, SessionLocal, init_db


def _parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def get_snapshot(
    as_of: str | date | None = None,
    *,
    fields: Iterable[str] | None = None,
) -> pd.DataFrame:
    """指定交易日全市场估值截面；``as_of`` 默认库内 snapshot 最新日。"""
    init_db()
    as_of_d = _parse_date(as_of)
    if as_of_d is None:
        with SessionLocal() as session:
            as_of_d = session.execute(select(func.max(DailySnapshot.trade_date))).scalar_one_or_none()
        if as_of_d is None:
            as_of_d = latest_trade_date()
    if as_of_d is None:
        return pd.DataFrame()

    want = list(fields) if fields else [
        "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv", "turnover_rate", "source"
    ]
    cols = [c for c in want if c != "ts_code"]

    with SessionLocal() as session:
        rows = session.execute(
            select(DailySnapshot).where(DailySnapshot.trade_date == as_of_d)
        ).scalars().all()

    records: list[dict[str, Any]] = []
    for r in rows:
        records.append({"ts_code": r.ts_code, **{c: getattr(r, c) for c in cols}})
    return pd.DataFrame(records)


def get_financials(ts_code: str, *, limit: int = 8) -> pd.DataFrame:
    """单票财报指标，按报告期倒序。"""
    init_db()
    code = ts_code.strip()
    if "." in code:
        code = code.split(".", 1)[0]

    with SessionLocal() as session:
        rows = (
            session.execute(
                select(FinancialIndicator)
                .where(FinancialIndicator.ts_code == code)
                .order_by(FinancialIndicator.report_date.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "ts_code", "report_date", "pe_ttm", "pb",
                "revenue", "net_profit", "roe", "source",
            ]
        )

    return pd.DataFrame(
        [
            {
                "ts_code": r.ts_code,
                "report_date": r.report_date,
                "pe_ttm": r.pe_ttm,
                "pb": r.pb,
                "revenue": r.revenue,
                "net_profit": r.net_profit,
                "roe": r.roe,
                "source": r.source,
            }
            for r in rows
        ]
    )
