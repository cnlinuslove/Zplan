#!/usr/bin/env python3
"""查看 zplan.db 与分时 Parquet 拉取结果。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from zplan_shared.config import PARQUET_ROOT, ZPLAN_ROOT
from zplan_shared.data_sources import daily_provider_label, daily_source_tag


def _print_connectivity_hint() -> None:
    """轻量提示：完整探测请运行 check_akshare_connectivity.py。"""
    from datetime import timedelta

    import pandas as pd

    from zplan_shared.etl_akshare import probe_daily_em, probe_daily_sina, probe_daily_tx

    end = pd.Timestamp.today()
    start = (end - timedelta(days=5)).strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    em_ok, _, _ = probe_daily_em("000001", start, end_s, quick=True)
    sina_ok, _, _ = probe_daily_sina("000001", start, end_s, quick=True)
    tx_ok, _, _ = probe_daily_tx("000001", start, end_s, quick=True)
    tag = daily_source_tag()
    configured_ok = (
        em_ok
        if tag == "akshare_em"
        else sina_ok
        if tag == "akshare_sina"
        else tx_ok
    )
    if not configured_ok:
        print("⚠ 当前配置日线源不可用，建议:")
        print("  ./scripts/upgrade_akshare.sh")
        print("  python scripts/check_akshare_connectivity.py --quick")
        if not em_ok and sina_ok:
            print("  （新浪可用: AKSHARE_DAILY_PROVIDER=sina）")
        elif not em_ok and tx_ok:
            print("  （腾讯可用: AKSHARE_DAILY_PROVIDER=tx）")
        print()


def main() -> None:
    db = ZPLAN_ROOT / "zplan.db"
    if not db.exists():
        print(f"数据库不存在: {db}")
        sys.exit(1)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print(f"数据根: {ZPLAN_ROOT}")
    print(f"数据库: {db}")
    print(f"配置日线源: {daily_provider_label()} ({daily_source_tag()})")
    _print_connectivity_hint()
    print()

    c.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ts_code), MIN(trade_date), MAX(trade_date) FROM daily_prices"
    )
    total, stocks, dmin, dmax = c.fetchone()
    print("=== daily_prices ===")
    print(f"  行数: {total:,}  股票数: {stocks}  日期: {dmin} ~ {dmax}")

    c.execute("SELECT source, COUNT(*) FROM daily_prices GROUP BY source ORDER BY COUNT(*) DESC")
    print("  按来源:")
    sources = c.fetchall()
    for row in sources:
        print(f"    {row[0]}: {row[1]:,}")
    if len(sources) > 1:
        print("  ⚠ 日线存在多种 source，请 --realign-source 或统一 AKSHARE_DAILY_PROVIDER 后重拉")
    elif len(sources) == 1 and sources[0][0] != daily_source_tag():
        print(f"  ⚠ 与当前配置 {daily_source_tag()} 不一致，建议 --realign-source")

    c.execute(
        """
        SELECT ts_code,
               COUNT(*) AS n,
               MIN(trade_date),
               MAX(trade_date),
               SUM(CASE WHEN source='akshare_em' THEN 1 ELSE 0 END) AS em_rows,
               ROUND(100.0 * SUM(CASE WHEN volume IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS vol_pct,
               ROUND(100.0 * SUM(CASE WHEN pct_chg IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_pct
        FROM daily_prices
        GROUP BY ts_code
        ORDER BY ts_code
        """
    )
    print("\n  各股票:")
    for row in c.fetchall():
        print(
            f"    {row['ts_code']}  bars={row['n']:>4}  {row[2]}~{row[3]}  "
            f"em_rows={row['em_rows']}  vol%={row['vol_pct']}  pct%={row['pct_pct']}"
        )

    c.execute(
        """
        SELECT ts_code, trade_date, close, volume, pct_chg, turnover_rate, source
        FROM daily_prices
        ORDER BY ts_code, trade_date DESC
        LIMIT 8
        """
    )
    print("\n  样例（每票最新 1 条，最多 8 行）:")
    seen: set[str] = set()
    for row in c.fetchall():
        if row["ts_code"] in seen:
            continue
        seen.add(row["ts_code"])
        print(
            f"    {row['ts_code']} {row['trade_date']} close={row['close']} "
            f"vol={row['volume']} pct={row['pct_chg']} src={row['source']}"
        )

    intraday_root = PARQUET_ROOT / "intraday"
    print(f"\n=== 分时 Parquet ({intraday_root}) ===")
    if not intraday_root.exists():
        print("  (目录不存在)")
    else:
        try:
            import pandas as pd
        except ImportError:
            print("  需要 pandas")
        else:
            files = sorted(intraday_root.rglob("*.parquet"))
            if not files:
                print("  (无文件)")
            for path in files:
                df = pd.read_parquet(path)
                rel = path.relative_to(PARQUET_ROOT)
                tmin = df["bar_time"].min() if "bar_time" in df.columns and len(df) else "-"
                tmax = df["bar_time"].max() if "bar_time" in df.columns and len(df) else "-"
                print(f"  {rel}  rows={len(df)}  {tmin} ~ {tmax}")

    conn.close()


if __name__ == "__main__":
    main()
