"""将 pick_runs 导出为 Excel 摘要。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.models import SessionLocal, StockList, init_db
from zplan_shared.pick_store import get_run, list_runs


def _latest_llm_top_run_id() -> int | None:
    for r in list_runs(limit=30, run_kind="llm_top300"):
        if r.get("llm_enabled"):
            return int(r["run_id"])
    return None


def build_llm_top_summary_rows(run_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = get_run(run_id)
    if not data:
        raise LookupError(f"未找到 run_id={run_id}")

    run = data["run"]
    init_db()
    codes = [e["ts_code"] for e in data["entries"]]
    industry_map: dict[str, str | None] = {}
    with SessionLocal() as session:
        if codes:
            rows = session.execute(
                select(StockList.ts_code, StockList.industry).where(
                    StockList.ts_code.in_(codes)
                )
            ).all()
            industry_map = {r[0]: r[1] for r in rows}

    rows_out: list[dict[str, Any]] = []
    for e in sorted(data["entries"], key=lambda x: x.get("rank") or 9999):
        proc = e.get("analysis_process") or {}
        brief = proc.get("llm_brief") or {}
        kdj = proc.get("kdj") or {}
        rows_out.append(
            {
                "排名": e.get("rank"),
                "代码": e["ts_code"],
                "名称": e.get("name"),
                "板块": industry_map.get(e["ts_code"]) or "",
                "当前股价": e.get("close_price"),
                "规则技术分": e.get("rule_tech_score"),
                "规则综合分": e.get("rule_composite_score"),
                "LLM综合分": e.get("llm_composite_score"),
                "最终综合分": e.get("final_composite_score"),
                "操作建议": e.get("recommendation"),
                "技术面": e.get("verdict"),
                "20日涨跌%": proc.get("ret_20d"),
                "KDJ_K": kdj.get("k"),
                "KDJ_D": kdj.get("d"),
                "48h资讯条数": proc.get("news_mentions_48h"),
                "走势简评": brief.get("trend") or "",
                "相对规则说明": brief.get("vs_rule_engine") or "",
                "关键信号": "；".join(proc.get("signals") or [])[:200],
                "entry_id": e.get("entry_id"),
            }
        )

    meta = {
        "run_id": run_id,
        "run_kind": run.get("run_kind"),
        "trade_date_as_of": run.get("trade_date_as_of"),
        "rule_version": run.get("rule_version"),
        "llm_enabled": run.get("llm_enabled"),
        "count": len(rows_out),
    }
    return rows_out, meta


def export_llm_top_excel(
    run_id: int | None = None,
    *,
    output: Path | str | None = None,
) -> Path:
    """导出 Top N LLM 简评为 ``.xlsx``（含排名、板块、股价、打分）。"""
    rid = run_id if run_id is not None else _latest_llm_top_run_id()
    if rid is None:
        raise LookupError("无 llm_top300 且 llm_enabled 的运行记录")

    rows, meta = build_llm_top_summary_rows(rid)
    df = pd.DataFrame(rows)
    if "代码" in df.columns:
        df["代码"] = df["代码"].astype(str).map(
            lambda c: c.split(".")[0].zfill(6) if c.split(".")[0].isdigit() and len(c.split(".")[0]) <= 6 else c
        )

    if output is None:
        as_of = str(meta.get("trade_date_as_of") or "latest").replace("-", "")
        out_dir = Path(ZPLAN_ROOT) / "pick_exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"llm_top300_run{rid}_{as_of}.xlsx"
    else:
        path = Path(output)

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Top300简评", index=False)
        pd.DataFrame([meta]).to_excel(writer, sheet_name="元数据", index=False)
        ws = writer.sheets["Top300简评"]
        code_col = list(df.columns).index("代码") + 1 if "代码" in df.columns else None
        if code_col:
            for row in range(2, len(df) + 2):
                cell = ws.cell(row=row, column=code_col)
                cell.number_format = "@"
                cell.value = str(cell.value) if cell.value is not None else ""

    return path
