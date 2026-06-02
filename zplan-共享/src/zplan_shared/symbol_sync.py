"""按股票列表同步行情数据（供持仓订阅每日任务）。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from zplan_shared.etl_akshare import (
    configure_akshare_http,
    fetch_daily_bars,
    get_latest_trade_date,
    throttle,
    upsert_daily_prices,
)
from zplan_shared.etl_intraday import sync_intraday_for_symbol
from zplan_shared.http_client import configure_akshare_http as _configure_http
from zplan_shared.models import init_db

logger = logging.getLogger(__name__)


def sync_symbol_market_data(ts_code: str, *, include_intraday: bool = True) -> dict[str, Any]:
    """单票：日线增量 + 可选近端分时。"""
    init_db()
    configure_akshare_http()
    symbol = str(ts_code).strip().zfill(6)
    out: dict[str, Any] = {"ts_code": symbol, "daily_rows": 0, "intraday": {}}

    latest_date = get_latest_trade_date(symbol)
    start_date = None
    if latest_date:
        start_date = (latest_date + pd.Timedelta(days=1)).strftime("%Y%m%d")
    else:
        start_date = (pd.Timestamp.today() - pd.Timedelta(days=400)).strftime("%Y%m%d")

    try:
        price_df, source = fetch_daily_bars(symbol=symbol, start_date=start_date)
        out["daily_rows"] = upsert_daily_prices(symbol, price_df, source=source)
        out["daily_source"] = source
    except Exception as exc:
        out["daily_error"] = str(exc)
        logger.warning("[WARN] %s 日线同步失败: %s", symbol, exc)
    finally:
        throttle()

    if include_intraday:
        try:
            out["intraday"] = sync_intraday_for_symbol(symbol)
        except Exception as exc:
            out["intraday_error"] = str(exc)
            logger.warning("[WARN] %s 分时同步失败: %s", symbol, exc)

    return out


def sync_symbols_market_data(
    symbols: list[str],
    *,
    include_intraday: bool = True,
) -> dict[str, Any]:
    """批量同步持仓标的行情。"""
    results: list[dict[str, Any]] = []
    ok = fail = 0
    for idx, code in enumerate(symbols, 1):
        logger.info("[INFO] [%s/%s] 同步行情 %s", idx, len(symbols), code)
        row = sync_symbol_market_data(code, include_intraday=include_intraday)
        results.append(row)
        if row.get("daily_error") and not row.get("daily_rows"):
            fail += 1
        else:
            ok += 1
    return {"ok": ok, "fail": fail, "symbols": results}
