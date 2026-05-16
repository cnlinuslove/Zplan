"""行情只读查询 API — 各 Agent 统一入口，见 ``docs/DATA_ARCHITECTURE.md``。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import func, select

from zplan_shared.intraday_store import read_intraday_parquet
from zplan_shared.models import DailyPrice, SessionLocal, init_db

DEFAULT_ADJUST_TYPE = "qfq"
BAR_COLUMNS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "pct_chg",
    "change_amt",
    "turnover_rate",
    "adjust_type",
    "source",
)


def resolve_ts_code(code: str) -> str:
    """去掉 ``.SH`` / ``.SZ`` 等后缀，与库内 ``ts_code`` 对齐。"""
    raw = code.strip().upper()
    if "." in raw:
        return raw.split(".", 1)[0]
    return raw


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


def latest_trade_date(*, adjust_type: str = DEFAULT_ADJUST_TYPE) -> date | None:
    init_db()
    with SessionLocal() as session:
        return session.execute(
            select(func.max(DailyPrice.trade_date)).where(DailyPrice.adjust_type == adjust_type)
        ).scalar_one_or_none()


def get_bars(
    ts_code: str,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    adjust_type: str = DEFAULT_ADJUST_TYPE,
) -> pd.DataFrame:
    """单票日线 → DataFrame，索引为 ``trade_date``。"""
    init_db()
    code = resolve_ts_code(ts_code)
    start_d = _parse_date(start)
    end_d = _parse_date(end)
    stmt = (
        select(DailyPrice)
        .where(DailyPrice.ts_code == code, DailyPrice.adjust_type == adjust_type)
        .order_by(DailyPrice.trade_date)
    )
    if start_d:
        stmt = stmt.where(DailyPrice.trade_date >= start_d)
    if end_d:
        stmt = stmt.where(DailyPrice.trade_date <= end_d)

    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()

    if not rows:
        return pd.DataFrame(columns=["ts_code", *BAR_COLUMNS]).set_index(
            pd.Index([], name="trade_date")
        )

    records: list[dict[str, Any]] = []
    for row in rows:
        records.append(
            {
                "ts_code": row.ts_code,
                "trade_date": row.trade_date,
                **{col: getattr(row, col) for col in BAR_COLUMNS},
            }
        )
    df = pd.DataFrame(records).set_index("trade_date")
    return df


def get_panel(
    as_of: str | date | None = None,
    *,
    fields: Iterable[str] | None = None,
    adjust_type: str = DEFAULT_ADJUST_TYPE,
) -> pd.DataFrame:
    """指定交易日全市场截面；``as_of`` 默认库内最新交易日。"""
    init_db()
    as_of_d = _parse_date(as_of) if as_of is not None else latest_trade_date(adjust_type=adjust_type)
    if as_of_d is None:
        return pd.DataFrame()

    want = list(fields) if fields else ["close", "pct_chg", "turnover_rate", "volume", "amount"]
    cols = [c for c in want if c in BAR_COLUMNS or c == "ts_code"]

    with SessionLocal() as session:
        stmt = select(DailyPrice).where(
            DailyPrice.trade_date == as_of_d,
            DailyPrice.adjust_type == adjust_type,
        )
        rows = session.execute(stmt).scalars().all()

    records = [{"ts_code": r.ts_code, **{c: getattr(r, c) for c in cols}} for r in rows]
    return pd.DataFrame(records)


def as_of_close(
    ts_code: str,
    as_of: str | date,
    *,
    adjust_type: str = DEFAULT_ADJUST_TYPE,
) -> float | None:
    """资讯等场景：取某日收盘价（无则 ``None``）。"""
    init_db()
    code = resolve_ts_code(ts_code)
    as_of_d = _parse_date(as_of)
    if as_of_d is None:
        return None
    with SessionLocal() as session:
        row = session.execute(
            select(DailyPrice.close).where(
                DailyPrice.ts_code == code,
                DailyPrice.trade_date == as_of_d,
                DailyPrice.adjust_type == adjust_type,
            )
        ).scalar_one_or_none()
    return float(row) if row is not None else None


def get_minute_bars(
    ts_code: str,
    period: str = "5",
    *,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
) -> pd.DataFrame:
    """近端分时 K 线（Parquet）；``period`` 为 ``1``/``5``/``15`` 等，与 ETL 写入一致。"""
    code = resolve_ts_code(ts_code)
    start_ts = pd.to_datetime(start) if start is not None else None
    end_ts = pd.to_datetime(end) if end is not None else None
    df = read_intraday_parquet(code, period, start=start_ts, end=end_ts)
    if df.empty:
        return df
    return df.set_index("bar_time")
