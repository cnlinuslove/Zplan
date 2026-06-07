"""港股估值截面 ETL — 写入 ``daily_snapshot``（market='hk'）。

当前方案：逐票调用 ``stock_hk_financial_indicator_em``（东方财富港股财务指标）。
每票一次 API 调用，全市场约 2500 次请求；默认关闭，通过环境变量启用：

.. code-block:: bash

    export HK_SNAPSHOT_PER_SYMBOL_ENABLED=true

后续可探索批量接口替代（如扩展 ``stock_hk_spot_em`` 输出字段）。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from tenacity import retry, stop_after_attempt, wait_exponential

from zplan_shared.config import HK_SNAPSHOT_PER_SYMBOL_ENABLED
from zplan_shared.data_sources import HK_DAILY_SOURCE_EM
from zplan_shared.http_client import configure_akshare_http
from zplan_shared.market import DEFAULT_ADJUST_TYPE, latest_trade_date

logger = logging.getLogger(__name__)

HK_MARKET = "hk"
_SNAPSHOT_BATCH_SIZE = 50


def _float_val(data: dict, key: str) -> float | None:
    v = data.get(key)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


@retry(wait=wait_exponential(multiplier=2, min=3, max=30), stop=stop_after_attempt(2))
def _fetch_hk_financial_indicator(symbol: str) -> dict:
    """调用 ``stock_hk_financial_indicator_em`` 获取单票估值指标。"""
    configure_akshare_http()
    df = ak.stock_hk_financial_indicator_em(symbol=symbol)
    if df.empty:
        return {}
    # 返回最新一条记录（通常只有一条）
    row = df.iloc[0] if "指标" not in df.columns else df.set_index("指标").iloc[:, -1]
    if isinstance(row, pd.Series):
        return row.to_dict()
    return {"value": row}


def _extract_snapshot_fields(data: dict) -> dict:
    """从 ``stock_hk_financial_indicator_em`` 返回值中提取 snapshot 字段。

    列名映射（东方财富中文 → DB 列）：
    - 市盈率(PE) → pe_ttm
    - 市净率(PB) → pb
    - 总市值(港元) → total_mv
    - （港股无 ps_ttm，置空）
    """
    pe = _float_val(data, "市盈率(PE)") or _float_val(data, "市盈率")
    pb = _float_val(data, "市净率(PB)") or _float_val(data, "市净率")
    total_mv = _float_val(data, "总市值(港元)") or _float_val(data, "总市值")
    # 港股 spot 不直接提供换手率，可从日线表聚合
    return {
        "pe_ttm": pe,
        "pb": pb,
        "ps_ttm": None,
        "total_mv": total_mv,
        "circ_mv": None,
        "turnover_rate": None,
    }


def upsert_hk_snapshot(
    ts_code: str,
    trade_date: date,
    fields: dict,
    *,
    source: str = HK_DAILY_SOURCE_EM,
) -> None:
    """写入一条港股估值截面记录。"""
    from zplan_shared.models import DailySnapshot, SessionLocal

    ingested_at = datetime.utcnow()
    with SessionLocal() as session:
        stmt = insert(DailySnapshot).values(
            ts_code=ts_code,
            trade_date=trade_date,
            market=HK_MARKET,
            pe_ttm=fields.get("pe_ttm"),
            pb=fields.get("pb"),
            ps_ttm=fields.get("ps_ttm"),
            total_mv=fields.get("total_mv"),
            circ_mv=fields.get("circ_mv"),
            turnover_rate=fields.get("turnover_rate"),
            source=source,
            ingested_at=ingested_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ts_code", "trade_date", "market"],
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
        session.commit()


def run_hk_snapshot_update(limit: int | None = None) -> dict[str, int]:
    """港股估值截面批量更新。

    需设置 ``HK_SNAPSHOT_PER_SYMBOL_ENABLED=true`` 才会执行（否则直接返回）。
    """
    if not HK_SNAPSHOT_PER_SYMBOL_ENABLED:
        logger.info("[INFO] 港股 snapshot 未启用（HK_SNAPSHOT_PER_SYMBOL_ENABLED=false）")
        return {"enabled": False, "updated": 0, "failed": 0}

    from zplan_shared.models import SessionLocal, StockList, init_db
    init_db()

    as_of = latest_trade_date(market=HK_MARKET)
    if as_of is None:
        logger.warning("[WARN] 港股无行情数据，跳过 snapshot")
        return {"updated": 0, "failed": 0, "message": "无行情"}

    with SessionLocal() as session:
        codes = session.execute(
            select(StockList.ts_code).where(StockList.market == HK_MARKET)
        ).scalars().all()

    symbols = list(codes)
    if limit:
        symbols = symbols[:limit]

    updated = 0
    failed = 0
    base_interval = float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "3"))

    for idx, symbol in enumerate(symbols, 1):
        try:
            data = _fetch_hk_financial_indicator(symbol)
            if data:
                fields = _extract_snapshot_fields(data)
                upsert_hk_snapshot(symbol, as_of, fields)
                updated += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            logger.warning("[WARN] 港股 %s snapshot 失败: %s", symbol, exc)

        if idx % 50 == 0 or idx == len(symbols):
            logger.info(
                "[INFO] 港股 snapshot 进度 %s/%s ok=%s fail=%s",
                idx, len(symbols), updated, failed,
            )
        time.sleep(base_interval)

    stats = {"as_of": str(as_of), "updated": updated, "failed": failed}
    logger.info("[INFO] 港股 snapshot 完成: %s", stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import sys
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
        elif arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
    os.environ.setdefault("HK_SNAPSHOT_PER_SYMBOL_ENABLED", "true")
    # re-read after override
    import zplan_shared.config as _cfg
    _cfg.HK_SNAPSHOT_PER_SYMBOL_ENABLED = True
    run_hk_snapshot_update(limit=limit)
