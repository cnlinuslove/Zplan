from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert
from tenacity import retry, stop_after_attempt, wait_exponential

from zplan_shared.config import (
    AKSHARE_DAILY_CHUNK_DAYS,
    AKSHARE_FAIL_CIRCUIT_SLEEP_SECONDS,
    AKSHARE_FAIL_CIRCUIT_THRESHOLD,
    DAILY_BOOTSTRAP_CALENDAR_DAYS,
)
from zplan_shared.data_sources import (
    daily_provider,
    daily_provider_label,
    daily_source_tag,
)
from zplan_shared.http_client import configure_akshare_http, throttle
from zplan_shared.market import DEFAULT_ADJUST_TYPE, latest_trade_date as db_latest_trade_date
from zplan_shared.models import DailyPrice, SessionLocal, StockList, init_db


logger = logging.getLogger(__name__)

# 兼容旧引用
AKSHARE_SOURCE = daily_source_tag()
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
    """沪深京代码 → 新浪/腾讯前缀（``sz`` / ``sh`` / ``bj``）。"""
    code = ts_code.strip()
    if code.startswith("92") or code.startswith(("4", "8")):
        return f"bj{code}"
    if code.startswith(("5", "6")) or (
        code.startswith("9") and not code.startswith("92")
    ):
        return f"sh{code}"
    return f"sz{code}"


ts_code_to_sina_symbol = ts_code_to_tx_symbol

# 东财 stock_zh_a_hist 推荐显式 period（AkShare >=1.16 默认已是 daily）
_EM_HIST_KWARGS = {"period": "daily", "adjust": "qfq"}


def get_akshare_version() -> str:
    return getattr(ak, "__version__", "unknown")


def _normalize_hist_to_em_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if "日期" in df.columns:
        return _enrich_daily_derived_fields(df)
    out = df.copy()
    mapping = {
        "date": "日期",
        "open": "开盘",
        "close": "收盘",
        "high": "最高",
        "low": "最低",
        "amount": "成交额",
        "volume": "成交量",
    }
    for src, dst in mapping.items():
        if src in out.columns:
            out[dst] = out[src]
    if "turnover" in out.columns and "换手率" not in out.columns:
        # 新浪 turnover 为小数（如 0.006）；东财为百分数（如 0.63）
        t = pd.to_numeric(out["turnover"], errors="coerce")
        out["换手率"] = t * 100 if t.max(skipna=True) is not None and t.max() <= 1 else t
    return _enrich_daily_derived_fields(out)


def _enrich_daily_derived_fields(df: pd.DataFrame) -> pd.DataFrame:
    """新浪等缺涨跌幅/振幅时，由 OHLC 递推补齐（Phase A.1 完整量价）。"""
    if df.empty or "收盘" not in df.columns:
        return df
    out = df.sort_values("日期").copy() if "日期" in df.columns else df.copy()
    close = pd.to_numeric(out["收盘"], errors="coerce")
    high = pd.to_numeric(out.get("最高"), errors="coerce")
    low = pd.to_numeric(out.get("最低"), errors="coerce")
    prev = close.shift(1)

    if "涨跌幅" not in out.columns or out["涨跌幅"].isna().all():
        out["涨跌幅"] = (close / prev - 1) * 100
    else:
        out["涨跌幅"] = out["涨跌幅"].fillna((close / prev - 1) * 100)

    if "涨跌额" not in out.columns or out["涨跌额"].isna().all():
        out["涨跌额"] = close - prev
    else:
        out["涨跌额"] = out["涨跌额"].fillna(close - prev)

    if "振幅" not in out.columns or out["振幅"].isna().all():
        out["振幅"] = (high - low) / prev.replace(0, pd.NA) * 100
    else:
        out["振幅"] = out["振幅"].fillna((high - low) / prev.replace(0, pd.NA) * 100)

    return out


def _stock_list_from_db() -> pd.DataFrame:
    init_db()
    with SessionLocal() as session:
        rows = session.execute(select(StockList.ts_code, StockList.name)).all()
    if not rows:
        return pd.DataFrame(columns=["code", "name"])
    return pd.DataFrame([{"code": r[0], "name": r[1]} for r in rows])


