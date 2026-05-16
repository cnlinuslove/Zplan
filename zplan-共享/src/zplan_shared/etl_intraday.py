from __future__ import annotations

import logging
import time

import akshare as ak
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from zplan_shared.config import (
    AKSHARE_RATE_LIMIT_SECONDS,
    INTRADAY_COARSE_PERIOD,
    INTRADAY_FINE_CALENDAR_DAYS,
    INTRADAY_FINE_PERIOD,
    RECENT_INTRADAY_CALENDAR_DAYS,
)
from zplan_shared.etl_akshare import configure_akshare_http
from zplan_shared.intraday_store import upsert_intraday_parquet

logger = logging.getLogger(__name__)


@retry(wait=wait_exponential(multiplier=1, min=2, max=15), stop=stop_after_attempt(3))
def fetch_intraday_em(
    symbol: str,
    *,
    period: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    return ak.stock_zh_a_hist_min_em(
        symbol=symbol,
        start_date=start.strftime("%Y-%m-%d 09:30:00"),
        end_date=end.strftime("%Y-%m-%d 15:00:00"),
        period=period,
        adjust="qfq",
    )


def sync_intraday_for_symbol(symbol: str) -> dict[str, int]:
    """近端分时：细粒度 1min（约 5 日）+ 粗粒度 5min（约 14 日）。"""
    configure_akshare_http()
    end = pd.Timestamp.now().normalize() + pd.Timedelta(hours=15)
    windows = [
        (INTRADAY_FINE_PERIOD, end - pd.Timedelta(days=INTRADAY_FINE_CALENDAR_DAYS)),
        (INTRADAY_COARSE_PERIOD, end - pd.Timedelta(days=RECENT_INTRADAY_CALENDAR_DAYS)),
    ]
    written: dict[str, int] = {}
    for period, start in windows:
        try:
            df = fetch_intraday_em(symbol, period=period, start=start, end=end)
            n = upsert_intraday_parquet(symbol, period, df)
            written[period] = n
            logger.info("[INFO] %s 分时 period=%s 写入 %s 条", symbol, period, n)
        except Exception as exc:
            logger.warning("[WARN] %s 分时 period=%s 失败: %s", symbol, period, exc)
            written[period] = 0
        time.sleep(AKSHARE_RATE_LIMIT_SECONDS)
    return written


def sync_intraday_universe(symbols: list[str]) -> None:
    for idx, symbol in enumerate(symbols, 1):
        logger.info("[INFO] [%s/%s] 分时 %s", idx, len(symbols), symbol)
        sync_intraday_for_symbol(symbol)
