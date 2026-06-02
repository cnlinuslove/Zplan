"""技术指标快照只读 API（``daily_features`` 表）。"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select

from zplan_shared.features import SNAPSHOT_FLAG_KEYS, SNAPSHOT_FLOAT_KEYS
from zplan_shared.market import _parse_date, latest_trade_date
from zplan_shared.models import DailyFeature, SessionLocal, init_db

_FEATURE_COLS = ("ts_code", "trade_date", *SNAPSHOT_FLOAT_KEYS, *SNAPSHOT_FLAG_KEYS)


def get_features_panel(as_of: str | date | None = None) -> pd.DataFrame:
    """指定交易日全市场技术指标截面；无数据时返回空 DataFrame。"""
    init_db()
    as_of_d = _parse_date(as_of) if as_of is not None else latest_trade_date()
    if as_of_d is None:
        return pd.DataFrame()

    with SessionLocal() as session:
        rows = session.execute(
            select(DailyFeature).where(DailyFeature.trade_date == as_of_d)
        ).scalars().all()

    if not rows:
        return pd.DataFrame()

    records = [{c: getattr(r, c) for c in _FEATURE_COLS} for r in rows]
    return pd.DataFrame(records)


def get_stock_features(ts_code: str, as_of: str | date | None = None) -> dict:
    """单票指标快照字典。"""
    init_db()
    from zplan_shared.market import resolve_ts_code

    code = resolve_ts_code(ts_code)
    as_of_d = _parse_date(as_of) if as_of is not None else latest_trade_date()
    if as_of_d is None:
        return {}

    with SessionLocal() as session:
        row = session.execute(
            select(DailyFeature).where(
                DailyFeature.ts_code == code,
                DailyFeature.trade_date == as_of_d,
            )
        ).scalar_one_or_none()

    if row is None:
        return {}
    return {c: getattr(row, c) for c in _FEATURE_COLS if c not in ("ts_code", "trade_date")}