@retry(wait=wait_exponential(multiplier=2, min=4, max=45), stop=stop_after_attempt(4))
def _fetch_stock_list_em() -> pd.DataFrame:
    """东财沪深京 A 股列表（与日线同源，避免上交所 query.sse.com.cn 超时）。"""
    spot = ak.stock_zh_a_spot_em()
    out = spot[["代码", "名称"]].copy()
    out.columns = ["code", "name"]
    out["code"] = out["code"].astype(str).str.zfill(6)
    return out.dropna(subset=["code", "name"])


@retry(wait=wait_exponential(multiplier=2, min=4, max=45), stop=stop_after_attempt(3))
def _fetch_stock_list_exchanges() -> pd.DataFrame:
    """沪深京交易所官网列表（上交所段易超时）。"""
    return ak.stock_info_a_code_name()


def ensure_stock_list(*, min_cached: int = 3000) -> pd.DataFrame:
    """
    获取 A 股代码列表并写入 ``stock_list``。
    东财日线模式下优先 ``stock_zh_a_spot_em``；失败则用库内缓存（≥min_cached 条）。
    """
    configure_akshare_http()
    prefer_em = os.getenv("AKSHARE_STOCK_LIST_SOURCE", "auto").strip().lower()
    if prefer_em == "auto":
        prefer_em = "em" if daily_provider() in ("em", "sina") else "sse"

    last_exc: Exception | None = None
    if prefer_em == "em":
        try:
            df = _fetch_stock_list_em()
            logger.info("[INFO] 股票列表来自东财 spot，%s 只", len(df))
            upsert_stock_list(df)
            return df
        except Exception as exc:
            last_exc = exc
            logger.warning("[WARN] 东财股票列表失败: %s", exc)

    if prefer_em != "em":
        try:
            df = _fetch_stock_list_exchanges()
            logger.info("[INFO] 股票列表来自交易所接口，%s 只", len(df))
            upsert_stock_list(df)
            return df
        except Exception as exc:
            last_exc = exc
            logger.warning("[WARN] 交易所股票列表失败: %s", exc)

    cached = _stock_list_from_db()
    if len(cached) >= min_cached:
        logger.info(
            "[INFO] 使用库内股票列表缓存 %s 只（跳过在线刷新）",
            len(cached),
        )
        return cached

    if last_exc:
        raise last_exc
    raise RuntimeError("无法获取股票列表且无足够库内缓存")


def fetch_stock_list() -> pd.DataFrame:
    return ensure_stock_list()


