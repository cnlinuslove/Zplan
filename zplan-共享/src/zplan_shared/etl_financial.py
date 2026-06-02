"""Phase D：季报财务指标 ETL（新浪 ``stock_financial_abstract``）。"""
from __future__ import annotations

import logging
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.http_client import configure_akshare_http, throttle
from zplan_shared.models import FinancialIndicator, SessionLocal, StockList, init_db

logger = logging.getLogger(__name__)

FINANCIAL_SOURCE = "akshare_sina"
_QUARTER_LIMIT = 8
_METRIC_MAP = {
    "归母净利润": "net_profit",
    "营业总收入": "revenue",
    "净资产收益率": "roe",
}


def _parse_report_date(col: str) -> date | None:
    col = str(col).strip()
    if len(col) == 8 and col.isdigit():
        return pd.to_datetime(col, format="%Y%m%d").date()
    return None


def _fetch_financial_rows(symbol: str) -> list[dict]:
    configure_akshare_http()
    raw = ak.stock_financial_abstract(symbol=symbol)
    if raw.empty:
        return []
    date_cols = [c for c in raw.columns if c not in ("选项", "指标")]
    date_cols = sorted(
        [c for c in date_cols if _parse_report_date(c)],
        key=lambda x: x,
        reverse=True,
    )[:_QUARTER_LIMIT]

    by_date: dict[date, dict] = {}
    for col in date_cols:
        rd = _parse_report_date(col)
        if rd is None:
            continue
        by_date[rd] = {
            "ts_code": symbol,
            "report_date": rd,
            "pe_ttm": None,
            "pb": None,
            "revenue": None,
            "net_profit": None,
            "roe": None,
            "source": FINANCIAL_SOURCE,
        }

    for _, row in raw.iterrows():
        metric = str(row.get("指标", "")).strip()
        field = _METRIC_MAP.get(metric)
        if not field:
            continue
        for col in date_cols:
            rd = _parse_report_date(col)
            if rd is None or rd not in by_date:
                continue
            val = row.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            try:
                by_date[rd][field] = float(val)
            except (TypeError, ValueError):
                continue

    return list(by_date.values())


def upsert_financial_indicators(rows: list[dict]) -> int:
    if not rows:
        return 0
    ingested_at = datetime.utcnow()
    for r in rows:
        r["ingested_at"] = ingested_at
    with SessionLocal() as session:
        for chunk_start in range(0, len(rows), 50):
            chunk = rows[chunk_start : chunk_start + 50]
            stmt = insert(FinancialIndicator).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "report_date"],
                set_={
                    "pe_ttm": stmt.excluded.pe_ttm,
                    "pb": stmt.excluded.pb,
                    "revenue": stmt.excluded.revenue,
                    "net_profit": stmt.excluded.net_profit,
                    "roe": stmt.excluded.roe,
                    "source": stmt.excluded.source,
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
            session.execute(stmt)
        session.commit()
    return len(rows)


def run_financial_indicators_update(*, limit: int | None = None) -> dict[str, int]:
    init_db()
    with SessionLocal() as session:
        symbols = [r[0] for r in session.execute(select(StockList.ts_code)).all()]
    if limit:
        symbols = symbols[:limit]

    stats = {"ok": 0, "fail": 0, "rows": 0}
    all_rows: list[dict] = []
    for idx, symbol in enumerate(symbols, 1):
        if idx % 100 == 0:
            logger.info("[INFO] financial [%s/%s]", idx, len(symbols))
        try:
            rows = _fetch_financial_rows(symbol)
            if rows:
                all_rows.extend(rows)
                stats["ok"] += 1
            else:
                stats["fail"] += 1
        except Exception as exc:
            stats["fail"] += 1
            logger.warning("[WARN] %s 财报失败: %s", symbol, exc)
        throttle()

    if all_rows:
        stats["rows"] = upsert_financial_indicators(all_rows)
    logger.info("[INFO] financial_indicators 完成: %s", stats)
    return stats
