#!/usr/bin/env python3
"""排序策略对比实验：用已有 LLM 输出测试不同排序方式的前向收益。

零 LLM 成本——复用数据库中已存储的 LLM 简评结果。
对比 4 种策略：
  1. rule_only       — 纯规则分排序
  2. llm_primary     — 纯 LLM 分排序（现状）
  3. rule_filtered   — 规则分为主，LLM 只做风险扣分
  4. anti_enthusiasm — 规则分为主，LLM 热情过度时额外惩罚

用法::

    cd zplan-回测 && .venv/bin/python scripts/compare_ranking_strategies.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_SHARED_SRC = os.path.join(_REPO_ROOT, "zplan-共享", "src")
if _SHARED_SRC not in sys.path:
    sys.path.insert(0, _SHARED_SRC)

from zplan_shared.db_engine import build_engine

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

REVIEW_DIR = os.path.join(
    os.environ.get("ZPLAN_ROOT", os.path.join(_REPO_ROOT, "zplan-资讯")),
    "backtest_review",
)

TOP_N_VALUES = [5, 10, 20]  # 多个 TOP N 来比较


def load_picks_with_llm() -> pd.DataFrame:
    """加载所有有 LLM 简评 + 前向收益的 pick_entries。"""
    engine = build_engine()
    sql = """
        SELECT
            pe.id as entry_id,
            pe.ts_code,
            pe.name,
            pe.run_id,
            pr.trade_date_as_of,
            pr.run_kind,
            pe.rank_in_run,
            pe.rule_composite_score,
            pe.llm_composite_score,
            pe.final_composite_score,
            pe.recommendation,
            pe.verdict,
            pe.analysis_process_json,
            ple.return_from_close_pct
        FROM pick_entries pe
        JOIN pick_runs pr ON pe.run_id = pr.id
        JOIN pick_llm_evaluations ple ON ple.entry_id = pe.id
        WHERE ple.return_from_close_pct IS NOT NULL
          AND pr.run_kind = 'llm_top300'
          AND pe.llm_composite_score IS NOT NULL
        ORDER BY pr.trade_date_as_of, pe.rank_in_run
    """
    df = pd.read_sql_query(sql, engine)
    logger.info("加载 %s 条有 LLM + 前向收益的记录，覆盖 %s 个日期",
                len(df), df["trade_date_as_of"].nunique())
    return df


def extract_llm_fields(df: pd.DataFrame) -> pd.DataFrame:
    """从 analysis_process_json 提取 LLM 输出字段。

    优先从 llm_brief JSON 读取；若不存在则从 pick_entries 直接列推断。
    """
    risk_penalties = []
    positive_boosts = []
    conf_adjs = []

    for _, row in df.iterrows():
        try:
            ap = json.loads(row["analysis_process_json"]) if row["analysis_process_json"] else {}
        except (json.JSONDecodeError, TypeError):
            ap = {}
        brief = ap.get("llm_brief") or {}

        # 从 brief 读取，fallback 到直接列
        rp = brief.get("risk_penalty")
        if rp is None:
            # 推断：若 recommendation 是消极的，可能有处罚
            rec = str(row.get("recommendation") or "")
            if rec in ("谨慎", "回避"):
                rp = 5.0
            elif rec == "观望":
                rp = 2.0
            else:
                rp = 0.0
        risk_penalties.append(float(rp))

        pb = brief.get("positive_boost")
        if pb is None:
            rec = str(row.get("recommendation") or "")
            if rec in ("强烈关注", "积极关注"):
                pb = 3.0
            elif rec == "关注":
                pb = 1.0
            else:
                pb = 0.0
        positive_boosts.append(float(pb))

        ca = brief.get("confidence_adjustment")
        if ca is None:
            # 从 score_delta 推断
            delta = (float(row["llm_composite_score"] or 0) - float(row["rule_composite_score"] or 0))
            ca = max(-5.0, min(5.0, delta))
        conf_adjs.append(float(ca))

    df = df.copy()
    df["_risk_penalty"] = risk_penalties
    df["_positive_boost"] = positive_boosts
    df["_confidence_adj"] = conf_adjs
    return df


def score_rule_only(row) -> float:
    """纯规则分。"""
    return float(row["rule_composite_score"] or 0)


def score_llm_primary(row) -> float:
    """纯 LLM 分（现状）。"""
    return float(row["llm_composite_score"] or row["rule_composite_score"] or 0)


def score_rule_filtered(row) -> float:
    """规则分为主，LLM 只扣不加。"""
    rule = float(row["rule_composite_score"] or 0)
    risk_penalty = float(row["_risk_penalty"] or 0)
    # 只用风险扣分，不用正向加分
    return rule - risk_penalty


def score_anti_enthusiasm(row) -> float:
    """反热情：规则分 + 风险扣分 - 过度热情惩罚。

    LLM 抬分 > 3 或推荐包含「强烈」「积极」→ 额外扣 5 分。
    """
    rule = float(row["rule_composite_score"] or 0)
    risk_penalty = float(row["_risk_penalty"] or 0)
    confidence_adj = float(row["_confidence_adj"] or 0)
    rec = str(row.get("recommendation") or "")

    score = rule - risk_penalty

    # 过度热情惩罚
    over_enthusiasm = False
    if confidence_adj > 3:
        over_enthusiasm = True
    if any(w in rec for w in ["强烈关注", "积极关注", "推荐"]):
        over_enthusiasm = True

    if over_enthusiasm:
        score -= 5.0

    return score


STRATEGIES = {
    "rule_only": {"fn": score_rule_only, "desc": "纯规则分排序"},
    "llm_primary": {"fn": score_llm_primary, "desc": "纯LLM分排序（现状）"},
    "rule_filtered": {"fn": score_rule_filtered, "desc": "规则主+LLM风险扣分"},
    "anti_enthusiasm": {"fn": score_anti_enthusiasm, "desc": "规则主+反热情惩罚"},
}


def run_comparison(df: pd.DataFrame, top_n: int = 10) -> dict[str, Any]:
    """在每个日期上，用不同策略排序取 TOP N，计算前向收益。"""
    dates = sorted(df["trade_date_as_of"].unique())
    results: dict[str, Any] = {"by_date": {}, "aggregate": {}}

    for d in dates:
        day_df = df[df["trade_date_as_of"] == d].copy()
        if len(day_df) < top_n:
            continue

        date_str = str(d)
        results["by_date"][date_str] = {}

        for strat_name, strat_info in STRATEGIES.items():
            score_fn = strat_info["fn"]
            day_df["_score"] = day_df.apply(score_fn, axis=1)
            top = day_df.nlargest(top_n, "_score")

            returns = top["return_from_close_pct"].dropna()
            if len(returns) < top_n * 0.5:
                results["by_date"][date_str][strat_name] = {"error": "insufficient returns"}
                continue

            arr = returns.values
            results["by_date"][date_str][strat_name] = {
                "mean_return": round(float(np.mean(arr)), 2),
                "median_return": round(float(np.median(arr)), 2),
                "win_rate": round(float((arr > 0).mean()) * 100, 1),
                "n": len(arr),
            }

    # 汇总
    agg: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for d, strats in results["by_date"].items():
        for sname, sm in strats.items():
            if "mean_return" in sm:
                agg[sname]["mean_returns"].append(sm["mean_return"])
                agg[sname]["win_rates"].append(sm["win_rate"])

    for sname in STRATEGIES:
        mr = agg[sname].get("mean_returns", [])
        wr = agg[sname].get("win_rates", [])
        results["aggregate"][sname] = {
            "avg_mean_return": round(float(np.mean(mr)), 2) if mr else None,
            "median_mean_return": round(float(np.median(mr)), 2) if mr else None,
            "avg_win_rate": round(float(np.mean(wr)), 1) if wr else None,
            "n_dates": len(mr),
            "best_count": 0,  # filled below
        }

    # 计算每个日期哪个策略最好
    for d, strats in results["by_date"].items():
        best_strat = None
        best_return = -999
        for sname, sm in strats.items():
            if sm.get("mean_return", -999) > best_return:
                best_return = sm["mean_return"]
                best_strat = sname
        if best_strat:
            results["aggregate"][best_strat]["best_count"] += 1

    return results


def generate_report(results: dict[str, Any]) -> str:
    """生成 Markdown 对比报告。"""
    agg = results.get("aggregate", {})

    lines = [
        "# 排序策略对比实验",
        "",
        "> 复用数据库中已存储的 LLM 简评结果，对比 4 种排序策略的 TOP10 前向收益。",
        "> 零 LLM 成本——不调用 API。",
        "",
        "## 汇总",
        "",
        "| 策略 | 均收益 | 中位数收益 | 胜率 | 日期数 | 最佳次数 |",
        "|------|--------|----------|------|--------|---------|",
    ]

    # Sort by avg_mean_return
    sorted_strats = sorted(
        agg.items(),
        key=lambda x: x[1].get("avg_mean_return", -999) or -999,
        reverse=True,
    )

    for sname, sm in sorted_strats:
        desc = STRATEGIES[sname]["desc"]
        lines.append(
            f"| **{sname}** | {_f(sm.get('avg_mean_return'))} | "
            f"{_f(sm.get('median_mean_return'))} | "
            f"{_p(sm.get('avg_win_rate'))} | "
            f"{sm.get('n_dates', 0)} | "
            f"{sm.get('best_count', 0)} |"
        )
    lines.append(f"*{desc}*" if False else "")
    lines.append("")

    # 策略说明
    lines.append("## 策略说明")
    lines.append("")
    for sname, sinfo in STRATEGIES.items():
        lines.append(f"- **{sname}**: {sinfo['desc']}")
    lines.append("")

    # 逐日期
    lines.append("## 逐日期明细")
    lines.append("")

    for d in sorted(results.get("by_date", {}).keys()):
        strats = results["by_date"][d]
        lines.append(f"### {d}")
        lines.append("")
        lines.append("| 策略 | TOP10 均收益 | 胜率 |")
        lines.append("|------|------------|------|")

        sorted_day = sorted(strats.items(), key=lambda x: x[1].get("mean_return", -999) or -999, reverse=True)
        for sname, sm in sorted_day:
            if "error" in sm:
                lines.append(f"| {sname} | {sm['error']} | - |")
            else:
                best_mark = " ⭐" if sorted_day and sname == sorted_day[0][0] else ""
                lines.append(
                    f"| {sname}{best_mark} | {_f(sm.get('mean_return'))} | "
                    f"{_p(sm.get('win_rate'))} |"
                )
        lines.append("")

    # 结论
    best_strat = sorted_strats[0][0] if sorted_strats else None
    lines.append("## 结论")
    lines.append("")
    if best_strat:
        lines.append(f"- **最佳策略**: `{best_strat}`（{STRATEGIES[best_strat]['desc']}）")
        best_return = agg[best_strat].get("avg_mean_return", 0)
        llm_return = agg.get("llm_primary", {}).get("avg_mean_return", 0)
        if best_return and llm_return:
            improvement = best_return - llm_return
            lines.append(f"- 相对现状（llm_primary）改善: {improvement:+.2f}%")

    # 如果 anti_enthusiasm 是最佳，说明 LLM 热情确实是反向指标
    anti = agg.get("anti_enthusiasm", {})
    llm_p = agg.get("llm_primary", {})
    if anti.get("avg_mean_return") and llm_p.get("avg_mean_return"):
        if anti["avg_mean_return"] > llm_p["avg_mean_return"]:
            lines.append(f"- ✅ **反热情惩罚有效**：anti_enthusiasm 比 llm_primary 高 {anti['avg_mean_return'] - llm_p['avg_mean_return']:+.2f}%")
            lines.append(f"- 结论：LLM 的乐观情绪是最强反向指标，应当系统性压制")
        else:
            lines.append(f"- ❌ 反热情惩罚无效：anti_enthusiasm 未优于 llm_primary")

    lines.append("")
    lines.append("---")
    lines.append("*报告由 `compare_ranking_strategies.py` 生成*")

    return "\n".join(lines)


def _f(v):
    if v is None:
        return "-"
    return f"{float(v):+.2f}%"


def _p(v):
    if v is None:
        return "-"
    return f"{float(v):.1f}%"


def main() -> None:
    df = load_picks_with_llm()
    if df.empty:
        logger.error("无可用数据")
        sys.exit(1)

    df = extract_llm_fields(df)
    results = run_comparison(df)

    report = generate_report(results)
    md_path = os.path.join(REVIEW_DIR, "ranking_strategy_comparison.md")
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("报告已保存至 %s", md_path)

    # 控制台摘要
    agg = results.get("aggregate", {})
    print("\n" + "=" * 60)
    print("  排序策略对比（TOP10）")
    print("=" * 60)
    for sname in ["rule_only", "llm_primary", "rule_filtered", "anti_enthusiasm"]:
        sm = agg.get(sname, {})
        print(f"  {sname:<20} 均收益: {_f(sm.get('avg_mean_return'))}  "
              f"胜率: {_p(sm.get('avg_win_rate'))}  "
              f"最佳: {sm.get('best_count', 0)}/{sm.get('n_dates', 0)}")
    print(f"\n  报告: {md_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