@retry(wait=wait_exponential(multiplier=2, min=3, max=30), stop=stop_after_attempt(3))
def _fetch_stock_daily_hist_em_chunk(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    return ak.stock_zh_a_hist(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        timeout=60,
        **_EM_HIST_KWARGS,
    )


@retry(wait=wait_exponential(multiplier=2, min=3, max=30), stop=stop_after_attempt(3))
def _fetch_stock_daily_hist_em_full(symbol: str) -> pd.DataFrame:
    return ak.stock_zh_a_hist(symbol=symbol, timeout=60, **_EM_HIST_KWARGS)


def _fetch_stock_daily_hist_em(symbol: str, start_date: str | None = None) -> pd.DataFrame:
    """东财日线；跨度大于 ``AKSHARE_DAILY_CHUNK_DAYS`` 时分段请求后合并。"""
    configure_akshare_http()
    if not start_date:
        return _fetch_stock_daily_hist_em_full(symbol)

    start = pd.to_datetime(start_date, format="%Y%m%d")
    end = pd.Timestamp.today().normalize()
    if (end - start).days <= AKSHARE_DAILY_CHUNK_DAYS:
        return _fetch_stock_daily_hist_em_chunk(
            symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        )

    parts: list[pd.DataFrame] = []
    cur = start
    chunk = pd.Timedelta(days=AKSHARE_DAILY_CHUNK_DAYS)
    while cur <= end:
        chunk_end = min(cur + chunk - pd.Timedelta(days=1), end)
        beg, en = cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
        logger.info("[INFO] %s 日线分段 %s ~ %s", symbol, beg, en)
        part = pd.DataFrame()
        for attempt in range(2):
            try:
                part = _fetch_stock_daily_hist_em_chunk(symbol, beg, en)
                break
            except Exception as exc:
                if attempt == 0:
                    logger.warning(
                        "[WARN] %s 分段 %s~%s 失败，15s 后重试: %s",
                        symbol,
                        beg,
                        en,
                        exc,
                    )
                    throttle(15)
                else:
                    raise
        if not part.empty:
            parts.append(part)
        cur = chunk_end + pd.Timedelta(days=1)
        throttle(2)

    if not parts:
        return pd.DataFrame()
    merged = pd.concat(parts, ignore_index=True)
    return merged.drop_duplicates(subset=["日期"], keep="last")


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


@retry(wait=wait_exponential(multiplier=2, min=3, max=30), stop=stop_after_attempt(3))
def _fetch_stock_daily_hist_sina(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
) -> pd.DataFrame:
    end = end_date or pd.Timestamp.today().strftime("%Y-%m-%d")
    df = ak.stock_zh_a_daily(
        symbol=ts_code_to_sina_symbol(symbol),
        start_date=_ymd_to_iso(start_date),
        end_date=end,
        adjust="qfq",
    )
    return _normalize_hist_to_em_columns(df)


def fetch_daily_bars(symbol: str, start_date: str | None = None) -> tuple[pd.DataFrame, str]:
    """拉取日线；全库使用 ``AKSHARE_DAILY_PROVIDER`` 指定的唯一 AkShare 接口，不按票混源。"""
    configure_akshare_http()
    tag = daily_source_tag()
    provider = daily_provider()
    if not start_date and provider != "em":
        start_date = (pd.Timestamp.today() - pd.Timedelta(days=365)).strftime("%Y%m%d")
    if provider == "tx":
        return _fetch_stock_daily_hist_tx(symbol, start_date or "19700101"), tag
    if provider == "sina":
        return _fetch_stock_daily_hist_sina(symbol, start_date or "19900101"), tag
    return _fetch_stock_daily_hist_em(symbol, start_date), tag


def probe_daily_em(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    quick: bool = False,
) -> tuple[bool, str, int]:
    """东财日线短区间探测，供拉取前自检。``quick=True`` 时单次请求、不重试。"""
    import os

    prev_fb = os.environ.get("AKSHARE_EASTMONEY_PROXY_FALLBACK")
    if quick:
        os.environ["AKSHARE_EASTMONEY_PROXY_FALLBACK"] = "false"
    configure_akshare_http()
    try:
        if quick:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                timeout=15,
                **_EM_HIST_KWARGS,
            )
        else:
            df = _fetch_stock_daily_hist_em(symbol, start_date=start_date)
        n = len(df)
        return (n > 0, "ok" if n else "返回 0 行", n)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", 0
    finally:
        if quick:
            if prev_fb is None:
                os.environ.pop("AKSHARE_EASTMONEY_PROXY_FALLBACK", None)
            else:
                os.environ["AKSHARE_EASTMONEY_PROXY_FALLBACK"] = prev_fb


def probe_daily_tx(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    quick: bool = False,
) -> tuple[bool, str, int]:
    """腾讯日线短区间探测。"""
    configure_akshare_http()
    try:
        if quick:
            df = ak.stock_zh_a_hist_tx(
                symbol=ts_code_to_tx_symbol(symbol),
                start_date=_ymd_to_iso(start_date),
                end_date=_ymd_to_iso(end_date),
                adjust="qfq",
                timeout=15,
            )
        else:
            df = _fetch_stock_daily_hist_tx(symbol, start_date, end_date)
        n = len(df)
        return (n > 0, "ok" if n else "返回 0 行", n)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", 0


def probe_daily_sina(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    quick: bool = False,
) -> tuple[bool, str, int]:
    """新浪日线短区间探测。"""
    configure_akshare_http()
    try:
        if quick:
            df = ak.stock_zh_a_daily(
                symbol=ts_code_to_sina_symbol(symbol),
                start_date=_ymd_to_iso(start_date),
                end_date=_ymd_to_iso(end_date),
                adjust="qfq",
            )
        else:
            df = _fetch_stock_daily_hist_sina(symbol, start_date, end_date)
        n = len(df)
        return (n > 0, "ok" if n else "返回 0 行", n)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", 0


