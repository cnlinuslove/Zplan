"""Phase B：日频估值 / 市值截面 ETL。"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.http_client import configure_akshare_http
from zplan_shared.market import latest_trade_date
from zplan_shared.models import DailySnapshot, SessionLocal, StockList, init_db
from sqlalchemy import select

logger = logging.getLogger(__name__)

SNAPSHOT_SOURCE_EM = "akshare_em_snapshot"
SNAPSHOT_SOURCE_BAIDU = "akshare_baidu_valuation"
_UPSERT_BATCH = 100
_DB_WRITE_LOCK = threading.Lock()


def _mv_to_yuan(value: float | None) -> float | None:
    """统一市值单位为「元」：百度为亿元，东财 spot 为元。"""
    if value is None or pd.isna(value):
        return None
    v = float(value)
    if v <= 0:
        return None
    # 百度 valuation 接口数量级约千（亿）
    if v < 1e6:
        return v * 1e8
    return v


def _fetch_snapshot_em() -> pd.DataFrame:
    configure_akshare_http()
    spot = ak.stock_zh_a_spot_em()
    trade_date = latest_trade_date() or date.today()
    rows = []
    for _, row in spot.iterrows():
        code = str(row.get("代码", "")).strip().zfill(6)
        if not code:
            continue
        rows.append(
            {
                "ts_code": code,
                "trade_date": trade_date,
                "pe_ttm": _float_or_none(row.get("市盈率-动态")),
                "pb": _float_or_none(row.get("市净率")),
                "ps_ttm": None,
                "total_mv": _mv_to_yuan(_float_or_none(row.get("总市值"))),
                "circ_mv": _mv_to_yuan(_float_or_none(row.get("流通市值"))),
                "turnover_rate": _float_or_none(row.get("换手率")),
                "source": SNAPSHOT_SOURCE_EM,
            }
        )
    return pd.DataFrame(rows)


def _float_or_none(val: object) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _fetch_snapshot_baidu_one(symbol: str, trade_date: date) -> dict | None:
    configure_akshare_http()
    pe = pb = mv = None
    for indicator, attr in (
        ("市盈率(TTM)", "pe_ttm"),
        ("市净率", "pb"),
        ("总市值", "total_mv"),
    ):
        try:
            df = ak.stock_zh_valuation_baidu(
                symbol=symbol, indicator=indicator, period="近一年"
            )
            if df.empty:
                continue
            val = _float_or_none(df.iloc[-1].get("value"))
            if attr == "total_mv":
                val = _mv_to_yuan(val)
            if attr == "pe_ttm":
                pe = val
            elif attr == "pb":
                pb = val
            else:
                mv = val
        except Exception:
            continue
    if pe is None and pb is None and mv is None:
        return None
    return {
        "ts_code": symbol,
        "trade_date": trade_date,
        "pe_ttm": pe,
        "pb": pb,
        "ps_ttm": None,
        "total_mv": mv,
        "circ_mv": None,
        "turnover_rate": None,
        "source": SNAPSHOT_SOURCE_BAIDU,
    }


def upsert_daily_snapshot(rows: list[dict]) -> int:
    if not rows:
        return 0
    ingested_at = datetime.utcnow()
    for r in rows:
        r["ingested_at"] = ingested_at
    total = 0
    with SessionLocal() as session:
        for i in range(0, len(rows), _UPSERT_BATCH):
            chunk = rows[i : i + _UPSERT_BATCH]
            stmt = insert(DailySnapshot).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "trade_date"],
                set_={
                    "pe_ttm": stmt.excluded.pe_ttm,
                    "pb": stmt.excluded.pb,
                    "ps_ttm": stmt.excluded.ps_ttm,
                    "total_mv": stmt.excluded.total_mv,
                    "circ_mv": stmt.excluded.circ_mv,
                    "turnover_rate": stmt.excluded.turnover_rate,
                    "source": stmt.excluded.source,
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
            session.execute(stmt)
            total += len(chunk)
        session.commit()
    return total


def run_daily_snapshot_update(*, limit: int | None = None) -> dict[str, int]:
    """全市场估值截面：优先东财 spot 一次拉取；失败则百度逐票（较慢）。"""
    init_db()
    stats = {"ok": 0, "fail": 0, "rows": 0, "source": ""}
    trade_date = latest_trade_date() or date.today()

    provider = os.getenv("SNAPSHOT_PROVIDER", "auto").strip().lower()
    if provider in ("auto", "em"):
        try:
            df = _fetch_snapshot_em()
            if not df.empty:
                if limit:
                    df = df.head(limit)
                n = upsert_daily_snapshot(df.to_dict("records"))
                stats.update(ok=len(df), rows=n, source=SNAPSHOT_SOURCE_EM)
                logger.info("[INFO] daily_snapshot 东财 spot %s 条", n)
                return stats
        except Exception as exc:
            logger.warning("[WARN] 东财 spot 失败: %s", exc)
            if provider == "em":
                raise

    init_db()
    with SessionLocal() as session:
        symbols = [r[0] for r in session.execute(select(StockList.ts_code)).all()]
    if limit:
        symbols = symbols[:limit]

    n_workers = int(os.getenv("SNAPSHOT_WORKERS", "6"))
    n_workers = max(1, min(n_workers, 16))
    base_interval = float(os.getenv("SNAPSHOT_BAIDU_INTERVAL", "0.4"))
    interval = max(0.05, base_interval / n_workers)

    logger.info(
        "[INFO] snapshot 百度 workers=%s interval=%.2fs/票 (%s 只)",
        n_workers, interval, len(symbols),
    )

    all_rows: list[dict] = []
    ok = 0
    fail = 0
    done = 0

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_baidu_worker, (sym, trade_date, interval)): sym
            for sym in symbols
        }
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:
                fail += 1
                if done % 100 == 0 or done == len(symbols):
                    logger.warning("[WARN] snapshot 百度 [%s/%s] fail=%s", done, len(symbols), fail)
                continue
            if row:
                all_rows.append(row)
                ok += 1
            else:
                fail += 1
            if done % 100 == 0 or done == len(symbols):
                logger.info(
                    "[INFO] snapshot 百度 [%s/%s] ok=%s fail=%s",
                    done, len(symbols), ok, fail,
                )

    stats["ok"] = ok
    stats["fail"] = fail
    if all_rows:
        stats["rows"] = upsert_daily_snapshot(all_rows)
    stats["source"] = SNAPSHOT_SOURCE_BAIDU
    logger.info("[INFO] daily_snapshot 完成: %s", stats)
    return stats


def _baidu_worker(args: tuple[str, date, float]) -> dict | None:
    """单票百度估值抓取（进程内调用）。"""
    symbol, trade_date, interval = args
    configure_akshare_http()
    try:
        row = _fetch_snapshot_baidu_one(symbol, trade_date)
        time.sleep(interval)
        return row
    except Exception:
        time.sleep(max(interval, 1.0))
        return None
