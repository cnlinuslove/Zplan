from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert
from tenacity import retry, stop_after_attempt, wait_exponential

from zplan_shared.config import (
    AKSHARE_ALLOW_TX_FALLBACK,
    AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS,
    AKSHARE_FAIL_CIRCUIT_THRESHOLD,
    AKSHARE_RATE_LIMIT_SECONDS,
    DAILY_BOOTSTRAP_CALENDAR_DAYS,
)
from zplan_shared.http_client import configure_akshare_http, throttle
from zplan_shared.market import DEFAULT_ADJUST_TYPE
from zplan_shared.models import DailyPrice, SessionLocal, StockList, init_db


logger = logging.getLogger(__name__)

AKSHARE_SOURCE = "akshare_em"
AKSHARE_TX_SOURCE = "akshare_tx"
# SQLite 单条 INSERT 变量上限约 999；每行 ~15 列，批次需足够小
_UPSERT_BATCH_SIZE = 50


@dataclass
class CircuitBreaker:
    threshold: int = AKSHARE_FAIL_CIRCUIT_THRESHOLD
    sleep_seconds: int = AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS
    failures: int = 0

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            logger.warning(
                "连续失败 %s 次，触发熔断休眠 %s 秒。",
                self.failures,
                self.sleep_seconds,
            )
            time.sleep(self.sleep_seconds)
            self.failures = 0


circuit_breaker = CircuitBreaker()


def clear_demo_market_data() -> int:
    """删除 ``demo_seed`` 演示行情，便于首次拉取真实全历史。"""
    init_db()
    with SessionLocal() as session:
        result = session.execute(delete(DailyPrice).where(DailyPrice.source == "demo_seed"))
        session.commit()
        return int(result.rowcount or 0)


def _to_date(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _float_field(row: pd.Series, key: str) -> float | None:
    val = row.get(key)
    if val is None or pd.isna(val):
        return None
    return float(val)


def _ymd_to_iso(ymd: str) -> str:
    if len(ymd) == 8 and ymd.isdigit():
        return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return ymd


def ts_code_to_tx_symbol(ts_code: str) -> str:
    code = ts_code.strip()
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _normalize_hist_to_em_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "日期" in df.columns:
        return df
    out = df.copy()
    mapping = {
        "date": "日期",
        "open": "开盘",
        "close": "收盘",
        "high": "最高",
        "low": "最低",
        "amount": "成交额",
    }
    for src, dst in mapping.items():
        if src in out.columns:
            out[dst] = out[src]
    return out


@retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(5))
def fetch_stock_list() -> pd.DataFrame:
    return ak.stock_info_a_code_name()


@retry(wait=wait_exponential(multiplier=2, min=4, max=60), stop=stop_after_attempt(6))
def _fetch_stock_daily_hist_em(symbol: str, start_date: str | None = None) -> pd.DataFrame:
    kwargs = {"symbol": symbol, "adjust": "qfq"}
    if start_date:
        kwargs["start_date"] = start_date
    return ak.stock_zh_a_hist(**kwargs)


def _fetch_stock_daily_hist_tx(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
) -> pd.DataFrame:
    end = end_date or pd.Timestamp.today().strftime("%Y-%m-%d")
    df = ak.stock_zh_a_hist_tx(
        symbol=ts_code_to_tx_symbol(symbol),
        start_date=_ymd_to_iso(start_date),
        end_date=end,
        adjust="qfq",
    )
    return _normalize_hist_to_em_columns(df)


def fetch_stock_daily_hist_em_only(
    symbol: str,
    start_date: str | None = None,
) -> pd.DataFrame:
    """仅东财日线；失败抛错，不降级腾讯。"""
    configure_akshare_http()
    return _fetch_stock_daily_hist_em(symbol, start_date)