def fetch_stock_daily_hist(
    symbol: str,
    start_date: str | None = None,
    *,
    prefer_tx: bool = False,
    em_only: bool = False,
) -> tuple[pd.DataFrame, str]:
    """兼容旧调用；``prefer_tx``/``em_only`` 已忽略，请用 ``fetch_daily_bars``。"""
    if prefer_tx or em_only:
        logger.debug("prefer_tx/em_only 已废弃，使用 AKSHARE_DAILY_PROVIDER=%s", daily_provider())
    return fetch_daily_bars(symbol, start_date)


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


def get_latest_trade_date(
    ts_code: str,
    *,
    adjust_type: str = DEFAULT_ADJUST_TYPE,
) -> date | None:
    from zplan_shared.market import resolve_ts_code

    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        result = session.execute(
            select(func.max(DailyPrice.trade_date)).where(
                DailyPrice.ts_code == code,
                DailyPrice.adjust_type == adjust_type,
            )
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
) -> dict[str, int]:
    configure_akshare_http()
    init_db()
    logger.info(
        "[INFO] 开始拉取股票列表（日线源: %s / %s）...",
        daily_provider_label(),
        daily_source_tag(),
    )

    try:
        stock_df = ensure_stock_list()
        circuit_breaker.record_success()
    except Exception as exc:
        circuit_breaker.record_failure()
        logger.warning("[WARN] 股票列表不可用: %s", exc)
        raise

    symbols = stock_df["code"].astype(str).tolist()
    if limit:
        symbols = symbols[:limit]

    # 跑批开始时固定全库最新日；仅用「>」跳过（等于时仍拉下一交易日，避免新交易日只更新少数票）
    market_latest = db_latest_trade_date()
    skipped = 0
    updated = 0
    failed = 0
    rows = 0

    for idx, symbol in enumerate(symbols, 1):
        sym_latest = get_latest_trade_date(symbol)
        if market_latest and sym_latest and sym_latest > market_latest:
            skipped += 1
            if idx % 500 == 0 or idx == len(symbols):
                logger.info(
                    "[INFO] [%s/%s] 已跳过 %s 只（库内最新 %s）",
                    idx,
                    len(symbols),
                    skipped,
                    market_latest,
                )
            continue

        logger.info("[INFO] [%s/%s] 更新 %s", idx, len(symbols), symbol)
        start_date = None
        if sym_latest:
            start_date = (sym_latest + pd.Timedelta(days=1)).strftime("%Y%m%d")
        elif recent_days:
            start_date = (pd.Timestamp.today() - pd.Timedelta(days=recent_days)).strftime(
                "%Y%m%d"
            )

        try:
            price_df, source = fetch_daily_bars(symbol=symbol, start_date=start_date)
            upsert_n = upsert_daily_prices(symbol, price_df, source=source)
            circuit_breaker.record_success()
            if upsert_n:
                updated += 1
                rows += upsert_n
            logger.info("[INFO] %s 日线更新 %s 条", symbol, upsert_n)
        except Exception as exc:
            circuit_breaker.record_failure()
            failed += 1
            logger.warning("[WARN] %s 拉取失败: %s", symbol, exc)
        finally:
            throttle()

    stats = {
        "total": len(symbols),
        "skipped": skipped,
        "updated": updated,
        "failed": failed,
        "rows": rows,
        "market_latest": str(market_latest) if market_latest else None,
    }
    logger.info("[INFO] 增量更新完成: %s", stats)
    return stats


def symbols_missing_panel_date(panel_date: date | None = None) -> list[str]:
    """库内尚无 ``panel_date``（qfq）日线行的 ``ts_code`` 列表（默认全库最新交易日）。"""
    init_db()
    target = panel_date or db_latest_trade_date()
    if target is None:
        return []
    with SessionLocal() as session:
        have = set(
            session.execute(
                select(DailyPrice.ts_code).where(
                    DailyPrice.trade_date == target,
                    DailyPrice.adjust_type == DEFAULT_ADJUST_TYPE,
                )
            ).scalars().all()
        )
        all_codes = session.execute(select(StockList.ts_code)).scalars().all()
    return [c for c in all_codes if c not in have]


_db_write_lock = threading.Lock()


