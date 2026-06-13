"""选股 ↔ 回测迭代闭环 CLI 逻辑。"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.etl_akshare import run_catchup_panel_update
from zplan_shared.market import latest_panel_trade_date, latest_trade_date
from zplan_shared.models import PickRun, SessionLocal, init_db
from zplan_shared.pick_iterate_store import (
    append_iteration,
    compare_iterations,
    iteration_dir,
    list_iterations,
    load_iteration,
)

from backtest_agent.data_audit import audit_market_data, score_deviation_report

MONOREPO_ROOT = ZPLAN_ROOT.parent if (ZPLAN_ROOT.parent / "zplan-选股").is_dir() else ZPLAN_ROOT


def _pick_python() -> Path:
    p = MONOREPO_ROOT / "zplan-选股" / ".venv" / "bin" / "python"
    return p if p.is_file() else Path(sys.executable)


def _run_pick(cmd: str, *extra: str) -> dict[str, Any]:
    pick_root = MONOREPO_ROOT / "zplan-选股"
    proc = subprocess.run(
        [str(_pick_python()), str(pick_root / "main.py"), cmd, *extra],
        cwd=str(pick_root),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "stdout_tail": (proc.stdout or "")[-3000:],
        "stderr_tail": (proc.stderr or "")[-1500:],
    }


def ensure_market_ready(*, workers: int = 8) -> dict[str, Any]:
    audit = audit_market_data()
    if audit.get("ok"):
        return audit
    target = latest_panel_trade_date(min_symbols=300)
    if target:
        run_catchup_panel_update(panel_date=target, workers=workers)
    return audit_market_data()


def latest_llm_pick_run_id() -> int | None:
    """取最新有 forward 数据的 LLM 选股 run（trade_date < 最新交易日）。"""
    init_db()
    with SessionLocal() as session:
        today = latest_trade_date()
        if today:
            run = session.execute(
                select(PickRun)
                .where(
                    PickRun.run_kind.in_(["llm_top300", "scan"]),
                    PickRun.llm_enabled.is_(True),
                    PickRun.trade_date.isnot(None),
                    PickRun.trade_date <= today,
                )
                .order_by(PickRun.trade_date.desc(), desc(PickRun.id))
                .limit(1)
            ).scalar_one_or_none()
        else:
            run = None
        # 兜底
        if not run:
            run = session.execute(
                select(PickRun)
                .where(PickRun.run_kind.in_(["llm_top300", "scan"]), PickRun.llm_enabled.is_(True))
                .order_by(desc(PickRun.trade_date), desc(PickRun.id))
                .limit(1)
            ).scalar_one_or_none()
        return int(run.id) if run else None


def _rule_version(run_id: int) -> str | None:
    init_db()
    with SessionLocal() as session:
        run = session.get(PickRun, run_id)
        return run.rule_version if run else None


def _metrics_from_report(report: dict[str, Any]) -> dict[str, Any]:
    llm = report.get("llm_eval") or {}
    summary = report.get("score_summary") or {}
    s = llm.get("summary") or {}
    return {
        "pick_run_id": report.get("run_id"),
        "fail_rate": s.get("fail_rate"),
        "fail_count": s.get("fail"),
        "pass_count": s.get("pass"),
        "pending_count": s.get("pending"),
        "mean_rule": summary.get("mean_rule"),
        "mean_llm": summary.get("mean_llm"),
        "mean_delta": summary.get("mean_delta"),
        "mean_fwd_return": summary.get("mean_fwd_return"),
        "llm_worse_than_rule": summary.get("llm_worse_than_rule"),
        "tag_counts": llm.get("tag_counts") or {},
    }


def format_cycle_report(record: dict[str, Any], comparison: dict[str, Any] | None) -> str:
    m = record.get("metrics") or {}
    lines = [
        f"# 选股迭代闭环 · {record.get('iteration_id')}",
        "",
        f"- 阶段：**{record.get('phase')}** | pick_run_id=**{record.get('pick_run_id')}**",
        f"- 规则：`{record.get('rule_version') or '—'}`",
        "",
        "## 本轮",
        f"- 失败率 **{(m.get('fail_rate') or 0):.0%}** | forward **{m.get('mean_fwd_return')}%**",
        f"- 规则 **{m.get('mean_rule')}** / LLM **{m.get('mean_llm')}** (Δ{m.get('mean_delta')})",
        "",
        "## 对比上一轮",
    ]
    if not comparison:
        lines.append("（无上一轮）")
    else:
        for k, v in (comparison.get("deltas") or {}).items():
            if isinstance(v, (int, float)):
                lines.append(f"- {k}: **{v:+}**")
            else:
                lines.append(f"- {k}: {v}")
        lines.append(
            "- 结论：**整体改善** ✅" if comparison.get("improved") else "- 结论：继续按 Review 调整"
        )

    actions = record.get("review_actions") or []
    if actions:
        lines.extend(["", "## 下一轮 Review"])
        for a in actions[:6]:
            lines.append(f"- [{a.get('layer')}] {a.get('action')}")

    lines.extend(
        [
            "",
            "## 闭环",
            "```bash",
            "cd zplan-回测 && .venv/bin/python main.py iterate verify   # 每日",
            "cd zplan-回测 && .venv/bin/python main.py iterate full     # 选股+验证",
            "cd zplan-回测 && .venv/bin/python main.py iterate history",
            "```",
        ]
    )
    return "\n".join(lines)


def run_iteration_cycle(
    *,
    phase: str = "verify",
    run_id: int | None = None,
    top_n: int = 10,
    horizon_days: int = 5,
    catchup: bool = True,
    catchup_workers: int = 8,
    with_pick: bool = False,
    note: str = "",
) -> dict[str, Any]:
    steps: list[str] = []
    pick_steps: dict[str, Any] = {}

    if catchup:
        market = ensure_market_ready(workers=catchup_workers)
    else:
        market = audit_market_data()
    steps.append("market")

    if with_pick or phase == "full":
        pick_steps["init_rule"] = _run_pick("init-rule")
        steps.append("init_rule")
        if not pick_steps["init_rule"]["ok"]:
            return {"ok": False, "message": "init-rule 失败", "pick_steps": pick_steps, "market": market}
        pick_steps["llm_top"] = _run_pick("llm-top", "--top", "300")
        steps.append("llm_top")
        if not pick_steps["llm_top"]["ok"]:
            return {"ok": False, "message": "llm-top 失败", "pick_steps": pick_steps, "market": market}
        run_id = latest_llm_pick_run_id()

    resolved = run_id or latest_llm_pick_run_id()
    if resolved is None:
        return {"ok": False, "message": "无 llm_top300 记录，请 iterate full", "market": market}

    report = score_deviation_report(run_id=resolved, top_n=top_n, horizon_days=horizon_days)
    steps.append("audit")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    review_dir = Path(ZPLAN_ROOT) / "backtest_review"
    review_dir.mkdir(parents=True, exist_ok=True)
    audit_path = review_dir / f"audit_run{resolved}_{ts[:8]}.md"
    audit_path.write_text(report.get("markdown", ""), encoding="utf-8")

    opt = (report.get("llm_eval") or {}).get("optimization") or {}
    record = {
        "iteration_id": ts,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase": phase,
        "note": note,
        "pick_run_id": resolved,
        "rule_version": _rule_version(resolved),
        "horizon_days": horizon_days,
        "top_n": top_n,
        "market": {
            "ok": market.get("ok"),
            "effective_date": market.get("effective_latest_date"),
            "panel_rows": market.get("effective_panel_rows"),
        },
        "metrics": _metrics_from_report(report),
        "review_actions": opt.get("review_actions") or [],
        "report_path": str(audit_path),
        "steps": steps,
        "pick_steps": pick_steps or None,
    }
    append_iteration(record)

    history = list_iterations(limit=2)
    comparison = compare_iterations(history[1], history[0]) if len(history) >= 2 else None
    cycle_md = format_cycle_report(record, comparison)
    cycle_path = review_dir / "iterations" / f"cycle_{ts}.md"
    cycle_path.parent.mkdir(parents=True, exist_ok=True)
    cycle_path.write_text(cycle_md, encoding="utf-8")

    return {
        "ok": True,
        "iteration_id": ts,
        "pick_run_id": resolved,
        "metrics": record["metrics"],
        "comparison": comparison,
        "cycle_path": str(cycle_path),
        "report_path": str(audit_path),
        "markdown": cycle_md,
    }


def print_iteration_history(*, limit: int = 10) -> None:
    rows = list_iterations(limit=limit)
    if not rows:
        print("尚无迭代记录。先运行：main.py iterate verify")
        return
    print(f"{'时间':<22} {'阶段':<8} {'run':<6} {'失败率':<8} {'fwd%':<8} {'LLMΔ':<8}")
    for r in rows:
        m = r.get("metrics") or {}
        print(
            f"{(r.get('created_at_utc') or '')[:19]:<22} "
            f"{r.get('phase',''):<8} "
            f"{r.get('pick_run_id',''):<6} "
            f"{(m.get('fail_rate') or 0):.0%}    "
            f"{m.get('mean_fwd_return') or '—':<8} "
            f"{m.get('mean_delta') or '—':<8}"
        )


def print_iteration_diff(*, limit: int = 2) -> None:
    rows = list_iterations(limit=limit)
    if len(rows) < 2:
        print("至少需要 2 轮记录才能对比")
        return
    cmp = compare_iterations(rows[1], rows[0])
    print(json.dumps(cmp, ensure_ascii=False, indent=2, default=str))
