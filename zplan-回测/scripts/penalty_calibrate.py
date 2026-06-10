#!/usr/bin/env python3
"""处罚权重实证校准：分析每个 risk_flag / positive_flag 与实际前向收益的关系。

对每个 flag，计算：命中率、条件收益（有 flag vs 无）、收益差、建议处罚/奖励。

用法::

    cd zplan-回测 && .venv/bin/python scripts/penalty_calibrate.py
    cd zplan-回测 && .venv/bin/python scripts/penalty_calibrate.py --min-samples 5 --output results/calibration
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np

# 路径
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

# ── 所有已知 flag ──
ALL_RISK_FLAGS = [
    "追高风险(涨幅过高)",
    "量价背离(缩量上涨)",
    "接近阶段高点",
    "超买区域(KDJ/RSI)",
    "监管/减持风险",
    "基本面恶化",
    "题材退潮",
]

ALL_POSITIVE_FLAGS = [
    "多题材催化",
    "资讯催化",
    "温和放量上涨",
    "买价可成交",
]


def load_calibration_data() -> list[dict[str, Any]]:
    """从数据库加载所有有 LLM 评估数据的 pick。

    Returns:
        [{ts_code, run_id, run_kind, trade_date_as_of, rule_score, llm_score,
          risk_flags, positive_flags, risk_penalty, positive_boost, confidence_adjustment,
          recommendation, return_from_close_pct, return_from_buy_pct, verdict}]
    """
    engine = build_engine()
    sql = """
        SELECT
            pe.ts_code,
            pe.run_id,
            pr.run_kind,
            pr.trade_date_as_of,
            pe.rule_composite_score,
            pe.llm_composite_score,
            pe.analysis_process_json,
            ple.return_from_close_pct,
            ple.verdict,
            ple.failure_tags_json
        FROM pick_entries pe
        JOIN pick_runs pr ON pe.run_id = pr.id
        LEFT JOIN pick_llm_evaluations ple ON ple.entry_id = pe.id
        WHERE pe.analysis_process_json IS NOT NULL
          AND json_extract(pe.analysis_process_json, '$.llm_brief.risk_flags') IS NOT NULL
        ORDER BY pr.trade_date_as_of, pe.rank_in_run
    """
    import pandas as pd
    logger.info("加载 pick_entries + pick_llm_evaluations ...")
    df = pd.read_sql_query(sql, engine)

    results: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            ap = json.loads(row["analysis_process_json"]) if row["analysis_process_json"] else {}
        except (json.JSONDecodeError, TypeError):
            ap = {}
        brief = ap.get("llm_brief") or {}

        risk_flags = list(brief.get("risk_flags") or [])
        # "无明显风险" 不算真正的 flag
        risk_flags = [f for f in risk_flags if f != "无明显风险"]

        positive_flags = list(brief.get("positive_flags") or [])
        positive_flags = [f for f in positive_flags if f != "无明显催化"]

        results.append({
            "ts_code": row["ts_code"],
            "run_id": int(row["run_id"]),
            "run_kind": row["run_kind"],
            "trade_date_as_of": str(row["trade_date_as_of"]) if row["trade_date_as_of"] else None,
            "rule_score": float(row["rule_composite_score"]) if row["rule_composite_score"] is not None and not pd.isna(row["rule_composite_score"]) else None,
            "llm_score": float(row["llm_composite_score"]) if row["llm_composite_score"] is not None and not pd.isna(row["llm_composite_score"]) else None,
            "risk_flags": risk_flags,
            "positive_flags": positive_flags,
            "risk_penalty": float(brief.get("risk_penalty", 0)),
            "positive_boost": float(brief.get("positive_boost", 0)),
            "confidence_adjustment": float(brief.get("confidence_adjustment", 0)),
            "recommendation": brief.get("recommendation", ""),
            "return_from_close_pct": float(row["return_from_close_pct"]) if row["return_from_close_pct"] is not None and not pd.isna(row["return_from_close_pct"]) else None,
            "verdict": row["verdict"] or "",
        })

    logger.info("加载完成: %s 条记录（有 LLM 简评 + 评估数据）", len(results))
    return results


def analyze_flag_impact(
    data: list[dict[str, Any]],
    min_samples: int = 5,
) -> dict[str, Any]:
    """分析每个 flag 对前向收益的影响。

    对有前向收益的子集，计算每个 flag 的：
    - 命中率（在所有 pick 中的出现比例）
    - 有条件收益（有该 flag 的 pick 平均收益）
    - 无条件收益（无该 flag 的 pick 平均收益）
    - 收益差（条件 - 无条件）
    - 信息比（收益差 / 合并标准差）
    - 建议处罚（风险 flag）或建议奖励（正向 flag）

    Returns:
        {risk_flags: {flag: {...}}, positive_flags: {flag: {...}}, summary: {...}}
    """
    # 有前向收益的子集
    with_return = [d for d in data if d["return_from_close_pct"] is not None]
    logger.info("有前向收益: %s/%s 条", len(with_return), len(data))

    if len(with_return) < min_samples:
        logger.warning("样本量不足（%s < %s），无法做有意义的校准", len(with_return), min_samples)
        return {"risk_flags": {}, "positive_flags": {}, "summary": {"error": "样本量不足"}}

    all_returns = np.array([d["return_from_close_pct"] for d in with_return])
    baseline_mean = float(np.mean(all_returns))
    baseline_std = float(np.std(all_returns, ddof=1))

    def _flag_stats(flag: str, flag_list_key: str) -> dict[str, Any]:
        """计算单个 flag 的统计量。"""
        has_flag = [d for d in with_return if flag in d[flag_list_key]]
        no_flag = [d for d in with_return if flag not in d[flag_list_key]]

        n_has = len(has_flag)
        n_no = len(no_flag)
        hit_rate = n_has / len(with_return) * 100 if with_return else 0

        if n_has < min_samples:
            return {
                "hit_count": n_has,
                "no_hit_count": n_no,
                "hit_rate_pct": round(hit_rate, 1),
                "mean_return_with_flag": None,
                "mean_return_without_flag": None,
                "return_delta": None,
                "insufficient_samples": True,
            }

        ret_with = np.array([d["return_from_close_pct"] for d in has_flag])
        ret_without = np.array([d["return_from_close_pct"] for d in no_flag])

        mean_with = float(np.mean(ret_with))
        mean_without = float(np.mean(ret_without))
        delta = mean_with - mean_without

        # 合并标准差
        pooled_std = np.sqrt(
            (np.var(ret_with, ddof=1) * (n_has - 1) + np.var(ret_without, ddof=1) * (n_no - 1))
            / (n_has + n_no - 2)
        ) if n_has > 1 and n_no > 1 else baseline_std

        ir = delta / pooled_std if pooled_std > 0 else 0.0

        # 建议处罚/奖励：收益差 * 缩放因子
        # 缩放逻辑：如果 flag 让收益降低 2%，建议罚 2 分；降低 5%，建议罚 5 分
        # 但有上限（最多罚/奖 10 分），且有统计显著性门槛
        abs_delta = abs(delta)
        if abs_delta < 0.5:
            suggested = 0.0  # 收益差 < 0.5%，不值得罚
        elif abs_delta < 1.0:
            suggested = round(abs_delta * 1.5, 1)
        else:
            suggested = round(min(abs_delta * 1.0, 10.0), 1)

        return {
            "hit_count": n_has,
            "no_hit_count": n_no,
            "hit_rate_pct": round(hit_rate, 1),
            "mean_return_with_flag": round(mean_with, 2),
            "mean_return_without_flag": round(mean_without, 2),
            "return_delta": round(delta, 2),
            "information_ratio": round(ir, 3),
            "suggested_penalty": round(suggested, 1) if delta < 0 else 0.0,
            "suggested_boost": round(suggested, 1) if delta > 0 else 0.0,
            "is_significant": abs(ir) > 0.1 and n_has >= min_samples,
            "insufficient_samples": False,
        }

    risk_results = {}
    for flag in ALL_RISK_FLAGS:
        risk_results[flag] = _flag_stats(flag, "risk_flags")

    positive_results = {}
    for flag in ALL_POSITIVE_FLAGS:
        positive_results[flag] = _flag_stats(flag, "positive_flags")

    # ── 汇总统计 ──
    n_pass = sum(1 for d in with_return if d["verdict"] == "pass")
    n_fail = sum(1 for d in with_return if d["verdict"] == "fail")

    pass_returns = [d["return_from_close_pct"] for d in with_return if d["verdict"] == "pass"]
    fail_returns = [d["return_from_close_pct"] for d in with_return if d["verdict"] == "fail"]

    # 计算运行中的处罚指标
    risk_penalties = [d["risk_penalty"] for d in with_return]
    positive_boosts = [d["positive_boost"] for d in with_return]
    conf_adjs = [d["confidence_adjustment"] for d in with_return]

    summary = {
        "n_total": len(data),
        "n_with_return": len(with_return),
        "n_pass": n_pass,
        "n_fail": n_fail,
        "pass_rate_pct": round(n_pass / len(with_return) * 100, 1) if with_return else 0,
        "baseline_mean_return": round(baseline_mean, 2),
        "baseline_std_return": round(baseline_std, 2),
        "mean_pass_return": round(float(np.mean(pass_returns)), 2) if pass_returns else None,
        "mean_fail_return": round(float(np.mean(fail_returns)), 2) if fail_returns else None,
        "avg_risk_penalty": round(float(np.mean(risk_penalties)), 2) if risk_penalties else 0,
        "avg_positive_boost": round(float(np.mean(positive_boosts)), 2) if positive_boosts else 0,
        "avg_confidence_adjustment": round(float(np.mean(conf_adjs)), 2) if conf_adjs else 0,
    }

    return {
        "risk_flags": risk_results,
        "positive_flags": positive_results,
        "summary": summary,
    }


# ── Markdown 报告 ──────────────────────────────────────

def generate_report(analysis: dict[str, Any]) -> str:
    """生成 Markdown 校准报告。"""
    today_str = date.today().strftime("%Y-%m-%d")
    summary = analysis.get("summary", {})
    risk_flags = analysis.get("risk_flags", {})
    positive_flags = analysis.get("positive_flags", {})

    lines = [
        f"# 处罚权重实证校准报告",
        f"",
        f"> 生成日期: {today_str}  |  基于 {summary.get('n_with_return', 0)} 条有前向收益的 LLM 简评记录",
        f"",
        f"## 一、数据概览",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 有 LLM 简评的总记录 | {summary.get('n_total', 0)} |",
        f"| 有前向收益的记录 | {summary.get('n_with_return', 0)} |",
        f"| Pass / Fail | {summary.get('n_pass', 0)} / {summary.get('n_fail', 0)} |",
        f"| Pass 率 | {summary.get('pass_rate_pct', 0)}% |",
        f"| 全样本均收益 | {_fmt_pct(summary.get('baseline_mean_return'))} |",
        f"| Pass 均收益 | {_fmt_pct(summary.get('mean_pass_return'))} |",
        f"| Fail 均收益 | {_fmt_pct(summary.get('mean_fail_return'))} |",
        f"| 平均风险处罚 | {summary.get('avg_risk_penalty', 0):.1f} |",
        f"| 平均正向奖励 | {summary.get('avg_positive_boost', 0):.1f} |",
        f"| 平均置信调整 | {summary.get('avg_confidence_adjustment', 0):.1f} |",
        f"",
    ]

    # ── 风险 flag ──
    lines.append("## 二、风险 Flag 实证分析")
    lines.append("")
    lines.append("> 如果 flag 条件收益 < 无条件收益（收益差为负）→ flag 有效，建议维持或加强处罚。")
    lines.append("> 如果收益差为正 → flag 无效，建议取消或降低处罚。")
    lines.append("")
    lines.append("| 风险 Flag | 命中数 | 命中率 | 有 flag 收益 | 无 flag 收益 | 收益差 | IR | 当前处罚 | 建议处罚 | 结论 |")
    lines.append("|----------|--------|--------|------------|------------|--------|-----|---------|---------|------|")

    for flag in ALL_RISK_FLAGS:
        d = risk_flags.get(flag, {})
        if not d or d.get("insufficient_samples"):
            lines.append(f"| {flag} | {d.get('hit_count', 0)} | - | - | - | - | - | - | - | 样本不足 |")
            continue

        current = _get_current_penalty(flag, "risk")
        suggested = d.get("suggested_penalty", 0)
        delta = d.get("return_delta", 0)
        ir = d.get("information_ratio", 0)

        if suggested > 0 and abs(ir) > 0.1:
            if suggested > current:
                conclusion = f"⚠️ 加强处罚 {current}→{suggested}"
            elif suggested < current:
                conclusion = f"🔽 减轻处罚 {current}→{suggested}"
            else:
                conclusion = "✅ 维持"
        elif suggested == 0 and delta < 0 and abs(ir) > 0.1:
            conclusion = "✅ 有效（处罚合理）"
        elif suggested == 0 and delta >= 0:
            conclusion = "❌ 无效！可取消处罚"
        else:
            conclusion = "📊 证据不足"

        lines.append(
            f"| {flag} | {d['hit_count']} | {d['hit_rate_pct']}% | "
            f"{_fmt_pct(d.get('mean_return_with_flag'))} | "
            f"{_fmt_pct(d.get('mean_return_without_flag'))} | "
            f"{_fmt_pct(delta)} | {ir:+.3f} | "
            f"{current:.1f} | {suggested:.1f} | {conclusion} |"
        )
    lines.append("")

    # ── 正向 flag ──
    lines.append("## 三、正向 Flag 实证分析")
    lines.append("")
    lines.append("> 如果 flag 条件收益 > 无条件收益（收益差为正）→ flag 有效，建议维持或加强奖励。")
    lines.append("")
    lines.append("| 正向 Flag | 命中数 | 命中率 | 有 flag 收益 | 无 flag 收益 | 收益差 | IR | 当前奖励 | 建议奖励 | 结论 |")
    lines.append("|----------|--------|--------|------------|------------|--------|-----|---------|---------|------|")

    for flag in ALL_POSITIVE_FLAGS:
        d = positive_flags.get(flag, {})
        if not d or d.get("insufficient_samples"):
            lines.append(f"| {flag} | {d.get('hit_count', 0)} | - | - | - | - | - | - | - | 样本不足 |")
            continue

        current = _get_current_penalty(flag, "positive")
        suggested = d.get("suggested_boost", 0)
        delta = d.get("return_delta", 0)
        ir = d.get("information_ratio", 0)

        if suggested > 0 and abs(ir) > 0.1:
            if suggested > current:
                conclusion = f"⬆️ 加强奖励 {current}→{suggested}"
            else:
                conclusion = "✅ 维持"
        elif suggested == 0 and delta > 0 and abs(ir) > 0.1:
            conclusion = "✅ 有效（奖励合理）"
        elif suggested == 0 and delta <= 0:
            conclusion = "❌ 无效！可取消奖励"
        else:
            conclusion = "📊 证据不足"

        lines.append(
            f"| {flag} | {d['hit_count']} | {d['hit_rate_pct']}% | "
            f"{_fmt_pct(d.get('mean_return_with_flag'))} | "
            f"{_fmt_pct(d.get('mean_return_without_flag'))} | "
            f"{_fmt_pct(delta)} | {ir:+.3f} | "
            f"{current:.1f} | {suggested:.1f} | {conclusion} |"
        )
    lines.append("")

    # ── 建议的 calibration YAML ──
    lines.append("## 四、建议的 penalty_weights")
    lines.append("")
    lines.append("```yaml")
    lines.append("penalty_weights:")
    lines.append("  risk_flags:")
    for flag in ALL_RISK_FLAGS:
        d = risk_flags.get(flag, {})
        suggested = d.get("suggested_penalty", _get_current_penalty(flag, "risk"))
        current = _get_current_penalty(flag, "risk")
        if not d or d.get("insufficient_samples"):
            suggested = current
        lines.append(f"    {flag}: {suggested:.1f}  # 当前 {current:.1f}, 命中 {d.get('hit_count', 0)} 次")
    lines.append("  positive_flags:")
    for flag in ALL_POSITIVE_FLAGS:
        d = positive_flags.get(flag, {})
        suggested = d.get("suggested_boost", _get_current_penalty(flag, "positive"))
        current = _get_current_penalty(flag, "positive")
        if not d or d.get("insufficient_samples"):
            suggested = current
        lines.append(f"    {flag}: {suggested:.1f}  # 当前 {current:.1f}, 命中 {d.get('hit_count', 0)} 次")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append(f"*报告由 `penalty_calibrate.py` 自动生成*")

    return "\n".join(lines)


def generate_calibration_yaml(analysis: dict[str, Any]) -> str:
    """生成可直接复制到 strategy.yaml 的 calibration YAML。"""
    risk_flags = analysis.get("risk_flags", {})
    positive_flags = analysis.get("positive_flags", {})

    lines = ["# 自动校准的处罚权重 — 由 penalty_calibrate.py 生成", "penalty_weights:"]
    lines.append("  risk_flags:")
    for flag in ALL_RISK_FLAGS:
        d = risk_flags.get(flag, {})
        suggested = d.get("suggested_penalty", _get_current_penalty(flag, "risk"))
        if not d or d.get("insufficient_samples"):
            suggested = _get_current_penalty(flag, "risk")
        lines.append(f"    {flag}: {suggested:.1f}")
    lines.append("  positive_flags:")
    for flag in ALL_POSITIVE_FLAGS:
        d = positive_flags.get(flag, {})
        suggested = d.get("suggested_boost", _get_current_penalty(flag, "positive"))
        if not d or d.get("insufficient_samples"):
            suggested = _get_current_penalty(flag, "positive")
        lines.append(f"    {flag}: {suggested:.1f}")
    return "\n".join(lines)


def _get_current_penalty(flag: str, kind: str) -> float:
    """获取当前 strategy.yaml 中的处罚/奖励值。"""
    if kind == "risk":
        defaults = {
            "追高风险(涨幅过高)": 5.0,
            "量价背离(缩量上涨)": 4.0,
            "接近阶段高点": 3.0,
            "超买区域(KDJ/RSI)": 3.0,
            "监管/减持风险": 6.0,
            "基本面恶化": 5.0,
            "题材退潮": 4.0,
        }
        return defaults.get(flag, 3.0)
    else:
        defaults = {
            "多题材催化": 2.0,
            "资讯催化": 2.0,
            "温和放量上涨": 2.0,
            "买价可成交": 1.0,
        }
        return defaults.get(flag, 2.0)


def _fmt_pct(v):
    if v is None:
        return "-"
    return f"{v:+.2f}%"


# ── Main ───────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="处罚权重实证校准")
    p.add_argument("--min-samples", type=int, default=5, help="每个 flag 最少样本数（默认 5）")
    p.add_argument("--output", type=str, default=None, help="输出目录")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    out_dir = args.output or REVIEW_DIR
    os.makedirs(out_dir, exist_ok=True)

    # 加载数据
    data = load_calibration_data()
    if not data:
        logger.error("无可用数据")
        sys.exit(1)

    # 分析
    analysis = analyze_flag_impact(data, min_samples=args.min_samples)

    # 生成报告
    md_path = os.path.join(out_dir, "calibration_report.md")
    report = generate_report(analysis)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("校准报告已保存至 %s", md_path)

    # 生成 YAML
    yaml_path = os.path.join(out_dir, "calibration_weights.yaml")
    yaml_content = generate_calibration_yaml(analysis)
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)
    logger.info("校准 YAML 已保存至 %s", yaml_path)

    # 控制台摘要
    summary = analysis.get("summary", {})
    risk_flags = analysis.get("risk_flags", {})
    positive_flags = analysis.get("positive_flags", {})

    print("\n" + "=" * 60)
    print("  处罚权重实证校准")
    print("=" * 60)
    print(f"  样本: {summary.get('n_with_return', 0)} 条")
    print(f"  Pass 率: {summary.get('pass_rate_pct', 0)}%")
    print(f"  Pass 均收益: {_fmt_pct(summary.get('mean_pass_return'))}")
    print(f"  Fail 均收益: {_fmt_pct(summary.get('mean_fail_return'))}")
    print()
    print("  风险 Flag 有效性:")
    for flag in ALL_RISK_FLAGS:
        d = risk_flags.get(flag, {})
        if d and not d.get("insufficient_samples"):
            delta = d.get("return_delta", 0)
            ir = d.get("information_ratio", 0)
            suggested = d.get("suggested_penalty", 0)
            current = _get_current_penalty(flag, "risk")
            status = "✅" if delta < 0 else "❌"
            print(f"    {status} {flag}: Δ={_fmt_pct(delta)}, IR={ir:+.3f}, 处罚 {current:.0f}→{suggested:.0f}")
    print()
    print("  正向 Flag 有效性:")
    for flag in ALL_POSITIVE_FLAGS:
        d = positive_flags.get(flag, {})
        if d and not d.get("insufficient_samples"):
            delta = d.get("return_delta", 0)
            suggested = d.get("suggested_boost", 0)
            current = _get_current_penalty(flag, "positive")
            status = "✅" if delta > 0 else "❌"
            print(f"    {status} {flag}: Δ={_fmt_pct(delta)}, 奖励 {current:.0f}→{suggested:.0f}")
    print(f"\n  报告: {md_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