def _catchup_one_symbol(symbol: str, *, interval: float) -> tuple[str, int, bool]:
    """单票补齐（进程内调用，避免 AkShare/mini_racer 跨线程）。"""
    from zplan_shared.market import resolve_ts_code

    configure_akshare_http()
    ts_code = resolve_ts_code(symbol)
    sym_latest = get_latest_trade_date(ts_code)
    start_date = None
    if sym_latest:
        start_date = (sym_latest + pd.Timedelta(days=1)).strftime("%Y%m%d")
    try:
        price_df, source = fetch_daily_bars(symbol=symbol, start_date=start_date)
        with _db_write_lock:
            upsert_n = upsert_daily_prices(ts_code, price_df, source=source)
        time.sleep(interval)
        return symbol, upsert_n, True
    except Exception as exc:
        logger.warning("[WARN] 补齐 %s 失败: %s", symbol, exc)
        time.sleep(max(interval, 2.0))
        return symbol, 0, False


def _catchup_worker(args: tuple[str, float]) -> tuple[str, int, bool]:
    return _catchup_one_symbol(args[0], interval=args[1])


def run_post_incremental_catchup(
    limit: int | None = None,
    workers: int | None = None,
) -> dict[str, int | str | None | bool] | None:
    """
    增量结束后自动补齐缺「全库最新交易日」截面的标的（隔日/漏跑 cron 恢复）。

    可由环境变量 ``DAILY_AUTO_CATCHUP_PANEL=false`` 关闭。
    """
    if os.getenv("DAILY_AUTO_CATCHUP_PANEL", "true").lower() in ("0", "false", "no"):
        return None
    init_db()
    target = db_latest_trade_date()
    if target is None:
        return None
    missing = symbols_missing_panel_date(target)
    if not missing:
        logger.info("[INFO] 截面已齐 @ %s，无需自动补齐", target)
        return {
            "panel_date": str(target),
            "missing_before": 0,
            "queued": 0,
            "updated": 0,
            "failed": 0,
            "rows": 0,
            "skipped": True,
        }
    logger.info(
        "[INFO] 检测到 %s 只缺 %s 截面，开始自动补齐（漏跑/隔日恢复）",
        len(missing),
        target,
    )
    stats = run_catchup_panel_update(limit=limit, panel_date=target, workers=workers)
    stats["skipped"] = False
    return stats


def run_catchup_panel_update(
    limit: int | None = None,
    *,
    panel_date: date | None = None,
    workers: int | None = None,
) -> dict[str, int | str | None]:
    """
    仅补齐缺最新截面日的标的（选股 ``init-rule`` 前推荐先跑）。

    比全量 ``run_incremental_update`` 更快：跳过库内已对齐最新交易日的股票。
    """
    configure_akshare_http()
    init_db()
    target = panel_date or db_latest_trade_date()
    if target is None:
        return {"ok": 0, "message": "无 market latest 交易日", "missing": 0}

    missing_ts = symbols_missing_panel_date(target)
    if limit:
        missing_ts = missing_ts[:limit]

    def _symbol_from_ts(ts_code: str) -> str:
        return ts_code.split(".", 1)[0].zfill(6)

    missing_codes = [_symbol_from_ts(t) for t in missing_ts]

    updated = 0
    failed = 0
    rows = 0
    logger.info(
        "[INFO] 截面补齐 @ %s：待更新 %s 只（全市场缺行 %s）",
        target,
        len(missing_codes),
        len(missing_ts),
    )

    if not missing_codes:
        return {
            "panel_date": str(target),
            "missing_before": 0,
            "queued": 0,
            "updated": 0,
            "failed": 0,
            "rows": 0,
            "workers": 0,
        }

    n_workers = workers if workers is not None else int(os.getenv("CATCHUP_PANEL_WORKERS", "6"))
    n_workers = max(1, min(n_workers, 16))
    base_interval = float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "3"))
    interval = max(0.35, base_interval / n_workers)

    logger.info(
        "[INFO] 多进程补齐 workers=%s interval=%.2fs/票（AkShare 不宜多线程）",
        n_workers,
        interval,
    )

    done = 0
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_catchup_worker, (sym, interval)): sym
            for sym in missing_codes
        }
        for fut in as_completed(futures):
            done += 1
            symbol, upsert_n, ok = fut.result()
            if ok and upsert_n:
                updated += 1
                rows += upsert_n
            elif not ok:
                failed += 1
            if done % 100 == 0 or done == len(missing_codes):
                logger.info(
                    "[INFO] 进度 %s/%s ok=%s fail=%s rows=%s",
                    done,
                    len(missing_codes),
                    updated,
                    failed,
                    rows,
                )

    stats = {
        "panel_date": str(target),
        "missing_before": len(missing_ts),
        "queued": len(missing_codes),
        "updated": updated,
        "failed": failed,
        "rows": rows,
        "workers": n_workers,
    }
    logger.info("[INFO] 截面补齐完成: %s", stats)
    return stats


