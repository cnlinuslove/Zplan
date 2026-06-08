"""A/B 历史回放：同一历史日期跑多个策略变体，即时对比 fail_rate。

解决核心痛点：「每次只测一个配置，等 3~5 天才能验证」→「一天内用历史数据
得出统计显著结论」。

用法::

    # 单日对比
    python main.py ab-backtest --variants baseline,strict --as-of 2026-05-21

    # 历史区间（每个周五）
    python main.py ab-backtest --variants baseline,strict,value \\
        --from 2026-01-01 --to 2026-05-31 --horizon 5

    # 从 YAML 文件加载变体定义
    python main.py ab-backtest --variant-file variants.yaml --from 2026-01-01

变体定义（variants.yaml）::

    variants:
      baseline:
        description: "当前配置（max_ret_20d=5%, llm_weight=0.75）"
      strict_momentum:
        description: "收紧动量过滤：max_ret_20d=3%"
        overrides:
          filters:
            max_ret_20d: 3.0
      value_tilt:
        description: "提高财务权重到 25%"
        overrides:
          weights:
            financial: 0.25
            technical: 0.47

实现方式：每个变体生成临时 strategy.yaml → subprocess 调用 zplan-选股 main.py →
直接调用 zplan_shared.pick_llm_eval.evaluate_llm_run 验证（历史数据立即可用）。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.market import latest_panel_trade_date
from zplan_shared.models import init_db
from zplan_shared.pick_llm_eval import evaluate_llm_run

logger = logging.getLogger(__name__)

MONOREPO_ROOT = ZPLAN_ROOT.parent if (ZPLAN_ROOT.parent / "zplan-选股").is_dir() else ZPLAN_ROOT
PICK_ROOT = MONOREPO_ROOT / "zplan-选股"
STRATEGY_PATH = PICK_ROOT / "config" / "strategy.yaml"


def _pick_python() -> Path:
    p = PICK_ROOT / ".venv" / "bin" / "python"
    return p if p.is_file() else Path(sys.executable)


def _fridays_in_range(start: date, end: date) -> list[date]:
    """区间内所有周五（A 股交易日近似）。"""
    result: list[date] = []
    d = start
    while d <= end:
        if d.weekday() == 4:
            result.append(d)
        d += timedelta(days=1)
    return result


def _deep_merge(base: dict, overrides: dict) -> dict:
    """递归合并 overrides 到 base（修改 base 原地，返回 base）。"""
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _build_variant_strategy_yaml(variant_name: str, overrides: dict[str, Any]) -> Path:
    """基于当前 strategy.yaml + overrides 生成临时策略文件。

    支持深层覆盖：weights、filters、ranking、scan、risk_filters、
    diversification、signals 等任意 strategy.yaml 字段。

    Returns:
        临时文件路径（调用方负责在完成后删除）。
    """
    base = yaml.safe_load(STRATEGY_PATH.read_text(encoding="utf-8"))

    # 唯一 rule_version = 原版本 + variant 名
    base["rule_version"] = f"{base.get('rule_version', 'unknown')}::{variant_name}"

    # 深层合并所有覆盖项
    _deep_merge(base, overrides)

    # 写到临时文件
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix=f"zplan_ab_{variant_name}_",
        delete=False, encoding="utf-8",
    )
    yaml.safe_dump(base, tmp, allow_unicode=True, default_flow_style=False)
    tmp.close()
    return Path(tmp.name)


def _run_pick(cmd: str, *extra: str, timeout: int = 1800) -> dict[str, Any]:
    """通过 subprocess 调用 zplan-选股 main.py。"""
    proc = subprocess.run(
        [str(_pick_python()), str(PICK_ROOT / "main.py"), cmd, *extra],
        cwd=str(PICK_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "stdout_tail": (proc.stdout or "")[-3000:],
        "stderr_tail": (proc.stderr or "")[-1500:],
    }


def _latest_llm_run_id() -> int | None:
    """最近一次 LLM 选股的 run_id（scan 或 llm_top300）。"""
    from sqlalchemy import desc, select
    from zplan_shared.models import PickRun, SessionLocal

    init_db()
    with SessionLocal() as session:
        run = session.execute(
            select(PickRun)
            .where(PickRun.run_kind.in_(["llm_top300", "scan"]), PickRun.llm_enabled.is_(True))
            .order_by(desc(PickRun.id))
            .limit(1)
        ).scalar_one_or_none()
        return int(run.id) if run else None


def _run_one_variant(
    variant_name: str,
    as_of: date,
    *,
    overrides: dict[str, Any] | None = None,
    top_n: int = 100,
    batch_size: int = 10,
) -> dict[str, Any]:
    """对单个变体在指定日期跑 init-rule → llm-top → verify。"""
    overrides = overrides or {}
    tmp_yaml: Path | None = None

    try:
        if overrides:
            tmp_yaml = _build_variant_strategy_yaml(variant_name, overrides)
            strategy_arg = str(tmp_yaml)
        else:
            strategy_arg = str(STRATEGY_PATH)

        # Step 1: init-rule
        init_args = ["init-rule", "--strategy", strategy_arg, "--skip-health-check"]
        init_args += ["--as-of", str(as_of)]
        init_r = _run_pick(*init_args)
        if not init_r["ok"]:
            return {
                "variant": variant_name, "ok": False,
                "error": f"init-rule 失败: {init_r['stderr_tail'][:200]}", "step": "init-rule",
            }

        # Step 2: llm-top
        llm_args = [
            "llm-top",
            "--strategy", strategy_arg,
            "--as-of", str(as_of),
            "--top", str(top_n),
            "--batch-size", str(batch_size),
            "--variant", variant_name,
        ]
        llm_r = _run_pick(*llm_args, timeout=3600)
        if not llm_r["ok"]:
            return {
                "variant": variant_name, "ok": False,
                "error": f"llm-top 失败: {llm_r['stderr_tail'][:200]}", "step": "llm-top",
            }

        run_id = _latest_llm_run_id()
        if not run_id:
            return {
                "variant": variant_name, "ok": False,
                "error": "llm-top 未生成 run_id", "step": "llm-top",
            }

        # Step 3: verify（历史数据立即可用）
        eval_r = evaluate_llm_run(run_id=run_id, top_n=min(top_n, 20), horizon_days=5)
        summary = eval_r.get("summary") or {}
        entries = eval_r.get("entries") or []

        # 计算 forward 收益（比 pass/fail 更有区分度）
        fwd_returns = [e.get("return_from_close_pct") for e in entries if e.get("return_from_close_pct") is not None]
        import statistics
        mean_fwd = round(statistics.mean(fwd_returns), 2) if fwd_returns else None
        median_fwd = round(statistics.median(fwd_returns), 2) if fwd_returns else None
        fwd_pos = sum(1 for x in fwd_returns if x > 0)

        return {
            "variant": variant_name,
            "ok": True,
            "run_id": run_id,
            "as_of": str(as_of),
            "fail_rate": summary.get("fail_rate"),
            "fail_count": summary.get("fail"),
            "pass_count": summary.get("pass"),
            "pending_count": summary.get("pending"),
            "mean_fwd_return": mean_fwd,
            "median_fwd_return": median_fwd,
            "fwd_positive": fwd_pos,
            "fwd_total": len(fwd_returns),
            "tag_counts": eval_r.get("tag_counts") or {},
        }
    finally:
        if tmp_yaml and tmp_yaml.exists():
            tmp_yaml.unlink()


def run_ab_backtest(
    *,
    variants: dict[str, dict[str, Any]],
    as_of: date | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    top_n: int = 100,
    horizon_days: int = 5,
    batch_size: int = 10,
) -> dict[str, Any]:
    """A/B 历史回放主入口。

    Args:
        variants: {name: {description, overrides: {filters, weights, ...}}}
        as_of: 单日模式
        from_date / to_date: 区间模式（取每周五）
        top_n: 每个变体选股数
        horizon_days: 验证期（交易日）
        batch_size: LLM 批大小
    """
    init_db()

    # 确定测试日期
    if as_of:
        test_dates = [as_of]
    elif from_date and to_date:
        test_dates = _fridays_in_range(from_date, to_date)
        if not test_dates:
            return {"ok": False, "message": f"{from_date} → {to_date} 无周五"}
    else:
        latest = latest_panel_trade_date(min_symbols=1000)
        if not latest:
            return {"ok": False, "message": "无可用截面日期"}
        test_dates = [latest]

    logger.info(
        "A/B 回放: %s 变体 × %s 日期 (%s → %s)",
        len(variants), len(test_dates), test_dates[0], test_dates[-1],
    )

    # 逐日期 × 逐变体跑
    all_results: list[dict[str, Any]] = []
    total = len(test_dates) * len(variants)
    n = 0

    for td in test_dates:
        for vname, vdef in variants.items():
            n += 1
            overrides = vdef.get("overrides") or {}

            logger.info("[%s/%s] %s @ %s", n, total, vname, td)
            row = _run_one_variant(
                vname, td,
                overrides=overrides,
                top_n=top_n, batch_size=batch_size,
            )
            row["date"] = str(td)
            row["description"] = vdef.get("description", "")
            all_results.append(row)

    # 聚合对比
    df = pd.DataFrame(all_results)
    comparison = _build_comparison(df, variants)

    # 写报告
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path(ZPLAN_ROOT) / "backtest_review" / "ab"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"ab_{ts}.json"
    report = {
        "generated_at": ts,
        "variants": {k: v.get("description", "") for k, v in variants.items()},
        "test_dates": [str(d) for d in test_dates],
        "top_n": top_n,
        "horizon_days": horizon_days,
        "results": all_results,
        "comparison": comparison,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )

    # Markdown 摘要
    md_path = report_dir / f"ab_{ts}.md"
    md_text = _format_ab_report(report)
    md_path.write_text(md_text, encoding="utf-8")

    return {
        "ok": True,
        "report_path": str(report_path),
        "markdown_path": str(md_path),
        "markdown": md_text,
        "comparison": comparison,
        "raw": report,
    }


def _build_comparison(
    df: pd.DataFrame,
    variants: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """聚合对比：每个变体的平均 fail_rate、排名。"""
    if df.empty:
        return {}

    ok = df[df["ok"] == True]
    if ok.empty:
        return {"error": "所有变体均失败", "details": df.to_dict("records")}

    grouped = (
        ok.groupby("variant")
        .agg(
            mean_fail_rate=("fail_rate", "mean"),
            mean_fwd_return=("mean_fwd_return", "mean"),
            mean_fwd_positive_ratio=("fwd_positive", lambda x: x.sum() / max(1, ok[ok["variant"] == x.name]["fwd_total"].sum())),
            total_runs=("fail_rate", "count"),
        )
        .reset_index()
        .sort_values("mean_fwd_return", ascending=False)
    )
    best = grouped.iloc[0].to_dict() if len(grouped) > 0 else {}

    # 胜率：逐日 mean_fwd_return 最高者胜（fwd 收益比 fail_rate 更有区分度）
    win_counts: dict[str, int] = {v: 0 for v in variants}
    for date_val in ok["date"].unique():
        day_df = ok[ok["date"] == date_val]
        if day_df.empty:
            continue
        if day_df["mean_fwd_return"].notna().any():
            best_variant = day_df.loc[day_df["mean_fwd_return"].idxmax()]["variant"]
        else:
            best_variant = day_df.loc[day_df["fail_rate"].idxmin()]["variant"]
        win_counts[best_variant] = win_counts.get(best_variant, 0) + 1

    return {
        "by_variant": grouped.to_dict("records"),
        "best_variant": best.get("variant"),
        "best_mean_fwd_return": best.get("mean_fwd_return"),
        "best_mean_fail_rate": best.get("mean_fail_rate"),
        "win_counts": win_counts,
        "total_dates": ok["date"].nunique(),
    }


def _format_ab_report(report: dict[str, Any]) -> str:
    cmp = report.get("comparison") or {}
    lines = [
        "# A/B 历史回放报告",
        "",
        f"生成时间: {report.get('generated_at')}",
        f"测试日期: {len(report.get('test_dates') or [])} 个",
        f"变体数: {len(report.get('variants') or {})}",
        f"每变体选股: Top{report.get('top_n')} → 评估 Top20",
        f"验证 horizon: {report.get('horizon_days')} 交易日",
        "",
        "## 变体定义",
    ]
    for name, desc in (report.get("variants") or {}).items():
        lines.append(f"- **{name}**: {desc}")

    lines.extend(["", "## 聚合对比（按 forward 收益排序）", ""])
    lines.append("| 变体 | 均 fwd 收益 | 胜率(正向) | 均失败率 | 跑数 |")
    lines.append("|------|------------|-----------|----------|------|")
    for row in cmp.get("by_variant") or []:
        lines.append(
            f"| {row.get('variant')} "
            f"| **{row.get('mean_fwd_return', 0):+.2f}%** "
            f"| {row.get('mean_fwd_positive_ratio', 0):.0%} "
            f"| {(row.get('mean_fail_rate') or 0):.0%} "
            f"| {row.get('total_runs')} |"
        )

    win = cmp.get("win_counts") or {}
    if win:
        lines.extend(["", "## 逐日胜率（mean_fwd_return 最高者胜）", ""])
        total = cmp.get("total_dates") or 1
        for v, c in sorted(win.items(), key=lambda x: -x[1]):
            lines.append(f"- **{v}**: {c}/{total} ({c / total:.0%})")

    best = cmp.get("best_variant")
    if best:
        lines.extend([
            "",
            f"## 🏆 最优变体: **{best}**",
            f"均 forward 收益: **{cmp.get('best_mean_fwd_return', 0):+.2f}%**",
            f"均失败率: {cmp.get('best_mean_fail_rate', 0):.0%}",
        ])

    lines.extend([
        "",
        "## 下一步",
        "```bash",
        f"# 将最优变体晋升为默认配置，然后跑实时验证：",
        f"cd zplan-选股 && .venv/bin/python main.py llm-top --variant {best or 'champion'}",
        "cd zplan-回测 && .venv/bin/python main.py iterate verify",
        "```",
    ])

    return "\n".join(lines)


# ── CLI 辅助（供 main.py import）──────────────────────────────


def _load_variant_file(path: str) -> dict[str, dict[str, Any]]:
    """加载变体定义 YAML/JSON 文件。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"变体文件不存在: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data.get("variants") or {}


def _parse_variants_arg(raw: str) -> dict[str, dict[str, Any]]:
    """从 CLI 逗号分隔字符串解析变体列表（无 overrides 的简单模式）。"""
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return {n: {"description": "（无覆盖，使用默认 strategy.yaml）"} for n in names}
