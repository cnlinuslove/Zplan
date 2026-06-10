#!/usr/bin/env python3
"""完整历史管道模拟：在历史日期跑 init-rule + llm-top，用后续 K 线验证。

这是零 look-ahead 偏见的真实回测——所有数据截断到 as_of 日期。
LLM 成本：每个日期 ~$0.30（100 只 × 批量模式）。

用法::

    cd zplan-选股 && ../zplan-回测/.venv/bin/python ../zplan-回测/scripts/simulate_full_pipeline.py
    cd zplan-选股 && ../zplan-回测/.venv/bin/python ../zplan-回测/scripts/simulate_full_pipeline.py \
        --dates 2026-05-08 2026-05-15 2026-05-22 --top 100
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

# 路径
_THIS_FILE = os.path.abspath(__file__)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_FILE)))
_PICK_SRC = os.path.join(_REPO_ROOT, "zplan-选股", "src")
_SHARED_SRC = os.path.join(_REPO_ROOT, "zplan-共享", "src")
if _PICK_SRC not in sys.path:
    sys.path.insert(0, _PICK_SRC)
if _SHARED_SRC not in sys.path:
    sys.path.insert(0, _SHARED_SRC)

from zplan_shared.db_engine import build_engine

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HORIZONS = [5, 10, 20]
REVIEW_DIR = os.path.join(
    os.environ.get("ZPLAN_ROOT", os.path.join(_REPO_ROOT, "zplan-资讯")),
    "backtest_review",
)


def get_forward_close(code: str, as_of: date, horizon_days: int) -> float | None:
    """获取 as_of 之后第 N 个交易日的收盘价。"""
    engine = build_engine()
    sql = """
        SELECT "close" FROM daily_prices
        WHERE ts_code = :code AND trade_date > :d
        ORDER BY trade_date
        LIMIT 1 OFFSET :off
    """
    df = pd.read_sql_query(sql, engine, params={"code": code, "d": as_of, "off": horizon_days - 1})
    if df.empty or df["close"].iloc[0] is None:
        return None
    return float(df["close"].iloc[0])


def simulate_one_date(
    as_of: date,
    top_n: int = 100,
    variant_label: str | None = None,
    use_v2: bool = False,
) -> dict[str, Any]:
    """在 as_of 日期上跑完整 init-rule + llm-top 管道。

    Returns:
        {ok, as_of, n_picks, picks: [{ts_code, name, close, rule_score, llm_score,
          recommendation, risk_flags, positive_flags, confidence_adj,
          fwd_5d, fwd_10d, fwd_20d}]}
    """
    from pick_agent.rule_universe import build_rule_scores_universe
    from pick_agent.llm_top300 import run_llm_top_from_rule_scores
    from pick_agent.strategy import load_strategy

    strat = load_strategy()
    label = variant_label or ("v2" if use_v2 else "v1")

    # Step 1: init-rule
    logger.info("[%s | %s] init-rule ...", as_of, label)
    init_result = build_rule_scores_universe(
        strategy=strat,
        skip_health_check=True,
        use_v2=use_v2,
        as_of=as_of,
    )
    if not init_result.get("ok"):
        return {"ok": False, "message": init_result.get("message", "init-rule失败"),
                "as_of": str(as_of), "label": label}

    logger.info("[%s | %s] scored=%s stocks", as_of, label, init_result["table_total"])

    # Step 2: llm-top（含 LLM 调用）
    logger.info("[%s | %s] llm-top %s ...", as_of, label, top_n)
    try:
        llm_result = run_llm_top_from_rule_scores(
            top_n=top_n,
            strategy=strat,
            trade_date_as_of=as_of,
            deepen=True,
            use_llm=True,
            persist=False,  # 不写 DB
            variant_label=variant_label,
        )
    except Exception as e:
        logger.error("[%s | %s] LLM 调用失败: %s", as_of, label, e)
        return {"ok": False, "message": f"LLM失败: {e}", "as_of": str(as_of), "label": label}

    if not llm_result.get("ok"):
        return {"ok": False, "message": llm_result.get("message", "llm-top失败"),
                "as_of": str(as_of), "label": label}

    picks = llm_result.get("picks", [])
    logger.info("[%s | %s] got %s picks", as_of, label, len(picks))

    # Step 3: 前向收益
    for p in picks:
        close0 = p.get("close")
        code = p.get("ts_code", "")
        for h in HORIZONS:
            col = f"fwd_{h}d"
            if close0 and close0 > 0:
                close_h = get_forward_close(code, as_of, h)
                if close_h and close_h > 0:
                    p[col] = round((close_h / float(close0) - 1) * 100, 2)
                else:
                    p[col] = None
            else:
                p[col] = None

    n_with_fwd = sum(1 for p in picks if p.get("fwd_5d") is not None)
    logger.info("[%s | %s] %s/%s with 5d fwd return", as_of, label, n_with_fwd, len(picks))

    return {
        "ok": True,
        "as_of": str(as_of),
        "label": label,
        "n_picks": len(picks),
        "n_with_fwd": n_with_fwd,
        "picks": picks,
        "llm_usage": llm_result.get("llm_usage"),
    }


def compare_runs(results: list[dict[str, Any]], top_n: int = 10) -> dict[str, Any]:
    """对比多次模拟的 TOP N 前向收益。"""
    comparison: dict[str, Any] = {"by_date": {}, "aggregate": {}}

    by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in results:
        if not r.get("ok"):
            continue
        by_date[r["as_of"]][r["label"]] = r

    for d, variants in sorted(by_date.items()):
        date_comp: dict[str, Any] = {}
        for label, r in variants.items():
            picks = r.get("picks", [])
            # 按 final_composite_score 排序取 TOP N
            sorted_picks = sorted(picks, key=lambda x: x.get("final_composite_score") or 0, reverse=True)[:top_n]
            metrics: dict[str, Any] = {"n_picks": len(sorted_picks)}
            for h in HORIZONS:
                col = f"fwd_{h}d"
                returns = [p[col] for p in sorted_picks if p.get(col) is not None]
                if returns:
                    arr = np.array(returns)
                    metrics[f"mean_{h}d"] = round(float(arr.mean()), 2)
                    metrics[f"win_rate_{h}d"] = round(float((arr > 0).mean()) * 100, 1)
                else:
                    metrics[f"mean_{h}d"] = None
                    metrics[f"win_rate_{h}d"] = None

            ret20s = [p.get("ret_20d") for p in sorted_picks if p.get("ret_20d") is not None]
            metrics["avg_ret_20d"] = round(float(np.mean(ret20s)), 1) if ret20s else None
            date_comp[label] = metrics

        comparison["by_date"][d] = date_comp

    # 汇总
    agg: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for d, variants in comparison["by_date"].items():
        for label, m in variants.items():
            for h in HORIZONS:
                v = m.get(f"mean_{h}d")
                if v is not None:
                    agg[label][f"mean_{h}d"].append(v)
                v = m.get(f"win_rate_{h}d")
                if v is not None:
                    agg[label][f"win_rate_{h}d"].append(v)

    for label in agg:
        entry = {}
        for key, vals in agg[label].items():
            if vals:
                entry[f"avg_{key}"] = round(float(np.mean(vals)), 2)
        comparison["aggregate"][label] = entry

    return comparison


def generate_report(comparison: dict[str, Any], top_n: int) -> str:
    """生成 Markdown 报告。"""
    agg = comparison.get("aggregate", {})
    labels = sorted(agg.keys())

    lines = [
        f"# 完整历史管道模拟报告",
        "",
        f"> init-rule + llm-top（LLM 简评）→ TOP{top_n} 前向收益验证",
        f"> 所有数据截断到 as_of 日期，零 look-ahead 偏见",
        "",
        "## 汇总",
        "",
    ]

    # 表头
    h_labels = [f"{h}日均收益" for h in HORIZONS] + [f"{h}日胜率" for h in HORIZONS]
    lines.append("| 策略 | " + " | ".join(h_labels) + " | 日期数 |")
    lines.append("|------|" + "|".join(["------" for _ in h_labels]) + "|--------|")

    for label in labels:
        entry = agg[label]
        vals = []
        for h in HORIZONS:
            vals.append(_f(entry.get(f"avg_mean_{h}d")))
        for h in HORIZONS:
            vals.append(_p(entry.get(f"avg_win_rate_{h}d")))
        n_dates = len(comparison.get("by_date", {}))
        lines.append(f"| {label} | " + " | ".join(vals) + f" | {n_dates} |")
    lines.append("")

    # 逐日期
    lines.append("## 逐日期明细")
    for d in sorted(comparison.get("by_date", {}).keys()):
        variants = comparison["by_date"][d]
        lines.append(f"\n### {d}")
        lines.append("")
        lines.append("| 策略 | " + " | ".join([f"{h}日均收益" for h in HORIZONS]) + " |")
        lines.append("|------|" + "|".join(["------" for _ in HORIZONS]) + "|")
        for label in sorted(variants.keys()):
            m = variants[label]
            vs = [_f(m.get(f"mean_{h}d")) for h in HORIZONS]
            lines.append(f"| {label} | " + " | ".join(vs) + " |")
    lines.append("")

    lines.append("---")
    lines.append("*报告由 `simulate_full_pipeline.py` 生成*")
    return "\n".join(lines)


def _f(v):
    if v is None: return "-"
    return f"{float(v):+.2f}%"


def _p(v):
    if v is None: return "-"
    return f"{float(v):.1f}%"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="完整历史管道模拟（含 LLM）")
    p.add_argument("--dates", type=str, nargs="+",
                   default=["2026-05-08", "2026-05-15", "2026-05-22"],
                   help="目标日期 YYYY-MM-DD")
    p.add_argument("--top", type=int, default=100, help="LLM 简评数量（默认 100）")
    p.add_argument("--show-top", type=int, default=10, help="报告中展示 TOP N（默认 10）")
    p.add_argument("--dry-run", action="store_true", help="仅 init-rule，不调 LLM")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    target_dates = [date.fromisoformat(d) for d in args.dates]
    all_results: list[dict[str, Any]] = []

    for as_of in target_dates:
        r = simulate_one_date(as_of, top_n=args.top, variant_label="v1_rule_filtered")
        all_results.append(r)
        if not r.get("ok"):
            logger.warning("[%s] 失败: %s", as_of, r.get("message"))

    comparison = compare_runs(all_results, top_n=args.show_top)
    report = generate_report(comparison, args.show_top)

    md_path = os.path.join(REVIEW_DIR, "full_pipeline_simulation.md")
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    # 控制台摘要
    agg = comparison.get("aggregate", {})
    print("\n" + "=" * 60)
    print(f"  完整管道模拟（LLM 简评 TOP{args.show_top}）")
    print("=" * 60)
    for label, entry in agg.items():
        print(f"  {label}:")
        for h in HORIZONS:
            print(f"    {h}日: 均收益 {_f(entry.get(f'avg_mean_{h}d'))}  胜率 {_p(entry.get(f'avg_win_rate_{h}d'))}")
    print(f"\n  报告: {md_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