def fetch_stock_daily_hist(
    symbol: str,
    start_date: str | None = None,
    *,
    prefer_tx: bool = False,
    em_only: bool = False,
) -> tuple[pd.DataFrame, str]:
    """拉取日线。``em_only=True`` 或默认关闭 ``AKSHARE_ALLOW_TX_FALLBACK`` 时仅用东财。"""
    configure_akshare_http()
    if prefer_tx and not em_only:
        if not start_date:
            start_date = (pd.Timestamp.today() - pd.Timedelta(days=365)).strftime("%Y%m%d")
        return _fetch_stock_daily_hist_tx(symbol, start_date), AKSHARE_TX_SOURCE

    try:
        return _fetch_stock_daily_hist_em(symbol, start_date), AKSHARE_SOURCE
    except Exception as exc:
        if em_only or not AKSHARE_ALLOW_TX_FALLBACK:
            raise
        logger.warning("东财 %s 拉取失败，改用腾讯（设 AKSHARE_ALLOW_TX_FALLBACK=false 可禁用）: %s", symbol, exc)
        if not start_date:
            start_date = (pd.Timestamp.today() - pd.Timedelta(days=365)).strftime("%Y%m%d")
        return _fetch_stock_daily_hist_tx(symbol, start_date), AKSHARE_TX_SOURCE


def upsert_stock_list(df: pd.DataFrame) -> int:
    if df.empty:
        return 0

    rows = []
    for _, row in df.iterrows():
        ts_code = str(row.get("code", "")).strip()
        name = str(row.get("name", "")).strip()
        if not ts_code or not name:
            continue
        rows.append(
            {
                "ts_code": ts_code,
                "name": name,
                "industry": None,
                "listing_date": None,
            }
        )

    if not rows:
        return 0

    with SessionLocal() as session:
        stmt = insert(StockList).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[StockList.ts_code],
            set_={
                "name": stmt.excluded.name,
                "industry": stmt.excluded.industry,
                "listing_date": stmt.excluded.listing_date,
            },
        )
        session.execute(stmt)
        session.commit()

    return len(rows)


def get_latest_trade_date(ts_code: str) -> date | None:
    with SessionLocal() as session:
        result = session.execute(
            select(func.max(DailyPrice.trade_date)).where(DailyPrice.ts_code == ts_code)
        ).scalar_one_or_none()
    return result


def upsert_daily_prices(
    ts_code: str,
    df: pd.DataFrame,
    *,
    adjust_type: str = DEFAULT_ADJUST_TYPE,
    source: str = AKSHARE_SOURCE,
) -> int:
    if df.empty:
        return 0

    ingested_at = datetime.utcnow()
    rows = []
    for _, row in df.iterrows():
        trade_date = _to_date(row.get("日期"))
        if trade_date is None:
            continue
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": _float_field(row, "开盘"),
                "high": _float_field(row, "最高"),
                "low": _float_field(row, "最低"),
                "close": _float_field(row, "收盘"),
                "volume": _float_field(row, "成交量"),
                "amount": _float_field(row, "成交额"),
                "amplitude": _float_field(row, "振幅"),
                "pct_chg": _float_field(row, "涨跌幅"),
                "change_amt": _float_field(row, "涨跌额"),
                "turnover_rate": _float_field(row, "换手率"),
                "adjust_type": adjust_type,
                "source": source,
                "ingested_at": ingested_at,
            }
        )

    if not rows:
        return 0

    total = 0
    with SessionLocal() as session:
        for i in range(0, len(rows), _UPSERT_BATCH_SIZE):
            chunk = rows[i : i + _UPSERT_BATCH_SIZE]
            stmt = insert(DailyPrice).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "trade_date"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                    "amplitude": stmt.excluded.amplitude,
                    "pct_chg": stmt.excluded.pct_chg,
                    "change_amt": stmt.excluded.change_amt,
                    "turnover_rate": stmt.excluded.turnover_rate,
                    "adjust_type": stmt.excluded.adjust_type,
                    "source": stmt.excluded.source,
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
            session.execute(stmt)
            total += len(chunk)
        session.commit()
    return total


