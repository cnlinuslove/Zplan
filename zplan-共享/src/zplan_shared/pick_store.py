"""选股运行结果持久化（``pick_runs`` / ``pick_entries``）。"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import desc, select

from zplan_shared.models import PickEntry, PickRun, SessionLocal, init_db
from zplan_shared.pick_predictions import price_levels_from_pick, price_levels_from_report


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


def _parse_as_of(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _price_fields_from_pick(p: dict[str, Any]) -> dict[str, Any]:
    lv = price_levels_from_pick(p)
    return {
        "predicted_buy_price": lv.get("predicted_buy_price"),
        "predicted_target_price": lv.get("predicted_target_price"),
        "predicted_stop_loss": lv.get("predicted_stop_loss"),
        "price_source": lv.get("price_source"),
    }


def _exit_plan_json_from_pick(p: dict[str, Any]) -> str | None:
    """从 pick 字典提取 exit_plan JSON 字符串。"""
    llm_brief = p.get("llm_brief") or {}
    llm_block = p.get("llm") if isinstance(p.get("llm"), dict) else {}
    ep = llm_block.get("exit_plan")
    if ep and isinstance(ep, dict) and ep.get("recommended_plan"):
        return _dumps(ep)
    # 从 brief 构建简化版 exit_plan
    brief_plan = llm_brief.get("recommended_exit_plan")
    if brief_plan:
        return _dumps({
            "recommended_plan": brief_plan,
            "reasoning": llm_brief.get("exit_reasoning", ""),
        })
    return None


def _entry_from_pick(p: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    llm_brief = p.get("llm_brief") or {}
    llm_block = p.get("llm") if isinstance(p.get("llm"), dict) else {}
    return {
        "ts_code": p.get("ts_code"),
        "name": p.get("name"),
        "rank_in_run": rank,
        "rule_tech_score": p.get("tech_score"),
        "rule_composite_score": p.get("rule_composite_score") or p.get("composite_score"),
        "llm_composite_score": p.get("llm_composite_score") or p.get("adjusted_score") or llm_block.get("composite_score"),
        "llm_technical_score": llm_block.get("technical_score"),
        "llm_financial_score": llm_block.get("financial_score"),
        "llm_news_score": llm_block.get("news_score"),
        "final_composite_score": p.get("adjusted_score") or p.get("composite_score") or p.get("llm_composite_score"),
        "recommendation": llm_brief.get("recommendation") or llm_block.get("recommendation"),
        "verdict": p.get("verdict"),
        "close_price": p.get("close"),
        "analysis_process_json": _dumps(
            {
                "signals": p.get("signals"),
                "llm_brief": llm_brief,
                "ret_20d": p.get("ret_20d"),
                "high_60d_pct": p.get("high_60d_pct"),
                "kdj": {"k": p.get("kdj_k"), "d": p.get("kdj_d")},
                "news_mentions_48h": p.get("news_mentions_48h"),
                "industry_relative_note": p.get("industry_relative_note"),
            }
        ),
        **_price_fields_from_pick(p),
        "exit_plan_source": "llm" if (llm_brief.get("recommended_exit_plan") or llm_block.get("exit_plan")) else None,
        "exit_plan_key": llm_brief.get("recommended_exit_plan") or (llm_block.get("exit_plan") or {}).get("recommended_plan"),
        "exit_plan_json": _exit_plan_json_from_pick(p),
        "llm_exit_reasoning": llm_brief.get("exit_reasoning") or (llm_block.get("exit_plan") or {}).get("reasoning"),
    }


def _entry_from_report(report: dict[str, Any]) -> dict[str, Any]:
    meta = report.get("meta") or {}
    advice = report.get("投资建议") or {}
    m4 = (report.get("modules") or {}).get("4_股价分析") or {}
    m5 = (report.get("modules") or {}).get("5_财务情况") or {}
    llm = report.get("llm") or {}
    prices = price_levels_from_report(report)
    return {
        "ts_code": meta.get("ts_code"),
        "name": meta.get("name"),
        "rank_in_run": None,
        "rule_tech_score": m4.get("技术得分"),
        "rule_composite_score": advice.get("规则引擎综合分"),
        "llm_composite_score": advice.get("LLM综合分") or llm.get("composite_score"),
        "llm_technical_score": llm.get("technical_score") or m4.get("LLM技术得分"),
        "llm_financial_score": llm.get("financial_score") or m5.get("LLM财务得分"),
        "llm_news_score": llm.get("news_score"),
        "final_composite_score": advice.get("综合推荐分"),
        "recommendation": advice.get("操作建议") or llm.get("recommendation"),
        "verdict": m4.get("技术面结论"),
        "close_price": (m4.get("指标快照") or {}).get("close"),
        "predicted_buy_price": prices.get("predicted_buy_price"),
        "predicted_target_price": prices.get("predicted_target_price"),
        "predicted_stop_loss": prices.get("predicted_stop_loss"),
        "price_source": prices.get("price_source"),
        "analysis_process_json": _dumps(
            {
                "pipeline": report.get("pipeline")
                or ["rule_engine", "llm_research" if llm else "rule_only"],
                "rule_version": report.get("rule_version"),
                "as_of": report.get("as_of"),
                "rule_signals": m4.get("关键信号"),
                "rule_features": m4.get("指标快照"),
                "intraday": m4.get("分时特征"),
                "llm_usage": llm.get("usage"),
                "llm_raw": {k: v for k, v in llm.items() if k not in ("usage",)},
                "modules_summary": {
                    k: report.get("modules", {}).get(k)
                    for k in report.get("modules", {})
                },
            }
        ),
        "report_json": _dumps(report),
    "exit_plan_source": "llm" if (llm.get("exit_plan") or {}).get("recommended_plan") else None,
    "exit_plan_key": (llm.get("exit_plan") or {}).get("recommended_plan"),
    "exit_plan_json": _dumps(llm["exit_plan"]) if llm.get("exit_plan") else None,
    "llm_exit_reasoning": (llm.get("exit_plan") or {}).get("reasoning"),
    }


def save_scan_run(
    result: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    variant_label: str | None = None,
    prompt_hash: str | None = None,
) -> int:
    """保存全市场扫描结果，返回 ``run_id``。"""
    init_db()
    picks = result.get("picks") or []
    summary = {
        k: result[k]
        for k in (
            "scanned",
            "prefiltered",
            "qualified",
            "as_of",
            "llm_scan_brief",
            "llm_usage",
            "llm_cost_estimate",
            "health",
        )
        if k in result
    }
    with SessionLocal() as session:
        run = PickRun(
            run_kind=result.get("run_kind") or "scan",
            trade_date_as_of=_parse_as_of(result.get("as_of")),
            rule_version=str(result.get("rule_version") or ""),
            llm_enabled=bool(result.get("llm_scan_brief")),
            llm_model=(result.get("llm_usage") or {}).get("model"),
            symbol_query=None,
            variant_label=variant_label,
            prompt_hash=prompt_hash,
            params_json=_dumps(params or {}),
            summary_json=_dumps(summary),
        )
        session.add(run)
        session.flush()

        for i, p in enumerate(picks, start=1):
            row = _entry_from_pick(p, rank=i)
            session.add(
                PickEntry(
                    run_id=run.id,
                    markdown=None,
                    report_json=None,
                    **row,
                )
            )
        session.commit()
        return int(run.id)


def save_report_run(
    report: dict[str, Any],
    *,
    symbol_query: str | None = None,
    markdown: str | None = None,
    params: dict[str, Any] | None = None,
    llm_enabled: bool = False,
    llm_model: str | None = None,
) -> int:
    """保存单票深度研报，返回 ``run_id``。"""
    init_db()
    row = _entry_from_report(report)
    meta = report.get("meta") or {}
    with SessionLocal() as session:
        run = PickRun(
            run_kind="report",
            trade_date_as_of=_parse_as_of(report.get("as_of")),
            rule_version=str(report.get("rule_version") or ""),
            llm_enabled=llm_enabled,
            llm_model=llm_model,
            symbol_query=symbol_query,
            params_json=_dumps(params or {}),
            summary_json=_dumps(
                {
                    "ts_code": meta.get("ts_code"),
                    "name": meta.get("name"),
                    "composite_score": (report.get("投资建议") or {}).get("综合推荐分"),
                }
            ),
        )
        session.add(run)
        session.flush()
        session.add(
            PickEntry(
                run_id=run.id,
                markdown=markdown,
                report_json=row.pop("report_json"),
                **row,
            )
        )
        session.commit()
        return int(run.id)


def list_runs(*, limit: int = 30, run_kind: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as session:
        stmt = select(PickRun).order_by(desc(PickRun.created_at_utc)).limit(limit)
        if run_kind:
            stmt = stmt.where(PickRun.run_kind == run_kind)
        rows = session.execute(stmt).scalars().all()
    return [
        {
            "run_id": r.id,
            "run_kind": r.run_kind,
            "trade_date_as_of": str(r.trade_date_as_of) if r.trade_date_as_of else None,
            "rule_version": r.rule_version,
            "llm_enabled": r.llm_enabled,
            "llm_model": r.llm_model,
            "symbol_query": r.symbol_query,
            "variant_label": r.variant_label,
            "prompt_hash": r.prompt_hash,
            "summary": _loads(r.summary_json),
            "created_at_utc": r.created_at_utc.isoformat() + "Z",
        }
        for r in rows
    ]


def get_run(run_id: int) -> dict[str, Any] | None:
    init_db()
    with SessionLocal() as session:
        run = session.get(PickRun, run_id)
        if not run:
            return None
        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run_id)
            .order_by(PickEntry.rank_in_run, PickEntry.id)
        ).scalars().all()
    return {
        "run": {
            "run_id": run.id,
            "run_kind": run.run_kind,
            "trade_date_as_of": str(run.trade_date_as_of) if run.trade_date_as_of else None,
            "rule_version": run.rule_version,
            "llm_enabled": run.llm_enabled,
            "llm_model": run.llm_model,
            "symbol_query": run.symbol_query,
            "variant_label": run.variant_label,
            "prompt_hash": run.prompt_hash,
            "params": _loads(run.params_json),
            "summary": _loads(run.summary_json),
            "created_at_utc": run.created_at_utc.isoformat() + "Z",
        },
        "entries": [_entry_dict(e) for e in entries],
    }


def _entry_dict(e: PickEntry) -> dict[str, Any]:
    return {
        "entry_id": e.id,
        "ts_code": e.ts_code,
        "name": e.name,
        "rank": e.rank_in_run,
        "final_composite_score": e.final_composite_score,
        "rule_tech_score": e.rule_tech_score,
        "rule_composite_score": e.rule_composite_score,
        "llm_composite_score": e.llm_composite_score,
        "recommendation": e.recommendation,
        "verdict": e.verdict,
        "close_price": e.close_price,
        "predicted_buy_price": e.predicted_buy_price,
        "predicted_target_price": e.predicted_target_price,
        "predicted_stop_loss": e.predicted_stop_loss,
        "price_source": e.price_source,
        "has_report": bool(e.report_json),
        "has_markdown": bool(e.markdown),
        "analysis_process": _loads(e.analysis_process_json),
        "created_at_utc": e.created_at_utc.isoformat() + "Z",
    }


def get_entry_report(entry_id: int) -> dict[str, Any] | None:
    init_db()
    with SessionLocal() as session:
        e = session.get(PickEntry, entry_id)
        if not e:
            return None
        return {
            "entry_id": e.id,
            "run_id": e.run_id,
            "ts_code": e.ts_code,
            "name": e.name,
            "markdown": e.markdown,
            "report": _loads(e.report_json),
            "analysis_process": _loads(e.analysis_process_json),
            "scores": {
                "final_composite": e.final_composite_score,
                "rule_tech": e.rule_tech_score,
                "rule_composite": e.rule_composite_score,
                "llm_composite": e.llm_composite_score,
                "llm_technical": e.llm_technical_score,
                "llm_financial": e.llm_financial_score,
                "llm_news": e.llm_news_score,
            },
        }


def history_for_stock(ts_code: str, *, limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    code = ts_code.strip().zfill(6) if ts_code.isdigit() else ts_code
    with SessionLocal() as session:
        rows = session.execute(
            select(PickEntry, PickRun)
            .join(PickRun, PickEntry.run_id == PickRun.id)
            .where(PickEntry.ts_code == code)
            .order_by(desc(PickEntry.created_at_utc))
            .limit(limit)
        ).all()
    out: list[dict[str, Any]] = []
    for entry, run in rows:
        out.append(
            {
                "entry_id": entry.id,
                "run_id": run.id,
                "run_kind": run.run_kind,
                "trade_date_as_of": str(run.trade_date_as_of) if run.trade_date_as_of else None,
                "final_composite_score": entry.final_composite_score,
                "recommendation": entry.recommendation,
                "created_at_utc": entry.created_at_utc.isoformat() + "Z",
            }
        )
    return out
