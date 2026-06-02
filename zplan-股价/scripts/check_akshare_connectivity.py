#!/usr/bin/env python3
"""拉取前探测 AkShare 日线源（东财 / 新浪 / 腾讯）并给出修复建议。"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

import pandas as pd

from zplan_shared.data_sources import daily_provider, daily_provider_label, daily_source_tag
from zplan_shared.etl_akshare import (
    _fetch_stock_daily_hist_em,
    _fetch_stock_daily_hist_tx,
    get_akshare_version,
    probe_daily_em,
    probe_daily_sina,
    probe_daily_tx,
)


def _compare_ohlc(symbol: str, start: str, end: str) -> None:
    em = _fetch_stock_daily_hist_em(symbol, start_date=start)
    tx = _fetch_stock_daily_hist_tx(symbol, start, end)
    if em.empty or tx.empty:
        print("  对比跳过：某一源无数据")
        return
    em = em.rename(columns={"日期": "date", "收盘": "close"})
    tx = tx.rename(columns={"date": "date", "close": "close"})
    em["date"] = pd.to_datetime(em["date"]).dt.strftime("%Y-%m-%d")
    tx["date"] = pd.to_datetime(tx["date"]).dt.strftime("%Y-%m-%d")
    merged = em[["date", "close"]].merge(
        tx[["date", "close"]], on="date", suffixes=("_em", "_tx")
    )
    if merged.empty:
        print("  对比跳过：无重叠交易日")
        return
    merged["diff_pct"] = (merged["close_em"] - merged["close_tx"]).abs() / merged["close_tx"] * 100
    max_diff = merged["diff_pct"].max()
    mean_diff = merged["diff_pct"].mean()
    over_01 = (merged["diff_pct"] > 0.1).sum()
    print(f"  重叠 {len(merged)} 个交易日")
    print(f"  收盘价 |diff|% : 均值 {mean_diff:.4f}%  最大 {max_diff:.4f}%  >0.1% 共 {over_01} 天")
    if max_diff > 0.5:
        worst = merged.loc[merged["diff_pct"].idxmax()]
        print(
            f"  最大偏差日 {worst['date']}: EM={worst['close_em']:.4f} TX={worst['close_tx']:.4f}"
        )


def _print_troubleshooting(em_ok: bool) -> None:
    print("\n=== 排查步骤（东财不通时）===")
    print(f"  1. 升级 AkShare: ./scripts/upgrade_akshare.sh  （当前 {get_akshare_version()}）")
    print("  2. 东财接口: stock_zh_a_hist(period='daily') + stock_zh_a_spot_em")
    print("  3. 平替日线: AKSHARE_DAILY_PROVIDER=sina 或 tx，再 --realign-source")
    print("  4. 限流: 加大 AKSHARE_RATE_LIMIT_SECONDS；勿多线程猛刷东财")
    if not em_ok:
        print("  5. 东财仍失败可试: AKSHARE_EASTMONEY_DIRECT=false（走代理）或隔 30～60 分钟再试")


def main() -> None:
    parser = argparse.ArgumentParser(description="AkShare 日线源连通性自检")
    parser.add_argument("--symbol", default="000001", help="探测用股票代码，默认 000001")
    parser.add_argument("--days", type=int, default=10, help="探测区间日历天数，默认 10")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="东财、腾讯均可用时，对比重叠日收盘价差异",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="单次请求、不重试（验收脚本用）",
    )
    parser.add_argument(
        "--require",
        choices=("configured", "em", "tx", "sina", "any"),
        default="configured",
        help="退出码：configured=当前配置源；any=任一路可用",
    )
    args = parser.parse_args()

    end = pd.Timestamp.today()
    start = (end - timedelta(days=args.days)).strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    print(f"AkShare 版本: {get_akshare_version()}")
    print(f"配置日线源: {daily_provider_label()} ({daily_source_tag()})")
    print(f"探测标的: {args.symbol}  区间: {start} ~ {end_s}\n")

    em_ok, em_msg, em_n = probe_daily_em(args.symbol, start, end_s, quick=args.quick)
    sina_ok, sina_msg, sina_n = probe_daily_sina(args.symbol, start, end_s, quick=args.quick)
    tx_ok, tx_msg, tx_n = probe_daily_tx(args.symbol, start, end_s, quick=args.quick)

    print("=== 连通性 ===")
    print(
        f"  东财 (akshare_em):   {'✓' if em_ok else '✗'}  {em_msg}"
        + (f"  ({em_n} 行)" if em_ok else "")
    )
    print(
        f"  新浪 (akshare_sina): {'✓' if sina_ok else '✗'}  {sina_msg}"
        + (f"  ({sina_n} 行)" if sina_ok else "")
    )
    print(
        f"  腾讯 (akshare_tx):   {'✓' if tx_ok else '✗'}  {tx_msg}"
        + (f"  ({tx_n} 行)" if tx_ok else "")
    )

    print("\n=== 字段差异（入库后）===")
    print("  东财: OHLC + 成交量/涨跌幅/振幅/换手率 等（最全）")
    print("  新浪: OHLC + 成交量/成交额/换手率；涨跌幅通常需自行计算")
    print("  腾讯: OHLC + 成交额；成交量/涨跌幅/换手率 多为 NULL")
    print("  分时 Parquet 仍走东财，与日线 provider 无关")

    if args.compare and em_ok and tx_ok:
        print("\n=== OHLC 抽样对比（东财 vs 腾讯，前复权收盘）===")
        _compare_ohlc(args.symbol, start, end_s)

    print("\n=== 建议 ===")
    if em_ok:
        print("  保持 AKSHARE_DAILY_PROVIDER=em；限流时加大 AKSHARE_RATE_LIMIT_SECONDS。")
    elif sina_ok:
        print("  东财不可用，推荐 AKSHARE_DAILY_PROVIDER=sina（含成交量）后 --realign-source。")
    elif tx_ok:
        print("  可临时 AKSHARE_DAILY_PROVIDER=tx；缺成交量/涨跌幅时注意选股逻辑。")
    else:
        print("  三路均失败：先 upgrade_akshare.sh，再检查网络/代理。")

    _print_troubleshooting(em_ok)

    provider = daily_provider()
    ok = False
    if args.require == "em":
        ok = em_ok
    elif args.require == "tx":
        ok = tx_ok
    elif args.require == "sina":
        ok = sina_ok
    elif args.require == "any":
        ok = em_ok or sina_ok or tx_ok
    elif provider == "em":
        ok = em_ok
    elif provider == "sina":
        ok = sina_ok
    else:
        ok = tx_ok

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
