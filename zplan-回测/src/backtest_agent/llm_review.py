"""LLM Top 池回测报告 CLI。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.pick_llm_eval import evaluate_llm_run, format_llm_eval_report


def run_llm_eval(
    *,
    run_id: int | None = None,
    top_n: int = 10,
    horizon_days: int = 5,
) -> dict:
    result = evaluate_llm_run(
        run_id=run_id,
        top_n=top_n,
        horizon_days=horizon_days,
    )
    result["markdown"] = format_llm_eval_report(result)
    return result


def print_llm_eval(
    *,
    run_id: int | None = None,
    top_n: int = 10,
    horizon_days: int = 5,
    as_json: bool = False,
    output: str | Path | None = None,
) -> Path | None:
    result = run_llm_eval(run_id=run_id, top_n=top_n, horizon_days=horizon_days)
    out_path: Path | None = None

    if output:
        out_path = Path(output)
        if not out_path.is_absolute():
            review_dir = ZPLAN_ROOT / "backtest_review"
            review_dir.mkdir(parents=True, exist_ok=True)
            out_path = review_dir / out_path.name
        out_path.write_text(result["markdown"], encoding="utf-8")
        json_path = out_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result["markdown"])
        if out_path:
            print(f"\n---\n已写入: {out_path}\nJSON: {out_path.with_suffix('.json')}")

    return out_path


def default_review_filename(run_id: int | None) -> str:
    rid = run_id if run_id is not None else "latest"
    return f"llm_eval_run{rid}_{date.today().isoformat()}.md"