def run_a1_update(
    limit: int | None = None,
    *,
    skip_intraday: bool = False,
    clear_demo: bool = False,
    realign_source: bool = False,
) -> dict[str, int]:
    """Phase A.1：全市场日线（统一 AkShare 源）+ 近两周分时（东财接口）。"""
    from zplan_shared.etl_intraday import sync_intraday_universe

    stats = {
        "daily_ok": 0,
        "daily_fail": 0,
        "daily_rows": 0,
        "intraday_ok": 0,
        "intraday_fail": 0,
        "daily_source": daily_source_tag(),
    }

    if clear_demo:
        n = clear_demo_market_data()
        if n:
            logger.info("已清除演示行情 %s 条", n)

    configure_akshare_http()
    init_db()
    logger.info(
        "[INFO] A.1 开始：股票列表 + 日线(%s) + 近端分时(akshare_em)",
        daily_source_tag(),
    )

    try:
        stock_df = ensure_stock_list()
        circuit_breaker.record_success()
        logger.info("[INFO] 股票列表就绪，%s 只", len(stock_df))
    except Exception as exc:
        circuit_breaker.record_failure()
        logger.warning("[WARN] 股票列表不可用: %s", exc)
        raise

    symbols = stock_df["code"].astype(str).tolist()
    if limit:
        symbols = symbols[:limit]

    for idx, symbol in enumerate(symbols, 1):
        logger.info("[INFO] [%s/%s] 日线 %s", idx, len(symbols), symbol)
        latest_date = get_latest_trade_date(symbol)
        expected = daily_source_tag()
        needs_realign = realign_source and _needs_source_realign(symbol, expected)

        if realign_source and latest_date and not needs_realign:
            logger.info("[INFO] %s 已是 %s，跳过", symbol, expected)
            stats["daily_ok"] += 1
            throttle()
            continue

        if needs_realign or not latest_date:
            start_date = (
                pd.Timestamp.today() - pd.Timedelta(days=DAILY_BOOTSTRAP_CALENDAR_DAYS)
            ).strftime("%Y%m%d")
            if needs_realign:
                logger.info("[INFO] %s 对齐 %s，自 %s 全量重拉", symbol, expected, start_date)
        else:
            start_date = (latest_date + pd.Timedelta(days=1)).strftime("%Y%m%d")

        try:
            price_df, source = fetch_daily_bars(symbol=symbol, start_date=start_date)
            upsert_rows = upsert_daily_prices(symbol, price_df, source=source)
            circuit_breaker.record_success()
            stats["daily_ok"] += 1
            stats["daily_rows"] += upsert_rows
            logger.info("[INFO] %s 日线(%s) 更新 %s 条", symbol, source, upsert_rows)
        except Exception as exc:
            circuit_breaker.record_failure()
            stats["daily_fail"] += 1
            logger.warning("[WARN] %s 日线(%s) 失败: %s", symbol, expected, exc)
            throttle(8)
        else:
            throttle()

    if not skip_intraday:
        intra_stats = sync_intraday_universe(symbols)
        stats["intraday_ok"] = intra_stats.get("ok", 0)
        stats["intraday_fail"] = intra_stats.get("fail", 0)

    logger.info("[INFO] A.1 完成。统计: %s", stats)
    return stats


def _needs_source_realign(ts_code: str, expected_source: str) -> bool:
    """库内该票是否与当前配置的日线 source 不一致。"""
    with SessionLocal() as session:
        sources = session.execute(
            select(DailyPrice.source)
            .where(DailyPrice.ts_code == ts_code)
            .distinct()
        ).scalars().all()
    if not sources:
        return True
    return expected_source not in sources or len(sources) > 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_incremental_update(limit=5)
