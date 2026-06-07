"""港股 (HKEX) 行情 ETL — AkShare ``stock_hk_*`` 系列接口。

复用 ``etl_akshare.py`` 的底层写入函数（``upsert_daily_prices`` 等），仅新增
港股特定的数据拉取、代码列表获取逻辑。
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from tenacity import retry, stop_after_attempt, wait_exponential

from zplan_shared.config import (
    HK_DAILY_BOOTSTRAP_CALENDAR_DAYS,
    HK_DAILY_CHUNK_DAYS,
)
from zplan_shared.data_sources import (
    HK_DAILY_SOURCE_EM,
    HK_DAILY_SOURCE_SINA,
    hk_daily_provider,
    hk_daily_source_tag,
    hk_daily_provider_label,
)
from zplan_shared.etl_akshare import (
    _enrich_daily_derived_fields,
    _float_field,
    _to_date,
    _UPSERT_BATCH_SIZE,
    CircuitBreaker,
    throttle as _throttle,
    upsert_daily_prices as _upsert_daily_prices_a,
    upsert_stock_list as _upsert_stock_list_a,
)
from zplan_shared.http_client import configure_akshare_http
from zplan_shared.market import DEFAULT_ADJUST_TYPE

logger = logging.getLogger(__name__)

HK_MARKET = "hk"
_HK_DAILY_HIST_KWARGS = {"period": "daily", "adjust": "qfq"}


def throttle(seconds: float | None = None) -> None:
    """港股限流间隔。"""
    base = float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "3"))
    _throttle(seconds if seconds is not None else base)


def _configure_hk_http() -> None:
    """港股 ETL HTTP 配置：优先走代理（东财 CDN 在某些网络下直连不通）。"""
    # 让东财域名走代理而非直连（与 curl 行为一致）
    if os.getenv("AKSHARE_EASTMONEY_DIRECT") is None:
        os.environ["AKSHARE_EASTMONEY_DIRECT"] = "false"
    configure_akshare_http()


# ── 港股代码列表 ──────────────────────────────────────────────────


@retry(wait=wait_exponential(multiplier=2, min=4, max=45), stop=stop_after_attempt(3))
def _fetch_stock_list_hk_em() -> pd.DataFrame:
    """东方财富港股实时行情 → 代码 + 名称列表。"""
    _configure_hk_http()
    spot = ak.stock_hk_spot_em()
    out = spot[["代码", "名称"]].copy()
    out.columns = ["code", "name"]
    out["code"] = out["code"].astype(str).str.strip().str.zfill(5)
    return out.dropna(subset=["code", "name"])


def _fetch_stock_list_hk_sina() -> pd.DataFrame:
    """新浪港股实时行情 → 代码 + 名称列表（约 2771 只，境外可访问）。"""
    _configure_hk_http()
    spot = ak.stock_hk_spot()
    out = spot[["代码", "中文名称"]].copy()
    out.columns = ["code", "name"]
    out["code"] = out["code"].astype(str).str.strip().str.zfill(5)
    return out.dropna(subset=["code", "name"])


def _fetch_stock_list_hk() -> pd.DataFrame:
    """按 ``HK_DAILY_PROVIDER`` 选择港股列表源。"""
    provider = hk_daily_provider()
    if provider == "em":
        return _fetch_stock_list_hk_em()
    logger.info("[INFO] 港股列表使用 Sina 源（境外兼容）")
    return _fetch_stock_list_hk_sina()


def ensure_hk_stock_list(*, min_cached: int = 500) -> pd.DataFrame:
    """获取港股代码列表并写入 ``stock_list``（market='hk'）。"""
    _configure_hk_http()
    provider_label = hk_daily_provider_label()

    try:
        df = _fetch_stock_list_hk()
        logger.info("[INFO] 港股列表来自 %s，%s 只", provider_label, len(df))
        _upsert_hk_stock_list(df)
        return df
    except Exception as exc:
        logger.warning("[WARN] 东财港股列表失败: %s，尝试库内缓存", exc)
        from zplan_shared.models import SessionLocal, StockList

        with SessionLocal() as session:
            rows = session.execute(
                select(StockList.ts_code, StockList.name).where(
                    StockList.market == HK_MARKET
                )
            ).all()
        cached = pd.DataFrame(
            [{"code": r[0], "name": r[1]} for r in rows]
        )
        if len(cached) >= min_cached:
            logger.info("[INFO] 使用库内港股列表缓存 %s 只", len(cached))
            return cached
        raise


def _upsert_hk_stock_list(df: pd.DataFrame) -> int:
    """港股列表写入 ``stock_list``，带 ``market='hk'``。"""
    if df.empty:
        return 0
    from zplan_shared.models import SessionLocal, StockList

    rows = []
    for _, row in df.iterrows():
        ts_code = str(row.get("code", "")).strip()
        name = str(row.get("name", "")).strip()
        if not ts_code or not name:
            continue
        rows.append({
            "ts_code": ts_code,
            "name": name,
            "industry": None,
            "listing_date": None,
            "market": HK_MARKET,
        })

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
                "market": stmt.excluded.market,
            },
        )
        session.execute(stmt)
        session.commit()
    return len(rows)


# ── 港股日线拉取 ──────────────────────────────────────────────────


def _hk_start_end(start_date: str | None) -> tuple[str, str]:
    """计算港股日线请求起止日期（YYYYMMDD）。"""
    end = pd.Timestamp.today().normalize()
    if start_date:
        start = pd.to_datetime(start_date, format="%Y%m%d")
    else:
        start = end - pd.Timedelta(days=HK_DAILY_BOOTSTRAP_CALENDAR_DAYS)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


@retry(wait=wait_exponential(multiplier=2, min=3, max=30), stop=stop_after_attempt(3))
def _fetch_stock_daily_hist_hk_chunk(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """港股日线 — 单段请求。"""
    return ak.stock_hk_hist(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        timeout=60,
        **_HK_DAILY_HIST_KWARGS,
    )


def _normalize_hk_sina_to_em_columns(df: pd.DataFrame) -> pd.DataFrame:
    """新浪港股日线 → 东财统一列名（中文 11 列）。

    Sina 返回: date, open, high, low, close, volume, amount
    缺失字段由 OHLCV 递推：pct_chg, change_amt, amplitude, turnover_rate
    """
    if df.empty:
        return df
    out = df.copy()
    # 列名映射
    col_map = {
        "date": "日期", "open": "开盘", "high": "最高",
        "low": "最低", "close": "收盘", "volume": "成交量", "amount": "成交额",
    }
    out.rename(columns={k: v for k, v in col_map.items() if k in out.columns}, inplace=True)

    # 排序
    if "日期" in out.columns:
        out = out.sort_values("日期").reset_index(drop=True)

    close = pd.to_numeric(out["收盘"], errors="coerce")
    high = pd.to_numeric(out["最高"], errors="coerce")
    low = pd.to_numeric(out["最低"], errors="coerce")
    prev_close = close.shift(1)

    # 涨跌幅
    out["涨跌幅"] = (close / prev_close - 1) * 100
    # 涨跌额
    out["涨跌额"] = close - prev_close
    # 振幅
    out["振幅"] = (high - low) / prev_close.replace(0, float("nan")) * 100
    # 换手率（Sina 不提供，留空）
    out["换手率"] = None

    return out


@retry(wait=wait_exponential(multiplier=2, min=3, max=30), stop=stop_after_attempt(3))
def _fetch_stock_daily_hist_hk_sina(symbol: str) -> pd.DataFrame:
    """新浪港股日线（``stock_hk_daily``，境外可访问）。"""
    _configure_hk_http()
    df = ak.stock_hk_daily(symbol=symbol, adjust="qfq")
    return _normalize_hk_sina_to_em_columns(df)


def fetch_hk_daily_bars(symbol: str, start_date: str | None = None) -> tuple[pd.DataFrame, str]:
    """拉取港股日线，返回 (DataFrame, source_tag)。

    按 ``HK_DAILY_PROVIDER`` 选择源：
    - ``sina``（默认）：新浪 ``stock_hk_daily``，境外可访问，返回全历史
    - ``em``：东财 ``stock_hk_hist``，境内更快，跨度大时自动分段
    """
    _configure_hk_http()
    provider = hk_daily_provider()

    # ── Sina 源 ──
    if provider == "sina":
        tag = HK_DAILY_SOURCE_SINA
        df = _fetch_stock_daily_hist_hk_sina(symbol)
        if start_date and not df.empty and "日期" in df.columns:
            df = df[df["日期"] >= pd.to_datetime(start_date, format="%Y%m%d").date()]
        return df, tag

    # ── 东财源 ──
    tag = HK_DAILY_SOURCE_EM
    beg, end = _hk_start_end(start_date)

    if not start_date:
        df = _fetch_stock_daily_hist_hk_chunk(symbol, beg, end)
        return df, tag

    start = pd.to_datetime(beg, format="%Y%m%d")
    end_dt = pd.to_datetime(end, format="%Y%m%d")
    if (end_dt - start).days <= HK_DAILY_CHUNK_DAYS:
        df = _fetch_stock_daily_hist_hk_chunk(symbol, beg, end)
        return df, tag

    parts: list[pd.DataFrame] = []
    cur = start
    chunk = pd.Timedelta(days=HK_DAILY_CHUNK_DAYS)
    while cur <= end_dt:
        chunk_end = min(cur + chunk - pd.Timedelta(days=1), end_dt)
        beg_s, end_s = cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
        logger.info("[INFO] %s 港股日线分段 %s ~ %s", symbol, beg_s, end_s)
        part = pd.DataFrame()
        for attempt in range(2):
            try:
                part = _fetch_stock_daily_hist_hk_chunk(symbol, beg_s, end_s)
                break
            except Exception as exc:
                if attempt == 0:
                    logger.warning("[WARN] %s 港股分段 %s~%s 失败重试: %s", symbol, beg_s, end_s, exc)
                    time.sleep(15)
                else:
                    raise
        if not part.empty:
            parts.append(part)
        cur = chunk_end + pd.Timedelta(days=1)
        throttle(2)

    if not parts:
        return pd.DataFrame(), tag
    merged = pd.concat(parts, ignore_index=True)
    return merged.drop_duplicates(subset=["日期"], keep="last"), tag


# ── 港股日线入库 ──────────────────────────────────────────────────


def upsert_hk_daily_prices(
    ts_code: str,
    df: pd.DataFrame,
    *,
    adjust_type: str = DEFAULT_ADJUST_TYPE,
    source: str = HK_DAILY_SOURCE_EM,
) -> int:
    """港股日线写入 ``daily_prices``（market='hk'）。

    复用 A 股 upsert 的字段映射逻辑，仅注入 ``market`` 列。
    """
    if df.empty:
        return 0

    ingested_at = datetime.utcnow()
    rows = []
    for _, row in df.iterrows():
        trade_date = _to_date(row.get("日期"))
        if trade_date is None:
            continue
        rows.append({
            "ts_code": ts_code,
            "trade_date": trade_date,
            "market": HK_MARKET,
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
        })

    if not rows:
        return 0

    from zplan_shared.models import DailyPrice, SessionLocal

    total = 0
    with SessionLocal() as session:
        for i in range(0, len(rows), _UPSERT_BATCH_SIZE):
            chunk = rows[i : i + _UPSERT_BATCH_SIZE]
            stmt = insert(DailyPrice).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "trade_date", "market"],
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


# ── 港股增量更新 ──────────────────────────────────────────────────


def get_hk_latest_trade_date(ts_code: str) -> date | None:
    """库内某港股的最新交易日。"""
    from zplan_shared.models import DailyPrice, SessionLocal

    with SessionLocal() as session:
        return session.execute(
            select(func.max(DailyPrice.trade_date)).where(
                DailyPrice.ts_code == ts_code,
                DailyPrice.market == HK_MARKET,
            )
        ).scalar_one_or_none()


def run_hk_incremental_update(limit: int | None = None) -> dict[str, int]:
    """港股全市场日线增量更新。

    与 A 股 ``run_incremental_update`` 平行的独立入口。
    """
    _configure_hk_http()
    from zplan_shared.models import init_db
    init_db()

    logger.info("[INFO] 开始拉取港股列表…")
    try:
        stock_df = ensure_hk_stock_list()
    except Exception as exc:
        logger.warning("[WARN] 港股列表不可用: %s", exc)
        raise

    symbols = stock_df["code"].astype(str).tolist()
    if limit:
        symbols = symbols[:limit]

    from zplan_shared.market import latest_trade_date
    market_latest = latest_trade_date(market=HK_MARKET)
    skipped = 0
    updated = 0
    failed = 0
    rows = 0

    for idx, symbol in enumerate(symbols, 1):
        sym_latest = get_hk_latest_trade_date(symbol)
        if market_latest and sym_latest and sym_latest > market_latest:
            skipped += 1
            if idx % 200 == 0 or idx == len(symbols):
                logger.info(
                    "[INFO] [港股 %s/%s] 已跳过 %s 只（库内最新 %s）",
                    idx, len(symbols), skipped, market_latest,
                )
            continue

        logger.info("[INFO] [港股 %s/%s] 更新 %s", idx, len(symbols), symbol)
        start_date = None
        if sym_latest:
            start_date = (sym_latest + pd.Timedelta(days=1)).strftime("%Y%m%d")

        try:
            price_df, source = fetch_hk_daily_bars(symbol=symbol, start_date=start_date)
            upsert_n = upsert_hk_daily_prices(symbol, price_df, source=source)
            if upsert_n:
                updated += 1
                rows += upsert_n
            logger.info("[INFO] %s 港股日线更新 %s 条", symbol, upsert_n)
        except Exception as exc:
            failed += 1
            logger.warning("[WARN] 港股 %s 拉取失败: %s", symbol, exc)
        finally:
            throttle()

    stats = {
        "total": len(symbols),
        "skipped": skipped,
        "updated": updated,
        "failed": failed,
        "rows": rows,
        "market": HK_MARKET,
    }
    logger.info("[INFO] 港股增量更新完成: %s", stats)
    return stats


def run_hk_a1_update(
    limit: int | None = None,
    *,
    skip_intraday: bool = False,
) -> dict[str, int]:
    """港股 Phase A.1：全市场日线（东财 source）。

    当前不包含分时（港股分时接口 `stock_hk_hist_min_em` 可选启用）。
    """
    _configure_hk_http()
    from zplan_shared.models import init_db
    init_db()

    stats: dict[str, int] = {
        "daily_ok": 0,
        "daily_fail": 0,
        "daily_rows": 0,
        "daily_source": 0,  # source tag 是 str，但保持 int 兼容
    }

    stats["daily_source"] = 0  # placeholder

    logger.info("[INFO] 港股 A.1 开始：股票列表 + 日线(%s)", hk_daily_source_tag())

    try:
        stock_df = ensure_hk_stock_list()
        logger.info("[INFO] 港股列表就绪，%s 只", len(stock_df))
    except Exception as exc:
        logger.warning("[WARN] 港股列表不可用: %s", exc)
        raise

    symbols = stock_df["code"].astype(str).tolist()
    if limit:
        symbols = symbols[:limit]

    for idx, symbol in enumerate(symbols, 1):
        logger.info("[INFO] [港股 %s/%s] 日线 %s", idx, len(symbols), symbol)
        latest_date = get_hk_latest_trade_date(symbol)
        if not latest_date:
            start_date = (
                pd.Timestamp.today() - pd.Timedelta(days=HK_DAILY_BOOTSTRAP_CALENDAR_DAYS)
            ).strftime("%Y%m%d")
        else:
            start_date = (latest_date + pd.Timedelta(days=1)).strftime("%Y%m%d")

        try:
            price_df, source = fetch_hk_daily_bars(symbol=symbol, start_date=start_date)
            upsert_rows = upsert_hk_daily_prices(symbol, price_df, source=source)
            stats["daily_ok"] += 1
            stats["daily_rows"] += upsert_rows
            logger.info("[INFO] %s 港股日线(%s) 更新 %s 条", symbol, source, upsert_rows)
        except Exception as exc:
            stats["daily_fail"] += 1
            logger.warning("[WARN] 港股 %s 日线失败: %s", symbol, exc)
            throttle(8)
        else:
            throttle()

    logger.info("[INFO] 港股 A.1 完成。统计: %s", stats)
    return stats


# ── CLI 入口 ──────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import sys

    if "--a1" in sys.argv:
        limit = None
        for i, arg in enumerate(sys.argv):
            if arg.startswith("--limit="):
                limit = int(arg.split("=", 1)[1])
            elif arg == "--limit" and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
        skip_intraday = "--skip-intraday" in sys.argv
        run_hk_a1_update(limit=limit, skip_intraday=skip_intraday)
    else:
        limit = None
        for i, arg in enumerate(sys.argv):
            if arg.startswith("--limit="):
                limit = int(arg.split("=", 1)[1])
            elif arg == "--limit" and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
        run_hk_incremental_update(limit=limit)
