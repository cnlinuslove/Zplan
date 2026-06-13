"""行情完整性审计 + 选股打分偏差综合报告。"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import func, select

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.etl_akshare import run_catchup_panel_update, symbols_missing_panel_date
from zplan_shared.market import get_panel, latest_panel_trade_date, latest_trade_date
from zplan_shared.market_health import check_market_health
from zplan_shared.models import DailyPrice, PickRun, SessionLocal, init_db
from zplan_shared.pick_llm_eval import evaluate_llm_run, format_llm_eval_report
from zplan_shared.pick_predictions import validate_entries


def audit_market_data(*, min_panel_rows: int = 300) -> dict[str, Any]:
    init_db()
    health = check_market_health(min_panel_rows=min_panel_rows)
    raw_latest = None
    panel_latest = latest_panel_trade_date(min_symbols=min_panel_rows)
    effective = latest_trade_date(min_panel_symbols=min_panel_rows)

    with SessionLocal() as session:
        raw_latest = session.execute(
            select(func.max(DailyPrice.trade_date)).where(DailyPrice.adjust_type == "qfq")
        ).scalar_one_or_none()
        if raw_latest and effective:
            raw_cnt = session.execute(
                select(func.count(DailyPrice.ts_code)).where(
                    DailyPrice.trade_date == raw_latest,
                    DailyPrice.adjust_type == "qfq",
                )
            ).scalar_one()
            eff_cnt = len(get_panel(effective)) if effective else 0
        else:
            raw_cnt = eff_cnt = 0

    missing = symbols_missing_panel_date(raw_latest) if raw_latest else []
    return {
        "ok": health.ok,
        "health": health.__dict__,
        "raw_latest_date": str(raw_latest) if raw_latest else None,
        "raw_latest_symbols": raw_cnt,
        "effective_latest_date": str(effective) if effective else None,
        "effective_panel_rows": eff_cnt,
        "panel_complete_date": str(panel_latest) if panel_latest else None,
        "missing_on_raw_latest": len(missing),
        "catchup_hint": (
            f"cd zplan-股价 && .venv/bin/python main.py --catch-up-panel --workers 8"
            if missing
            else None
        ),
    }


def run_catchup_if_needed(*, limit: int | None = None, workers: int = 8) -> dict[str, Any]:
    audit = audit_market_data()
    if audit["missing_on_raw_latest"] == 0:
        return {"ok": True, "skipped": True, "message": "截面已齐", "audit": audit}
    stats = run_catchup_panel_update(limit=limit, workers=workers)
    audit_after = audit_market_data()
    return {"ok": True, "skipped": False, "catchup": stats, "audit_before": audit, "audit_after": audit_after}


def score_deviation_report(
    *,
    run_id: int | None = None,
    top_n: int = 10,
    horizon_days: int = 5,
) -> dict[str, Any]:
    """综合：行情审计 + 预测价验证 + LLM Top 偏差。"""
    init_db()
    market = audit_market_data()

    with SessionLocal() as session:
        if run_id is None:
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
                    .order_by(PickRun.trade_date.desc(), PickRun.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
            else:
                run = None
            if not run:
                run = session.execute(
                    select(PickRun)
                    .where(PickRun.run_kind.in_(["llm_top300", "scan"]), PickRun.llm_enabled.is_(True))
                    .order_by(PickRun.trade_date.desc(), PickRun.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
            run_id = run.id if run else None

    validation = validate_entries(run_id=run_id, horizons=[horizon_days, 10, 20], limit=top_n * 3)
    llm_eval = evaluate_llm_run(run_id=run_id, top_n=top_n, horizon_days=horizon_days) if run_id else {
        "ok": False,
        "message": "无 LLM 选股运行（scan/llm_top300）",
    }

    rule_vs_llm: list[dict[str, Any]] = []
    if llm_eval.get("ok"):
        for e in llm_eval.get("entries") or []:
            rule_vs_llm.append(
                {
                    "rank": e.get("rank"),
                    "ts_code": e.get("ts_code"),
                    "name": e.get("name"),
                    "rule_score": e.get("rule_score"),
                    "llm_score": e.get("llm_score"),
                    "score_delta": e.get("score_delta"),
                    "fwd_return_pct": e.get("return_from_close_pct"),
                    "verdict": e.get("verdict"),
                }
            )

    import pandas as pd

    df = pd.DataFrame(rule_vs_llm)
    summary: dict[str, Any] = {}
    if not df.empty:
        summary = {
            "mean_rule": round(float(df["rule_score"].mean()), 2),
            "mean_llm": round(float(df["llm_score"].mean()), 2),
            "mean_delta": round(float(df["score_delta"].mean()), 2),
            "mean_fwd_return": round(float(df["fwd_return_pct"].dropna().mean()), 2)
            if df["fwd_return_pct"].notna().any()
            else None,
            "fail_rate": round(float((df["verdict"] == "fail").mean()), 4),
            "llm_worse_than_rule": int(
                (
                    (df["fwd_return_pct"] < 0)
                    & (df["score_delta"] > 3)
                ).sum()
            ),
        }

    md_parts = [
        f"# 选股打分偏差审计（run_id={run_id}，{date.today()}）",
        "",
        "## 1. 行情数据",
        f"- 健康：{'✅' if market['ok'] else '❌'} {market['health']['message']}",
        f"- 库内 max 日：{market['raw_latest_date']}（{market['raw_latest_symbols']} 只）",
        f"- 有效截面日：{market['effective_latest_date']}（{market['effective_panel_rows']} 只）",
    ]
    if market["missing_on_raw_latest"]:
        md_parts.append(
            f"- ⚠️ max 日缺 {market['missing_on_raw_latest']} 只 → `{market['catchup_hint']}`"
        )

    md_parts.extend(["", "## 2. 预测价验证", "```json", json.dumps(validation, ensure_ascii=False, indent=2), "```"])

    if llm_eval.get("ok"):
        md_parts.extend(["", format_llm_eval_report(llm_eval)])

    if summary:
        md_parts.extend(
            [
                "",
                "## 3. 规则 vs LLM 偏差摘要",
                f"- 规则均分：**{summary.get('mean_rule')}** | LLM 均分：**{summary.get('mean_llm')}** | Δ **{summary.get('mean_delta')}**",
                f"- Top{top_n} 失败率：**{summary.get('fail_rate', 0):.0%}** | 均 forward 收益：**{summary.get('mean_fwd_return')}%**",
                f"- LLM 抬分但仍下跌：**{summary.get('llm_worse_than_rule')}** 只",
            ]
        )

    return {
        "ok": True,
        "run_id": run_id,
        "market": market,
        "validation": validation,
        "llm_eval": llm_eval,
        "score_summary": summary,
        "entries": rule_vs_llm,
        "markdown": "\n".join(md_parts),
    }
