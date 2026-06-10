"""大盘指数日线 ETL — AkShare 拉取 + SQLite upsert。

7 大核心指数：
  - 000001  上证指数
  - 399001  深证成指
  - 399006  创业板指
  - 000688  科创50
  - 000300  沪深300
  - 000905  中证500
  - 000852  中证1000

用法：
  python -m zplan_shared.etl_index          # 增量同步（近 10 日）
  python -m zplan_shared.etl_index --full   # 全量回填（不限日期）
  python -m zplan_shared.etl_index --catch-up  # 补缺（拉取库内缺的交易日）
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import akshare as ak
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.config import AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS, AKSHARE_FAIL_CIRCUIT_THRESHOLD
from zplan_shared.http_client import configure_akshare_http, throttle
from zplan_shared.models import DailyIndex, SessionLocal, init_db

logger = logging.getLogger(__name__)

_INDEX_MAP: dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
}
# 东财 → 腾讯 symbol 的兜底映射
_INDEX_TX_MAP: dict[str, str] = {
    "000001": "sh000001",
    "399001": "sz399001",
    "399006": "sz399006",
    "000688": "sh000688",
    "000300": "sh000300",
    "000905": "sh000905",
    "000852": "sh000852",
}

# ── 外盘指数（新浪接口，不需要腾讯兜底）──────
_GLOBAL_INDEX_MAP: dict[str, str] = {
    ".INX": "标普500",
    ".IXIC": "纳斯达克",
    ".DJI": "道琼斯",
    "HSI": "恒生指数",
}

A_INDEX_CODES = list(_INDEX_MAP.keys())
GLOBAL_INDEX_CODES = list(_GLOBAL_INDEX_MAP.keys())
ALL_INDEX_CODES = A_INDEX_CODES + GLOBAL_INDEX_CODES
INDEX_SOURCE = "akshare_em"
GLOBAL_INDEX_SOURCE = "akshare_sina"
_UPSERT_BATCH_SIZE = 50


@dataclass
class _CircuitBreaker:
    threshold: int = AKSHARE_FAIL_CIRCUIT_THRESHOLD
    sleep_seconds: int = AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS
    failures: int = 0

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            logger.warning("连续失败 %s 次，触发熔断休眠 %s 秒", self.failures, self.sleep_seconds)
            time.sleep(self.sleep_seconds)
            self.failures = 0


_circuit = _CircuitBreaker()


def _fetch_index_hist_em(index_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """东财 index_zh_a_hist 拉取单指数日线。"""
    try:
        configure_akshare_http()
        raw = ak.index_zh_a_hist(symbol=index_code, period="daily", start_date=start_date, end_date=end_date)
        if raw is None or raw.empty:
            return None
        df = raw.copy()
        # 标准化列名
        col_map = {
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_chg",
            "换手率": "turnover_rate",
        }
        existing = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=existing)
        # 确保 trade_date 是 date 类型
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        _circuit.record_success()
        return df
    except Exception as exc:
        logger.warning("东财拉取指数 %s 失败: %s", index_code, exc)
        _circuit.record_failure()
        return None


def _fetch_index_hist_tx(index_code: str) -> pd.DataFrame | None:
    """腾讯 stock_zh_index_daily_tx 兜底（全量下载，较慢）。"""
    tx_sym = _INDEX_TX_MAP.get(index_code)
    if not tx_sym:
        return None
    try:
        configure_akshare_http()
        raw = ak.stock_zh_index_daily_tx(symbol=tx_sym)
        if raw is None or raw.empty:
            return None
        df = raw.copy()
        col_map = {
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        }
        existing = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=existing)
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        _circuit.record_success()
        return df
    except Exception as exc:
        logger.warning("腾讯拉取指数 %s 失败: %s", index_code, exc)
        _circuit.record_failure()
        return None


def _fetch_global_index_sina(symbol: str) -> pd.DataFrame | None:
    """新浪接口拉取美股/港股指数日线。"""
    try:
        configure_akshare_http()
        if symbol == "HSI":
            raw = ak.stock_hk_index_daily_sina(symbol="HSI")
        else:
            raw = ak.index_us_stock_sina(symbol=symbol)
        if raw is None or raw.empty:
            return None
        df = raw.copy()
        col_map = {
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
        }
        existing = {k: v for k, v in col_map.items() if k in df.columns}
        df = df.rename(columns=existing)
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        _circuit.record_success()
        return df
    except Exception as exc:
        logger.warning("新浪拉取外盘指数 %s 失败: %s", symbol, exc)
        _circuit.record_failure()
        return None


def fetch_index_bars(index_code: str, *, start_date: str | None = None,
                     end_date: str | None = None) -> pd.DataFrame:
    """拉取单个指数日线。

    美股/港股 → 新浪接口；A 股指数 → 东财优先 → 腾讯兜底。
    """
    # 外盘指数走新浪
    if index_code in _GLOBAL_INDEX_MAP:
        df = _fetch_global_index_sina(index_code)
        if df is not None and not df.empty:
            if start_date:
                s_dt = pd.to_datetime(start_date, format="%Y%m%d").date()
                df = df[df["trade_date"] >= s_dt]
            if end_date:
                e_dt = pd.to_datetime(end_date, format="%Y%m%d").date()
                df = df[df["trade_date"] <= e_dt]
        return df if df is not None and not df.empty else pd.DataFrame()

    start_s = start_date or "19900101"
    end_s = end_date or date.today().strftime("%Y%m%d")

    df = _fetch_index_hist_em(index_code, start_s, end_s)
    if df is not None and not df.empty:
        return df

    logger.info("东财失败，尝试腾讯兜底: %s", index_code)
    df = _fetch_index_hist_tx(index_code)
    if df is not None and not df.empty:
        if start_date:
            mask = df["trade_date"] >= pd.to_datetime(start_date, format="%Y%m%d").date()
            df = df[mask]
        if end_date:
            mask = df["trade_date"] <= pd.to_datetime(end_date, format="%Y%m%d").date()
            df = df[mask]
    return df if df is not None and not df.empty else pd.DataFrame()


def _to_date(val: object) -> date | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    parsed = pd.to_datetime(val, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _float_field(row: pd.Series, key: str) -> float | None:
    val = row.get(key)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


def upsert_index_bars(index_code: str, df: pd.DataFrame) -> int:
    """将单指数日线 upsert 入库。"""
    if df.empty:
        return 0

    index_name = _INDEX_MAP.get(index_code) or _GLOBAL_INDEX_MAP.get(index_code, index_code)
    ingested_at = datetime.utcnow()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        td = _to_date(row.get("trade_date"))
        if td is None:
            continue
        rows.append({
            "index_code": index_code,
            "index_name": index_name,
            "trade_date": td,
            "open": _float_field(row, "open"),
            "high": _float_field(row, "high"),
            "low": _float_field(row, "low"),
            "close": _float_field(row, "close"),
            "volume": _float_field(row, "volume"),
            "amount": _float_field(row, "amount"),
            "pct_chg": _float_field(row, "pct_chg"),
            "turnover_rate": _float_field(row, "turnover_rate"),
            "source": INDEX_SOURCE,
            "ingested_at": ingested_at,
        })

    if not rows:
        return 0

    total = 0
    with SessionLocal() as session:
        for i in range(0, len(rows), _UPSERT_BATCH_SIZE):
            chunk = rows[i : i + _UPSERT_BATCH_SIZE]
            stmt = insert(DailyIndex).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["index_code", "trade_date"],
                set_={
                    "index_name": stmt.excluded.index_name,
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                    "pct_chg": stmt.excluded.pct_chg,
                    "turnover_rate": stmt.excluded.turnover_rate,
                    "source": stmt.excluded.source,
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
            session.execute(stmt)
            total += len(chunk)
        session.commit()
    return total


def _latest_index_date(index_code: str) -> date | None:
    """库内某指数最新交易日。"""
    with SessionLocal() as session:
        r = session.execute(
            select(func.max(DailyIndex.trade_date)).where(DailyIndex.index_code == index_code)
        ).scalar()
    if r is None:
        return None
    return r if isinstance(r, date) else r.date() if isinstance(r, datetime) else date.fromisoformat(str(r))


def sync_index_incremental(index_code: str, *, lookback_days: int = 10) -> int:
    """增量同步：拉取最近 N 个自然日数据并 upsert（去重自动跳过已有）。"""
    end = date.today()
    existing = _latest_index_date(index_code)
    if existing and existing >= end:
        logger.info("指数 %s 已是最新（%s），跳过", index_code, existing)
        return 0
    start = (existing or (end - timedelta(days=lookback_days * 2)))
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    logger.info("同步指数 %s: %s → %s", index_code, start_s, end_s)
    df = fetch_index_bars(index_code, start_date=start_s, end_date=end_s)
    if df.empty:
        logger.warning("指数 %s 无数据", index_code)
        return 0
    n = upsert_index_bars(index_code, df)
    logger.info("指数 %s upsert %s 行", index_code, n)
    return n


def sync_all_indices(*, lookback_days: int = 10, catch_up: bool = False,
                     include_global: bool = True) -> dict[str, int]:
    """同步全部指数（A 股 7 大 + 外盘 4 个）。

    Args:
        lookback_days: 增量回溯天数（仅 incremental 模式）
        catch_up: True=补缺模式（拉取库内缺失的交易日），否则增量模式
        include_global: 是否同步美股/港股指数

    Returns:
        {index_code: rows_upserted}
    """
    init_db()
    codes = ALL_INDEX_CODES if include_global else A_INDEX_CODES
    results: dict[str, int] = {}
    for i, code in enumerate(codes):
        if i > 0:
            throttle(1.0 if code in _GLOBAL_INDEX_MAP else 1.5)
        try:
            if catch_up:
                # 全量拉取（不限日期），upsert 自动跳过已有
                df = fetch_index_bars(code)
                n = upsert_index_bars(code, df)
            else:
                n = sync_index_incremental(code, lookback_days=lookback_days)
            results[code] = n
        except Exception as exc:
            logger.error("指数 %s 同步失败: %s", code, exc)
            results[code] = -1
    return results


def index_coverage_summary() -> dict[str, Any]:
    """指数数据覆盖情况（用于管道状态播报）。"""
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(
                DailyIndex.index_code,
                DailyIndex.index_name,
                func.min(DailyIndex.trade_date).label("first_date"),
                func.max(DailyIndex.trade_date).label("last_date"),
                func.count(DailyIndex.id).label("row_count"),
            ).group_by(DailyIndex.index_code)
        ).all()

    summary: dict[str, Any] = {}
    for r in rows:
        summary[r[0]] = {
            "name": r[1],
            "first_date": str(r[2]) if r[2] else None,
            "last_date": str(r[3]) if r[3] else None,
            "row_count": r[4],
        }
    return summary


# ── CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    catch_up = "--catch-up" in sys.argv or "--full" in sys.argv
    mode = "补缺" if catch_up else "增量"
    logger.info("开始 %s 同步 7 大指数...", mode)
    results = sync_all_indices(lookback_days=10, catch_up=catch_up)
    total = sum(max(0, v) for v in results.values())
    errors = sum(1 for v in results.values() if v < 0)
    logger.info("完成: %s 行, %s 错误", total, errors)
    for code, n in results.items():
        name = _INDEX_MAP.get(code, code)
        logger.info("  %s %s: %s 行", code, name, n)

    # 打印覆盖总结
    summary = index_coverage_summary()
    logger.info("数据覆盖:")
    for code, info in summary.items():
        logger.info("  %s: %s → %s (%s 行)", code, info["first_date"], info["last_date"], info["row_count"])
