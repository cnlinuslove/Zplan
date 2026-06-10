#!/usr/bin/env python3
"""历史管道模拟：在过去日期上跑 v1 vs v2 真实 init-rule + TOP300 选股，对比前向收益。

零 LLM 成本——只比较规则分粗筛差异（LLM 处罚函数对两套选股相同）。

用法::

    cd zplan-回测 && .venv/bin/python scripts/simulate_historical_picks.py
    cd zplan-回测 && .venv/bin/python scripts/simulate_historical_picks.py --dates 2026-05-08 2026-05-15 2026-05-22 2026-06-05
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_SHARED_SRC = os.path.join(_REPO_ROOT, "zplan-共享", "src")
_PICK_SRC = os.path.join(_REPO_ROOT, "zplan-选股", "src")
if _SHARED_SRC not in sys.path:
    sys.path.insert(0, _SHARED_SRC)
if _PICK_SRC not in sys.path:
    sys.path.insert(0, _PICK_SRC)

from zplan_shared.db_engine import build_engine

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HORIZONS = [5, 10, 20]
TOP_N = 300

REVIEW_DIR = os.path.join(
    os.environ.get("ZPLAN_ROOT", os.path.join(_REPO_ROOT, "zplan-资讯")),
    "backtest_review",
)


def get_trade_dates_forward(trade_date: date, n_days: int) -> list[date]:
    """获取 trade_date 之后 N 个交易日。"""
    import pandas as pd
    engine = build_engine()
    sql = """
        SELECT DISTINCT trade_date FROM daily_prices
        WHERE trade_date > :d
        ORDER BY trade_date
        LIMIT :n
    """
    df = pd.read_sql_query(sql, engine, params={"d": trade_date, "n": n_days})
    return sorted(pd.to_datetime(df["trade_date"]).dt.date.tolist())


def get_close_on_date(ts_code: str, trade_date: date) -> float | None:
    """获取某只股票在某交易日的收盘价。"""
    import pandas as pd
    engine = build_engine()
    sql = """
        SELECT "close" FROM daily_prices
        WHERE ts_code = :code AND trade_date = :d
        LIMIT 1
    """
    df = pd.read_sql_query(sql, engine, params={"code": ts_code, "d": trade_date})
    if df.empty:
        return None
    return float(df["close"].iloc[0])


def simulate_one_date(
    as_of: date,
    use_v2: bool,
    top_n: int = TOP_N,
) -> dict[str, Any]:
    """在 as_of 日期上跑 init-rule + TOP N 选股，返回 picks 列表。

    不写 DB，不调 LLM。
    """
    from pick_agent.rule_universe import build_rule_scores_universe
    from pick_agent.strategy import load_strategy
    from zplan_shared.stock_rule_scores import top_rule_scores

    strat = load_strategy()
    label = "v2_reversal" if use_v2 else "v1_momentum"

    # Step 1: 跑 init-rule
    logger.info("[%s | %s] init-rule ...", as_of, label)
    result = build_rule_scores_universe(
        strategy=strat,
        skip_health_check=True,
        use_v2=use_v2,
        as_of=as_of,
    )

    if not result.get("ok"):
        return {"ok": False, "message": result.get("message", "失败"), "label": label, "as_of": str(as_of)}

    logger.info("[%s | %s] scored=%s stocks, table_total=%s", as_of, label, result["scored"], result["table_total"])

    # Step 2: 取 TOP N
    picks = top_rule_scores(
        trade_date_as_of=as_of,
        rule_version=strat.rule_version,
        top_n=top_n,
    )

    if not picks:
        return {"ok": False, "message": "top_rule_scores 返回空", "label": label, "as_of": str(as_of)}

    # 补充 ret_20d 从 features_json
    for p in picks:
        features = p.get("features") or {}
        if isinstance(features, str):
            import json
            try:
                features = json.loads(features)
            except Exception:
                features = {}
        p["_ret_20d"] = features.get("ret_20d")
        p["_high_60d_pct"] = features.get("high_60d_pct")

    # Step 3: 计算前向收益
    fwd_dates_all = get_trade_dates_forward(as_of, max(HORIZONS))
    close_cache: dict[tuple[str, date], float | None] = {}

    for h in HORIZONS:
        col = f"fwd_{h}d"
        if len(fwd_dates_all) >= h:
            fwd_date = fwd_dates_all[h - 1]
        else:
            fwd_date = None

        for p in picks:
            code = p["ts_code"]
            close0 = p.get("close")
            if close0 is None or close0 == 0:
                p[col] = None
                continue
            if fwd_date is None:
                p[col] = None
                continue

            cache_key = (code, fwd_date)
            if cache_key not in close_cache:
                close_cache[cache_key] = get_close_on_date(code, fwd_date)
            close_h = close_cache[cache_key]

            if close_h and close_h > 0:
                p[col] = round((close_h / float(close0) - 1) * 100, 2)
            else:
                p[col] = None

    n_with_fwd = sum(1 for p in picks if p.get("fwd_5d") is not None)
    logger.info("[%s | %s] TOP%s picks, %s with 5d fwd return", as_of, label, len(picks), n_with_fwd)

    return {
        "ok": True,
        "label": label,
        "as_of": str(as_of),
        "n_picks": len(picks),
        "n_with_fwd": n_with_fwd,
        "picks": picks,
    }


def compare_results(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """对比 v1 vs v2 在各日期的表现。"""
    comparison: dict[str, Any] = {
        "by_date": {},
        "aggregate": {},
    }

    # 按日期分组
    by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in results:
        if not r.get("ok"):
            continue
        d = r["as_of"]
        by_date[d][r["label"]] = r

    # 逐日期对比
    for d, variants in sorted(by_date.items()):
        date_comp: dict[str, Any] = {}
        for label, r in variants.items():
            picks = r.get("picks", [])
            metrics: dict[str, Any] = {
                "n_picks": len(picks),
                "n_with_fwd": r.get("n_with_fwd", 0),
            }
            for h in HORIZONS:
                col = f"fwd_{h}d"
                returns = [p[col] for p in picks if p.get(col) is not None]
                if returns:
                    arr = np.array(returns)
                    metrics[f"mean_{h}d"] = round(float(arr.mean()), 2)
                    metrics[f"median_{h}d"] = round(float(np.median(arr)), 2)
                    metrics[f"win_rate_{h}d"] = round(float((arr > 0).mean()) * 100, 1)
                    metrics[f"std_{h}d"] = round(float(arr.std(ddof=1)), 2)
                else:
                    metrics[f"mean_{h}d"] = None
                    metrics[f"median_{h}d"] = None
                    metrics[f"win_rate_{h}d"] = None
                    metrics[f"std_{h}d"] = None

            # ret_20d 和 high_60d_pct 分布
            ret20s = [p.get("_ret_20d") for p in picks if p.get("_ret_20d") is not None]
            h60s = [p.get("_high_60d_pct") for p in picks if p.get("_high_60d_pct") is not None]
            metrics["avg_ret_20d"] = round(float(np.mean(ret20s)), 1) if ret20s else None
            metrics["avg_high_60d_pct"] = round(float(np.mean(h60s)), 1) if h60s else None
            metrics["pct_high60_over_90"] = round(float((np.array(h60s) > 90).mean()) * 100, 1) if h60s else None

            date_comp[label] = metrics

        # v2 - v1 超额
        if "v2_reversal" in date_comp and "v1_momentum" in date_comp:
            excess: dict[str, Any] = {}
            for h in HORIZONS:
                v2_mean = date_comp["v2_reversal"].get(f"mean_{h}d")
                v1_mean = date_comp["v1_momentum"].get(f"mean_{h}d")
                if v2_mean is not None and v1_mean is not None:
                    excess[f"excess_{h}d"] = round(v2_mean - v1_mean, 2)
                else:
                    excess[f"excess_{h}d"] = None

            v2_wr = date_comp["v2_reversal"].get("win_rate_5d")
            v1_wr = date_comp["v1_momentum"].get("win_rate_5d")
            if v2_wr is not None and v1_wr is not None:
                excess["wr_delta_5d"] = round(v2_wr - v1_wr, 1)

            # 选股重叠度
            v2_codes = {p["ts_code"] for p in variants.get("v2_reversal", {}).get("picks", [])}
            v1_codes = {p["ts_code"] for p in variants.get("v1_momentum", {}).get("picks", [])}
            overlap = len(v2_codes & v1_codes)
            excess["overlap_count"] = overlap
            excess["overlap_pct"] = round(overlap / max(len(v2_codes), len(v1_codes)) * 100, 1) if max(len(v2_codes), len(v1_codes)) > 0 else 0

            date_comp["_excess"] = excess

        comparison["by_date"][d] = date_comp

    # 汇总
    agg: dict[str, Any] = {"v1_momentum": defaultdict(list), "v2_reversal": defaultdict(list)}
    for d, variants in comparison["by_date"].items():
        for label in ["v1_momentum", "v2_reversal"]:
            if label in variants:
                m = variants[label]
                for h in HORIZONS:
                    v = m.get(f"mean_{h}d")
                    if v is not None:
                        agg[label][f"mean_{h}d"].append(v)
                    v = m.get(f"win_rate_{h}d")
                    if v is not None:
                        agg[label][f"win_rate_{h}d"].append(v)
                v = m.get("avg_ret_20d")
                if v is not None:
                    agg[label]["avg_ret_20d"].append(v)

    agg_out: dict[str, Any] = {}
    for label in ["v1_momentum", "v2_reversal"]:
        entry: dict[str, Any] = {}
        for key, vals in agg[label].items():
            if vals:
                entry[f"avg_{key}"] = round(float(np.mean(vals)), 2)
                entry[f"n_dates_{key.split('_')[-1]}"] = len(vals)
        agg_out[label] = entry

    # 超额汇总
    excess_agg: dict[str, list[float]] = defaultdict(list)
    for d, variants in comparison["by_date"].items():
        exc = variants.get("_excess", {})
        for h in HORIZONS:
            v = exc.get(f"excess_{h}d")
            if v is not None:
                excess_agg[f"excess_{h}d"].append(v)

    for h in HORIZONS:
        vals = excess_agg[f"excess_{h}d"]
        if vals:
            agg_out["v2_vs_v1"] = agg_out.get("v2_vs_v1", {})
            agg_out["v2_vs_v1"][f"mean_excess_{h}d"] = round(float(np.mean(vals)), 2)
            agg_out["v2_vs_v1"][f"excess_positive_pct_{h}d"] = round(
                float((np.array(vals) > 0).mean()) * 100, 1
            )

    comparison["aggregate"] = agg_out
    return comparison


def generate_report(comparison: dict[str, Any]) -> str:
    """生成 Markdown 对比报告。"""
    lines = [
        "# v1 vs v2 历史管道模拟对比",
        "",
        "> 在每个历史日期上跑真实 `init-rule --as-of <date>` + TOP300 选股，"
        "用后续真实 K 线计算前向收益。零 LLM 成本。",
        "",
    ]

    agg = comparison.get("aggregate", {})
    v2_vs_v1 = agg.get("v2_vs_v1", {})

    lines.append("## 汇总：v2 vs v1 超额")
    lines.append("")
    lines.append("| 周期 | v1 均收益 | v2 均收益 | v2-v1 超额 | 超额胜率 | v1 胜率 | v2 胜率 |")
    lines.append("|------|----------|----------|-----------|---------|--------|--------|")

    for h in HORIZONS:
        v1_mean = agg.get("v1_momentum", {}).get(f"avg_mean_{h}d", "-")
        v2_mean = agg.get("v2_reversal", {}).get(f"avg_mean_{h}d", "-")
        excess = v2_vs_v1.get(f"mean_excess_{h}d", "-")
        excess_pct = v2_vs_v1.get(f"excess_positive_pct_{h}d", "-")
        v1_wr = agg.get("v1_momentum", {}).get("avg_win_rate_{h}d", "-")
        v2_wr = agg.get("v2_reversal", {}).get("avg_win_rate_{h}d", "-")

        lines.append(
            f"| {h}日 | {_f(v1_mean)} | {_f(v2_mean)} | {_f(excess)} | "
            f"{_p(excess_pct)} | {_p(v1_wr)} | {_p(v2_wr)} |"
        )
    lines.append("")

    # 特征对比
    v1_ret20 = agg.get("v1_momentum", {}).get("avg_avg_ret_20d", "-")
    v2_ret20 = agg.get("v2_reversal", {}).get("avg_avg_ret_20d", "-")
    lines.append(f"| 特征 | v1 | v2 |")
    lines.append(f"|------|----|----|")
    lines.append(f"| 平均 ret_20d | {_f(v1_ret20)} | {_f(v2_ret20)} |")
    lines.append("")

    # 逐日期明细
    lines.append("## 逐日期明细")
    lines.append("")

    for d, variants in sorted(comparison.get("by_date", {}).items()):
        exc = variants.get("_excess", {})
        lines.append(f"### {d}")
        lines.append("")
        lines.append(f"| 指标 | v1_momentum | v2_reversal | v2-v1 |")
        lines.append(f"|------|------------|------------|-------|")

        for h in HORIZONS:
            v1_m = variants.get("v1_momentum", {}).get(f"mean_{h}d", "-")
            v2_m = variants.get("v2_reversal", {}).get(f"mean_{h}d", "-")
            ex = exc.get(f"excess_{h}d", "-")
            lines.append(f"| {h}日均收益 | {_f(v1_m)} | {_f(v2_m)} | {_f(ex)} |")

        v1_wr = variants.get("v1_momentum", {}).get("win_rate_5d", "-")
        v2_wr = variants.get("v2_reversal", {}).get("win_rate_5d", "-")
        lines.append(f"| 5日胜率 | {_p(v1_wr)} | {_p(v2_wr)} | {_p(exc.get('wr_delta_5d', '-'))} |")

        v1_ret = variants.get("v1_momentum", {}).get("avg_ret_20d", "-")
        v2_ret = variants.get("v2_reversal", {}).get("avg_ret_20d", "-")
        lines.append(f"| 平均 ret_20d | {_f(v1_ret)} | {_f(v2_ret)} | - |")

        v1_h60 = variants.get("v1_momentum", {}).get("avg_high_60d_pct", "-")
        v2_h60 = variants.get("v2_reversal", {}).get("avg_high_60d_pct", "-")
        lines.append(f"| 平均 high_60d_pct | {_f(v1_h60)} | {_f(v2_h60)} | - |")

        lines.append(f"| 高位占比(>90%) | {_p(variants.get('v1_momentum', {}).get('pct_high60_over_90', '-'))} | {_p(variants.get('v2_reversal', {}).get('pct_high60_over_90', '-'))} | - |")
        lines.append(f"| 选股重叠 | - | - | {exc.get('overlap_count', '-')} 只 ({exc.get('overlap_pct', '-')}%) |")
        lines.append(f"| v1 picks | {variants.get('v1_momentum', {}).get('n_picks', '-')} | {variants.get('v2_reversal', {}).get('n_picks', '-')} | - |")
        lines.append("")

    # 结论
    lines.append("## 结论")
    lines.append("")

    mean_excess_5 = v2_vs_v1.get("mean_excess_5d")
    mean_excess_20 = v2_vs_v1.get("mean_excess_20d")
    excess_positive_5 = v2_vs_v1.get("excess_positive_pct_5d")

    if mean_excess_5 is not None:
        if mean_excess_5 > 1.0:
            lines.append(f"- ✅ v2 在 5 日周期显著优于 v1（超额 {mean_excess_5:+.2f}%，胜率 {excess_positive_5}%）")
        elif mean_excess_5 > 0:
            lines.append(f"- 📊 v2 在 5 日周期略优于 v1（超额 {mean_excess_5:+.2f}%，胜率 {excess_positive_5}%）")
        elif mean_excess_5 > -1.0:
            lines.append(f"- 📊 v2 在 5 日周期略逊于 v1（超额 {mean_excess_5:+.2f}%）")
        else:
            lines.append(f"- ⚠️ v2 在 5 日周期显著差于 v1（超额 {mean_excess_5:+.2f}%）")

    if mean_excess_20 is not None:
        if mean_excess_20 > 0:
            lines.append(f"- v2 在 20 日周期优于 v1（超额 {mean_excess_20:+.2f}%），长期反转因子生效")
        else:
            lines.append(f"- v2 在 20 日周期未优于 v1（超额 {mean_excess_20:+.2f}%）")

    lines.append("")
    lines.append("---")
    lines.append("*报告由 `simulate_historical_picks.py` 生成*")

    return "\n".join(lines)


def _f(v):
    if v is None or v == "-":
        return "-"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def _p(v):
    if v is None or v == "-":
        return "-"
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return str(v)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="历史管道模拟：v1 vs v2 真实选股对比")
    p.add_argument("--dates", type=str, nargs="+", default=None,
                   help="目标日期列表 YYYY-MM-DD（默认最近 4 个周五）")
    p.add_argument("--top-n", type=int, default=300)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.dates:
        target_dates = [date.fromisoformat(d) for d in args.dates]
    else:
        target_dates = [
            date(2026, 5, 8),
            date(2026, 5, 15),
            date(2026, 5, 22),
            date(2026, 6, 5),
        ]

    all_results: list[dict[str, Any]] = []

    for as_of in target_dates:
        for use_v2 in [False, True]:
            r = simulate_one_date(as_of, use_v2=use_v2, top_n=args.top_n)
            all_results.append(r)
            if not r.get("ok"):
                logger.warning("跳过 %s v2=%s: %s", as_of, use_v2, r.get("message"))

    comparison = compare_results(all_results)

    # 输出
    report = generate_report(comparison)
    md_path = os.path.join(REVIEW_DIR, "v1_vs_v2_pipeline_simulation.md")
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("报告已保存至 %s", md_path)

    # 控制台摘要
    agg = comparison.get("aggregate", {})
    v2v1 = agg.get("v2_vs_v1", {})
    print("\n" + "=" * 60)
    print("  v1 vs v2 真实管道模拟结果")
    print("=" * 60)
    for h in HORIZONS:
        exc = v2v1.get(f"mean_excess_{h}d", "-")
        pos = v2v1.get(f"excess_positive_pct_{h}d", "-")
        print(f"  {h}日超额: {_f(exc)}  胜率: {_p(pos)}")
    print(f"\n  完整报告: {md_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
