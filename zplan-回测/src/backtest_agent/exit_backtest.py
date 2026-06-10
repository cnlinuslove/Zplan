"""出场策略对比回测：同一批 pick 信号跑多套出场方案，产出对比报告。

用法::

    cd zplan-回测 && .venv/bin/python main.py exit-compare --run-id 151 --plans static,atr_trail_2x
    cd zplan-回测 && .venv/bin/python main.py exit-optimize --sweep
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.exit_strategy import (
    ExitPlan,
    ExitType,
    simulate_exit_for_pick,
)
from zplan_shared.exit_config import (
    ExitConfig,
    ExitSweepConfig,
    generate_sweep_plans,
    load_exit_config,
)
from zplan_shared.features import enrich_bars
from zplan_shared.market import get_bars
from zplan_shared.models import (
    PickEntry,
    PickRun,
    SessionLocal,
    init_db,
)

REPORT_DIR = Path(ZPLAN_ROOT).parent / "zplan-回测" / "backtest_review"


# ── 单 run 对比 ──────────────────────────────────────────────────


def run_exit_compare(
    run_id: int,
    plans: list[tuple[str, ExitPlan]],
    *,
    top_n: int = 10,
    output_md: bool = True,
) -> dict[str, Any]:
    """对指定 pick_run 跑多套出场方案，返回对比结果。

    Args:
        run_id: pick_runs.id。
        plans: list of (label, ExitPlan) tuples。
        top_n: 取前 N 只票。
        output_md: 是否写 Markdown 报告。

    Returns:
        {
            "run_id": int,
            "as_of": str,
            "top_n": int,
            "plans": {label: {metrics...}},
            "details": [{ts_code, name, entry_price, plans: {label: result}}],
            "best_plan": str,
            "report_path": str | None,
        }
    """
    init_db()
    with SessionLocal() as session:
        run = session.get(PickRun, run_id)
        if run is None:
            return {"ok": False, "message": f"run_id={run_id} 不存在"}
        as_of = run.trade_date_as_of
        as_of_str = str(as_of) if as_of else "?"

        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run_id)
            .order_by(PickEntry.rank_in_run, PickEntry.id)
            .limit(top_n)
        ).scalars().all()

    if not entries:
        return {"ok": False, "message": f"run_id={run_id} 无 pick 记录"}

    # ── 逐票模拟 ──
    details: list[dict[str, Any]] = []
    plan_returns: dict[str, list[float]] = {label: [] for label, _ in plans}
    plan_outcomes: dict[str, dict[str, int]] = {
        label: {"win": 0, "loss": 0, "flat": 0} for label, _ in plans
    }

    for e in entries:
        bars = get_bars(e.ts_code)
        if bars.empty:
            continue
        bars = enrich_bars(bars)

        entry_price = e.close_price or 0
        if entry_price <= 0:
            # 尝试从实际收盘价获取
            actual = _get_close_on_date(e.ts_code, as_of)
            entry_price = actual or 0
        if entry_price <= 0:
            continue

        # 入场日 = as_of + 1
        entry_date = as_of + timedelta(days=1) if as_of else date.today()

        row: dict[str, Any] = {
            "entry_id": e.id,
            "ts_code": e.ts_code,
            "name": e.name or e.ts_code,
            "rank": e.rank_in_run,
            "entry_price": round(entry_price, 2),
            "plans": {},
        }

        for plan_label, plan in plans:
            result = simulate_exit_for_pick(
                entry_price=entry_price,
                entry_date=entry_date,
                bars=bars,
                exit_plan=plan,
            )
            row["plans"][plan_label] = result
            ret = result.get("return_pct")
            if ret is not None:
                plan_returns[plan_label].append(ret)
                if ret > 0.5:
                    plan_outcomes[plan_label]["win"] += 1
                elif ret < -0.5:
                    plan_outcomes[plan_label]["loss"] += 1
                else:
                    plan_outcomes[plan_label]["flat"] += 1

        details.append(row)

    # ── 汇总指标 ──
    plan_metrics: dict[str, dict[str, Any]] = {}
    for label, returns in plan_returns.items():
        n = len(returns)
        if n == 0:
            plan_metrics[label] = {"n": 0, "win_rate": 0, "avg_return": 0, "error": "无数据"}
            continue
        wins = plan_outcomes[label]["win"]
        plan_metrics[label] = {
            "n": n,
            "win_rate": round(wins / n * 100, 1),
            "avg_return": round(statistics.mean(returns), 2),
            "median_return": round(statistics.median(returns), 2),
            "max_return": round(max(returns), 2),
            "min_return": round(min(returns), 2),
            "wins": wins,
            "losses": plan_outcomes[label]["loss"],
            "flats": plan_outcomes[label]["flat"],
        }
        # Sharpe（简化版）
        if n >= 3 and (stdev := statistics.stdev(returns)) > 0:
            plan_metrics[label]["sharpe"] = round(
                statistics.mean(returns) / stdev * math.sqrt(252 / 5), 2
            )
        else:
            plan_metrics[label]["sharpe"] = None

        # 计算止损触发率
        stops = sum(
            1 for d in details
            if d["plans"].get(label, {}).get("rule_type") in ("static_stop", "atr_trail", "trailing_stop", "ma_stop")
        )
        plan_metrics[label]["stop_rate"] = round(stops / n * 100, 1) if n > 0 else 0

    # 最佳方案
    best = max(plan_metrics.items(), key=lambda kv: kv[1].get("avg_return", -999))
    best_label = best[0] if best else "?"

    result: dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "as_of": as_of_str,
        "top_n": len(details),
        "plans": plan_metrics,
        "details": details,
        "best_plan": best_label,
    }

    # ── 写 Markdown 报告 ──
    if output_md and details:
        report_path = _write_compare_report(run_id, as_of_str, plans, plan_metrics, details, best_label)
        result["report_path"] = str(report_path)

    return result


# ── 参数 sweep ───────────────────────────────────────────────────


def run_exit_sweep(
    run_id: int,
    config: ExitConfig | None = None,
    *,
    top_n: int = 10,
    output_md: bool = True,
) -> dict[str, Any]:
    """对指定 run 跑参数网格搜索，找最优出场参数组合。

    Args:
        run_id: pick_runs.id。
        config: ExitConfig（含 sweep 配置）。None 则自动加载。
        top_n: 取前 N 只票。
        output_md: 是否写 Markdown 报告。

    Returns:
        对比结果字典 + report_path。
    """
    if config is None:
        config = load_exit_config()

    sweep_plans = generate_sweep_plans(config)
    if not sweep_plans:
        return {"ok": False, "message": "sweep 配置为空，请检查 strategy.yaml exit.optimization"}

    result = run_exit_compare(run_id, sweep_plans, top_n=top_n, output_md=output_md)

    # 附加 sweep 建议
    if result.get("ok") and result.get("plans"):
        # 找最优的非 baseline 方案
        non_baseline = {
            k: v for k, v in result["plans"].items()
            if "baseline" not in k and v.get("n", 0) > 0
        }
        if non_baseline:
            top5 = sorted(
                non_baseline.items(),
                key=lambda kv: (kv[1].get("avg_return", -999), kv[1].get("sharpe") or 0),
                reverse=True,
            )[:5]
            result["top_params"] = [
                {"plan": label, "avg_return": m["avg_return"], "sharpe": m["sharpe"]}
                for label, m in top5
            ]

    return result


# ── 内部 ───────────────────────────────────────────────────────────


def _write_compare_report(
    run_id: int,
    as_of: str,
    plans: list[tuple[str, ExitPlan]],
    metrics: dict[str, dict[str, Any]],
    details: list[dict[str, Any]],
    best_label: str,
) -> Path:
    """写 Markdown 对比报告。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"exit_compare_run{run_id}.md"

    lines: list[str] = []
    lines.append(f"# 出场策略对比 · run_id={run_id} · {as_of}")
    lines.append("")
    lines.append(f"**区间**: {as_of} → (模拟至出场)  ")
    lines.append(f"**股票数**: {len(details)}  ")
    lines.append(f"**方案数**: {len(plans)}  ")
    lines.append("")

    # 汇总表
    lines.append("## 方案汇总")
    lines.append("")
    header = "| 方案 | 样本 | 胜率 | 均收益 | 中位收益 | 最大 | 最小 | 夏普(估) |"
    sep = "|------|------|------|--------|----------|------|------|----------|"
    lines.append(header)
    lines.append(sep)
    for label, m in metrics.items():
        marker = " ★" if label == best_label else ""
        sharpe = f"{m.get('sharpe', '-'):.2f}" if m.get("sharpe") else "-"
        lines.append(
            f"| {label}{marker} | {m.get('n', 0)} | {m.get('win_rate', 0)}% | "
            f"{m.get('avg_return', 0):+.2f}% | {m.get('median_return', 0):+.2f}% | "
            f"{m.get('max_return', 0):+.2f}% | {m.get('min_return', 0):+.2f}% | {sharpe} |"
        )
    lines.append("")
    lines.append(f"★ 最优: **{best_label}**")
    lines.append("")

    # 逐票明细
    lines.append("## 逐票明细")
    lines.append("")
    plan_labels = [label for label, _ in plans]
    detail_header = "| 票名 | 入场价 | " + " | ".join(plan_labels) + " |"
    detail_sep = "|------|--------|" + "|".join(["------"] * len(plan_labels)) + "|"
    lines.append(detail_header)
    lines.append(detail_sep)

    for d in details:
        rets: list[str] = []
        for label in plan_labels:
            pr = d["plans"].get(label, {})
            ret = pr.get("return_pct")
            rule = pr.get("rule_type", "")
            if ret is not None:
                symbol = " ✓" if rule in ("static_take_profit", "partial_take_profit") else (
                    " §" if rule in ("static_stop", "trailing_stop", "atr_trail", "ma_stop") else ""
                )
                rets.append(f"{ret:+.1f}%{symbol}")
            else:
                rets.append("-")
        name = d.get("name", "?")
        ep = d.get("entry_price", 0)
        lines.append(f"| {name} | {ep:.2f} | " + " | ".join(rets) + " |")

    lines.append("")
    lines.append("✓ = 止盈触发  § = 止损触发  (空白 = 到期/强制离场)")
    lines.append("")
    lines.append(f"*报告生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}*")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _get_close_on_date(ts_code: str, as_of) -> float | None:
    """取某日真实收盘价。"""
    if as_of is None:
        return None
    bars = get_bars(ts_code)
    if bars.empty:
        return None
    idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
    bars = bars.copy()
    bars.index = idx
    on = bars[bars.index == pd.Timestamp(as_of)]
    if on.empty:
        return None
    return float(on["close"].iloc[-1])
