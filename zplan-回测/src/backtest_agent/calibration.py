"""预测校准报告（文本 / JSON）。"""
from __future__ import annotations

import json
from typing import Any

from zplan_shared.pick_predictions import calibration_summary, list_outcomes


def format_calibration_report(summary: dict[str, Any]) -> str:
    if summary.get("count", 0) == 0:
        return summary.get("message", "无数据")

    lines = [
        f"# 选股预测价校准报告（{summary['horizon_days']} 交易日）",
        "",
        f"- 样本数：**{summary['count']}**",
        f"- 触及建议买价比例：**{summary['touch_rate']:.1%}**",
        f"- 买价偏差均值（%）：**{summary.get('mean_buy_gap_pct')}**（负=期内常跌破预测价）",
        f"- 目标价达成率：**{summary['target_hit_rate']:.1%}**",
        f"- 止损触发率：**{summary['stop_hit_rate']:.1%}**",
    ]
    if summary.get("mean_return_from_buy_pct") is not None:
        lines.append(f"- 触及买价后 horizon 收盘收益均值：**{summary['mean_return_from_buy_pct']}%**")

    lines.append("")
    lines.append("## 按价格来源")
    for row in summary.get("by_price_source") or []:
        lines.append(
            f"- `{row.get('price_source')}`：n={row.get('n')}，"
            f"触及率 {row.get('touch_rate', 0):.1%}，"
            f"均价差 {row.get('mean_gap_pct')}%"
        )

    hints = summary.get("optimization_hints") or []
    if hints:
        lines.extend(["", "## 优化建议（供选股 Agent 调参）"])
        for h in hints:
            lines.append(f"- {h}")

    return "\n".join(lines)


def build_calibration_report(*, horizon_days: int = 10) -> dict[str, Any]:
    summary = calibration_summary(horizon_days=horizon_days)
    return {
        "summary": summary,
        "markdown": format_calibration_report(summary),
        "recent_outcomes": list_outcomes(limit=20, horizon_days=horizon_days),
    }


def print_calibration(*, horizon_days: int = 10, as_json: bool = False) -> None:
    report = build_calibration_report(horizon_days=horizon_days)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(report["markdown"])