def run_incremental_update(
    limit: int | None = None,
    *,
    recent_days: int | None = None,
    prefer_tx: bool = False,
) -> None:
    configure_akshare_http()
    init_db()
    logger.info("[INFO] 开始拉取股票列表...")

    try:
        stock_df = fetch_stock_list()
        upsert_count = upsert_stock_list(stock_df)
        circuit_breaker.record_success()
        logger.info("[INFO] 股票列表更新完成，记录数: %s", upsert_count)
    except Exception as exc:
        circuit_breaker.record_failure()
        logger.warning("[WARN] 股票列表拉取失败: %s", exc)
        raise

    symbols = stock_df["code"].astype(str).tolist()
    if limit:
        symbols = symbols[:limit]

    for idx, symbol in enumerate(symbols, 1):
        logger.info("[INFO] [%s/%s] 更新 %s", idx, len(symbols), symbol)
        latest_date = get_latest_trade_date(symbol)
        start_date = None
        if latest_date:
            start_date = (latest_date + pd.Timedelta(days=1)).strftime("%Y%m%d")
        elif recent_days:
            start_date = (pd.Timestamp.today() - pd.Timedelta(days=recent_days)).strftime(
                "%Y%m%d"
            )

        try:
            price_df, source = fetch_stock_daily_hist(
                symbol=symbol, start_date=start_date, prefer_tx=prefer_tx
            )
            upsert_rows = upsert_daily_prices(symbol, price_df, source=source)
            circuit_breaker.record_success()
            logger.info("[INFO] %s 日线更新 %s 条", symbol, upsert_rows)
        except Exception as exc:
            circuit_breaker.record_failure()
            logger.warning("[WARN] %s 拉取失败: %s", symbol, exc)
        finally:
            throttle()

    logger.info("[INFO] 增量更新完成。")


def run_a1_update(
    limit: int | None = None,
    *,
    skip_intraday: bool = False,
    clear_demo: bool = False,
    backfill_em: bool = False,
) -> dict[str, int]:
    """Phase A.1：全市场东财日线 + 近两周分时（1min×5日 + 5min×14日）。仅东财，不降级腾讯。"""
    from zplan_shared.etl_intraday import sync_intraday_universe

    stats = {"daily_ok": 0, "daily_fail": 0, "daily_rows": 0, "intraday_ok": 0, "intraday_fail": 0}

    if clear_demo:
        n = clear_demo_market_data()
        if n:
            logger.info("已清除演示行情 %s 条", n)

    configure_akshare_http()
    init_db()
    logger.info("[INFO] A.1 开始：股票列表 + 日线(EM) + 近端分时")

    try:
        stock_df = fetch_stock_list()
        upsert_count = upsert_stock_list(stock_df)
        circuit_breaker.record_success()
        logger.info("[INFO] 股票列表更新完成，记录数: %s", upsert_count)
    except Exception as exc:
        circuit_breaker.record_failure()
        logger.warning("[WARN] 股票列表拉取失败: %s", exc)
        raise

    symbols = stock_df["code"].astype(str).tolist()
    if limit:
        symbols = symbols[:limit]

    for idx, symbol in enumerate(symbols, 1):
        logger.info("[INFO] [%s/%s] 日线 %s", idx, len(symbols), symbol)
        latest_date = get_latest_trade_date(symbol)
        start_date = None
        if latest_date:
            start_date = (latest_date + pd.Timedelta(days=1)).strftime("%Y%m%d")
        else:
            start_date = (
                pd.Timestamp.today() - pd.Timedelta(days=DAILY_BOOTSTRAP_CALENDAR_DAYS)
            ).strftime("%Y%m%d")

        try:
            price_df, source = fetch_stock_daily_hist(
                symbol=symbol, start_date=start_date, prefer_tx=False
            )
            upsert_rows = upsert_daily_prices(symbol, price_df, source=source)
            circuit_breaker.record_success()
            logger.info("[INFO] %s 日线更新 %s 条", symbol, upsert_rows)
        except Exception as exc:
            circuit_breaker.record_failure()
            logger.warning("[WARN] %s 日线失败: %s", symbol, exc)
        finally:
            time.sleep(AKSHARE_RATE_LIMIT_SECONDS)

    if not skip_intraday:
        sync_intraday_universe(symbols)

    logger.info("[INFO] A.1 完成。")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_incremental_update(limit=5)
