"""回填 daily_snapshot 历史数据 — 从百度估值 API 拉近五年 PE/PB/市值。

用法：
    cd zplan-股价 && .venv/bin/python scripts/backfill_snapshot_history.py

时间估算：每只股票 3 个 API 调用 × 0.4s throttle ≈ 1.2s/stock
3000 只 ≈ 60 分钟；5000 只 ≈ 100 分钟。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

# 添加共享库路径
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "zplan-共享" / "src"))

from zplan_shared.http_client import configure_akshare_http, throttle
from zplan_shared.models import DailySnapshot, SessionLocal, StockList, init_db
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')


def _float_or_none(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _mv_to_yuan(value: float | None) -> float | None:
    """百度市值单位为亿元 → 转换为元。"""
    if value is None or pd.isna(value):
        return None
    v = float(value)
    if v <= 0:
        return None
    if v < 1e6:
        return v * 1e8
    return v


def pull_stock_history(symbol: str) -> dict[str, pd.DataFrame]:
    """拉取一只股票的 PE/PB/市值历史数据。

    Returns {"pe_ttm": DataFrame, "pb": DataFrame, "total_mv": DataFrame}
    每个 DataFrame 有 date 和 value 列。
    """
    configure_akshare_http()
    import akshare as ak

    results = {}
    for indicator, key in [
        ("市盈率(TTM)", "pe_ttm"),
        ("市净率", "pb"),
        ("总市值", "total_mv"),
    ]:
        try:
            df = ak.stock_zh_valuation_baidu(
                symbol=symbol, indicator=indicator, period="近五年"
            )
            if not df.empty:
                df = df.rename(columns={"date": "trade_date", "value": key})
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
                results[key] = df[["trade_date", key]]
        except Exception as exc:
            logger.debug("%s %s 拉取失败: %s", symbol, key, exc)
        throttle(float(os.getenv("SNAPSHOT_BAIDU_INTERVAL", "0.4")))

    return results


def merge_indicators(pe_df, pb_df, mv_df) -> pd.DataFrame:
    """合并 PE/PB/市值三个 DataFrame → 统一宽表。"""
    dfs = []
    for df in [pe_df, pb_df, mv_df]:
        if df is not None and not df.empty:
            dfs.append(df.set_index("trade_date"))

    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, axis=1).reset_index()
    merged["ts_code"] = None  # placeholder, will be set
    return merged


def backfill_stocks(
    symbols: list[str],
    *,
    limit: int | None = None,
    resume_from: int = 0,
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict:
    """批量回填股票估值历史数据。

    Parameters
    ----------
    symbols : 股票代码列表
    limit : 限制数量（None=全部）
    resume_from : 从第几个开始（断点续跑）
    batch_size : 每 N 只股票提交一次 DB
    dry_run : 只测试前 5 只，不写入 DB
    """
    init_db()

    if limit:
        symbols = symbols[:limit]
    symbols = symbols[resume_from:]

    stats = {"total": len(symbols), "success": 0, "fail": 0, "rows": 0}

    batch_rows: list[dict] = []

    for idx, symbol in enumerate(symbols):
        if (idx + 1) % 10 == 0:
            elapsed = time.time() - stats.get("_start", time.time())
            rate = (idx + 1) / max(elapsed, 1)
            eta = (len(symbols) - idx - 1) / max(rate, 1e-8) / 60
            logger.info(
                "[%d/%d] %.1f stocks/min, ETA %.0f min, ok=%d fail=%d",
                idx + 1, len(symbols), rate * 60, eta, stats["success"], stats["fail"],
            )

        try:
            results = pull_stock_history(symbol)
            if not results:
                stats["fail"] += 1
                continue

            # 合并三个指标
            pe_df = results.get("pe_ttm")
            pb_df = results.get("pb")
            mv_df = results.get("total_mv")

            merged = None
            for df in [pe_df, pb_df, mv_df]:
                if df is not None and not df.empty:
                    if merged is None:
                        merged = df.copy()
                    else:
                        merged = merged.merge(df, on="trade_date", how="outer")

            if merged is None or merged.empty:
                stats["fail"] += 1
                continue

            merged["ts_code"] = symbol
            merged["ps_ttm"] = None
            merged["turnover_rate"] = None
            merged["circ_mv"] = None
            merged["source"] = "akshare_baidu_backfill"

            for _, row in merged.iterrows():
                batch_rows.append({
                    "ts_code": str(row["ts_code"]),
                    "trade_date": row["trade_date"],
                    "pe_ttm": _float_or_none(row.get("pe_ttm")),
                    "pb": _float_or_none(row.get("pb")),
                    "ps_ttm": None,
                    "total_mv": _mv_to_yuan(_float_or_none(row.get("total_mv"))),
                    "circ_mv": None,
                    "turnover_rate": None,
                    "source": "akshare_baidu_backfill",
                })

            stats["success"] += 1
            stats["rows"] += len(merged)

        except Exception as exc:
            stats["fail"] += 1
            logger.warning("[WARN] %s 失败: %s", symbol, exc)
            throttle(1.0)

        # 批量写入
        if len(batch_rows) >= batch_size * 50:
            if not dry_run:
                _upsert_batch(batch_rows)
            batch_rows = []

    # 最终写入
    if batch_rows and not dry_run:
        _upsert_batch(batch_rows)

    logger.info("回填完成: %s", stats)
    return stats


def _upsert_batch(rows: list[dict]) -> int:
    """批量 upsert daily_snapshot。"""
    if not rows:
        return 0
    ingested_at = datetime.utcnow()
    for r in rows:
        r["ingested_at"] = ingested_at

    total = 0
    with SessionLocal() as session:
        for i in range(0, len(rows), 100):
            chunk = rows[i : i + 100]
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="回填 daily_snapshot 历史 PE/PB/市值")
    parser.add_argument("--limit", type=int, default=None, help="限制股票数")
    parser.add_argument("--resume-from", type=int, default=0, help="断点续跑起始位置")
    parser.add_argument("--dry-run", action="store_true", help="仅测试前 5 只")
    parser.add_argument("--codes", nargs="*", default=None, help="指定股票代码（空格分隔）")
    args = parser.parse_args()

    init_db()

    # 获取股票列表
    if args.codes:
        symbols = [c.strip().zfill(6) for c in args.codes]
    else:
        with SessionLocal() as session:
            symbols = sorted([
                r[0] for r in session.execute(select(StockList.ts_code)).all()
            ])
        logger.info("共 %d 只股票待处理", len(symbols))
        # 优先处理有日线数据的股票
        with SessionLocal() as session:
            from zplan_shared.models import DailyPrice
            active_codes = {
                r[0] for r in session.execute(
                    select(DailyPrice.ts_code).distinct()
                ).all()
            }
        # 交集：有 stock_list 且有 daily_prices 的
        symbols = sorted(active_codes & set(symbols))
        logger.info("有日线数据的: %d 只", len(symbols))

    if args.dry_run:
        symbols = symbols[:5]
        logger.info("Dry run: %s", symbols)

    stats = backfill_stocks(
        symbols,
        limit=args.limit,
        resume_from=args.resume_from,
        dry_run=args.dry_run,
    )
    logger.info("最终: %s", stats)


if __name__ == "__main__":
    stats = {"_start": time.time()}
    main()
