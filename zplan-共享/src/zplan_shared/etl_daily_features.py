"""Phase A.3：技术指标日频快照物化（每票每个交易日一行）。"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.features import SNAPSHOT_FLAG_KEYS, SNAPSHOT_FLOAT_KEYS, scan_universe_features
from zplan_shared.market import get_history_window, latest_trade_date
from zplan_shared.models import DailyFeature, SessionLocal, init_db

logger = logging.getLogger(__name__)

_UPSERT_BATCH = 200
_MIN_BARS = int(__import__("os").getenv("DAILY_FEATURES_MIN_BARS", "60"))


def _row_from_series(ts_code: str, trade_date: date, row: pd.Series, *, market: str = "a") -> dict:
    ingested_at = datetime.utcnow()
    out: dict = {"ts_code": ts_code, "trade_date": trade_date, "market": market, "ingested_at": ingested_at}
    for k in SNAPSHOT_FLOAT_KEYS:
        if k in row.index:
            v = row[k]
            out[k] = None if pd.isna(v) else float(v)
    for k in SNAPSHOT_FLAG_KEYS:
        if k in row.index:
            v = row[k]
            out[k] = None if pd.isna(v) else float(v)
    return out


def upsert_daily_features(rows: list[dict]) -> int:
    if not rows:
        return 0
    total = 0
    update_cols = list(SNAPSHOT_FLOAT_KEYS) + list(SNAPSHOT_FLAG_KEYS)
    with SessionLocal() as session:
        for i in range(0, len(rows), _UPSERT_BATCH):
            chunk = rows[i : i + _UPSERT_BATCH]
            stmt = insert(DailyFeature).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "trade_date", "market"],
                set_={col: getattr(stmt.excluded, col) for col in update_cols}
                | {"ingested_at": stmt.excluded.ingested_at},
            )
            session.execute(stmt)
            total += len(chunk)
        session.commit()
    return total


def run_daily_features_update(
    *,
    as_of: date | str | None = None,
    calendar_days: int = 150,
    min_bars: int | None = None,
    limit: int | None = None,
    market: str = "a",
) -> dict[str, int]:
    """
    由 ``get_history_window`` 批量计算指标，仅 upsert ``as_of`` 当日快照（准确且快）。
    """
    init_db()
    min_bars = min_bars if min_bars is not None else _MIN_BARS
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of[:10])
    trade_date = as_of or latest_trade_date(market=market)
    if trade_date is None:
        return {"ok": 0, "rows": 0, "trade_date": None}

    history = get_history_window(end=trade_date, calendar_days=calendar_days, market=market)
    if limit and not history.empty:
        codes = sorted(history["ts_code"].unique())[:limit]
        history = history[history["ts_code"].isin(codes)]

    feat_df = scan_universe_features(history, min_bars=min_bars)
    if feat_df.empty:
        logger.warning("[WARN] daily_features 无有效指标（K 线不足）")
        return {"ok": 0, "rows": 0, "trade_date": str(trade_date)}

    rows: list[dict] = []
    for _, row in feat_df.iterrows():
        code = str(row["ts_code"])
        rows.append(_row_from_series(code, trade_date, row, market=market))

    n = upsert_daily_features(rows)
    stats = {"ok": len(rows), "rows": n, "trade_date": str(trade_date), "min_bars": min_bars}
    logger.info("[INFO] daily_features 快照完成: %s", stats)
    return stats


def count_features_coverage(as_of: date | None = None) -> tuple[int, int]:
    """返回 (as_of 日特征行数, 当日日线行数)。"""
    init_db()
    td = as_of or latest_trade_date()
    if td is None:
        return 0, 0
    with SessionLocal() as session:
        feat_n = session.execute(
            select(DailyFeature.ts_code).where(DailyFeature.trade_date == td)
        ).all()
        bar_n = session.execute(
            text("SELECT COUNT(*) FROM daily_prices WHERE trade_date = :d"),
            {"d": td},
        ).scalar_one()
    return len(feat_n), int(bar_n or 0)
