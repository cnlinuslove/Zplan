"""完整流水线：全市场规则分 → Top300 LLM 简评 → Top30 深度研报。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.llm.gemini import gemini_available
from zplan_shared.pick_store import save_report_run

from pick_agent.llm_cost import estimate_from_usage, estimate_full_report, estimate_scan_brief
from pick_agent.llm_research import format_llm_report_markdown, research_with_llm
from pick_agent.llm_top300 import run_llm_top_from_rule_scores
from pick_agent.rule_universe import build_rule_scores_universe
from pick_agent.strategy import PickStrategy, load_strategy

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_catchup_panel(*, limit: int | None = None, workers: int | None = None) -> dict[str, Any]:
    """调用股价 Agent 补齐最新交易日截面。"""
    price_dir = _repo_root().parent / "zplan-股价"
    py = price_dir / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)
    cmd = [str(py), str(price_dir / "main.py"), "--catch-up-panel"]
    if limit:
        cmd.extend(["--limit", str(limit)])
    if workers:
        cmd.extend(["--workers", str(workers)])
    logger.info("运行股价补齐: %s", " ".join(cmd))
    subprocess.run(cmd, cwd=str(price_dir), check=True)
    return {"ok": True, "agent": "catchup_panel"}


def run_deep_reports_top(
    picks: list[dict[str, Any]],
    *,
    top_n: int = 30,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
    persist: bool = True,
    deep_workers: int | None = None,
) -> dict[str, Any]:
    """对 LLM 简评后的候选取 Top N 生成深度研报（每只 1 次 API）。"""
    if not gemini_available():
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    strat = strategy or load_strategy()
    ranked = sorted(
        picks,
        key=lambda p: (
            float(p.get("llm_composite_score") or p.get("composite_score") or 0),
            float(p.get("rule_composite_score") or p.get("tech_score") or 0),
        ),
        reverse=True,
    )[:top_n]

    if not ranked:
        return {"ok": False, "message": "无候选标的"}

    n_workers = deep_workers if deep_workers is not None else int(
        os.getenv("PICK_DEEP_LLM_WORKERS", "2")
    )
    n_workers = max(1, min(n_workers, 4))
    logger.info("深度研报并行 workers=%s（Gemini 仍有全局限速）", n_workers)

    def _one_deep(i: int, p: dict[str, Any]) -> dict[str, Any]:
        code = str(p["ts_code"])
        name = p.get("name") or code
        logger.info("[深度 %s/%s] %s %s", i, top_n, name, code)
        report = research_with_llm(
            code,
            strategy=strat,
            skip_health_check=skip_health_check,
        )
        md = format_llm_report_markdown(report)
        entry: dict[str, Any] = {
            "ts_code": code,
            "name": name,
            "rank": i,
            "llm_composite_score": (report.get("投资建议") or {}).get("LLM综合分"),
            "rule_composite_score": (report.get("投资建议") or {}).get("规则引擎综合分"),
            "usage": (report.get("llm") or {}).get("usage"),
            "report": report,
            "markdown": md,
        }
        return entry

    reports: list[dict[str, Any]] = []
    run_ids: list[int] = []
    usages: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(_one_deep, i, p): i for i, p in enumerate(ranked, start=1)
        }
        by_rank: dict[int, dict[str, Any]] = {}
        for fut in as_completed(futures):
            entry = fut.result()
            by_rank[entry["rank"]] = entry

    for i in range(1, top_n + 1):
        entry = by_rank[i]
        usage = entry.pop("usage", None)
        report = entry.pop("report")
        entry.pop("markdown")
        md = format_llm_report_markdown(report)
        if usage:
            usages.append(usage)
        if persist:
            rid = save_report_run(
                report,
                symbol_query=entry["ts_code"],
                markdown=md,
                params={"pipeline": "deep_top", "rank": i, "from_llm_top": True},
                llm_enabled=True,
                llm_model=strat.llm_model,
            )
            entry["run_id"] = rid
            run_ids.append(rid)
        reports.append(entry)

    cost_est = estimate_full_report()
    total_usd = cost_est.usd * len(ranked)
    usage_total = None
    if usages:
        usage_total = usages[0]
        for u in usages[1:]:
            if usage_total:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage_total[k] = int(usage_total.get(k) or 0) + int(u.get(k) or 0)

    return {
        "ok": True,
        "deep_top_n": top_n,
        "reports": reports,
        "run_ids": run_ids,
        "llm_usage_aggregate": usage_total,
        "cost_estimate_usd": round(total_usd, 3),
        "cost_estimate_cny": round(total_usd * 7.2, 2),
    }


def run_full_pipeline(
    *,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
    catchup_panel: bool = False,
    catchup_limit: int | None = None,
    llm_top_n: int = 300,
    llm_batch_size: int | None = None,
    deep_top_n: int = 30,
    deepen: bool = True,
    catchup_workers: int | None = None,
    deepen_workers: int | None = None,
    deep_llm_workers: int | None = None,
) -> dict[str, Any]:
    """init-rule → llm-top → deep-top。"""
    strat = strategy or load_strategy()
    llm_top_n = llm_top_n if llm_top_n is not None else strat.llm_top_n
    llm_batch_size = llm_batch_size if llm_batch_size is not None else strat.llm_batch_size
    out: dict[str, Any] = {"ok": True, "steps": []}

    if catchup_panel:
        out["catchup"] = run_catchup_panel(limit=catchup_limit, workers=catchup_workers)
        out["steps"].append("catchup_panel")
        try:
            from zplan_shared.etl_daily_features import run_daily_features_update

            logger.info("补齐后刷新 daily_features 物化表…")
            out["daily_features"] = run_daily_features_update()
            out["steps"].append("daily_features")
        except Exception as exc:
            logger.warning("daily_features 更新失败（init-rule 将现场算指标）: %s", exc)

    init_r = build_rule_scores_universe(strategy=strat, skip_health_check=skip_health_check)
    out["init_rule"] = init_r
    if not init_r.get("ok"):
        out["ok"] = False
        out["message"] = init_r.get("message", "init-rule 失败")
        return out
    out["steps"].append("init_rule")

    llm_r = run_llm_top_from_rule_scores(
        top_n=llm_top_n,
        batch_size=llm_batch_size,
        strategy=strat,
        deepen=deepen,
        deepen_workers=deepen_workers,
        use_llm=True,
        persist=True,
    )
    out["llm_top"] = llm_r
    if not llm_r.get("ok"):
        out["ok"] = False
        out["message"] = llm_r.get("message", "llm-top 失败")
        return out
    out["steps"].append("llm_top")

    deep_r = run_deep_reports_top(
        llm_r.get("picks") or [],
        top_n=deep_top_n,
        strategy=strat,
        skip_health_check=skip_health_check,
        persist=True,
        deep_workers=deep_llm_workers,
    )
    out["deep_top"] = deep_r
    if not deep_r.get("ok"):
        out["ok"] = False
        out["message"] = deep_r.get("message", "deep-top 失败")
        return out
    out["steps"].append("deep_top")

    batches = max(1, (llm_top_n + llm_batch_size - 1) // llm_batch_size)
    llm_top_usd = round(batches * 0.044, 3)
    deep_usd = float(deep_r.get("cost_estimate_usd") or 0)
    out["cost_estimate"] = {
        "llm_top_batches": batches,
        "llm_top_usd_approx": llm_top_usd,
        "deep_top_usd": deep_usd,
        "deep_top_cny": deep_r.get("cost_estimate_cny"),
        "total_usd_approx": round(llm_top_usd + deep_usd, 2),
        "total_cny_approx": round((llm_top_usd + deep_usd) * 7.2, 1),
    }
    if deep_r.get("llm_usage_aggregate"):
        act = estimate_from_usage(deep_r["llm_usage_aggregate"], label="深度Top30实际")
        if act:
            out["deep_actual_cost"] = act.to_dict()

    return out
