"""条件筛选 CLI 逻辑（题材 / 行业 / 规则分 / LLM 分）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from zplan_shared.concept_screen import (
    ensure_concept_cached,
    get_concept_members,
    list_cached_concepts,
    screen_universe,
    sync_concept_members,
)
from zplan_shared.pick_store import get_run
from zplan_shared.stock_rule_scores import latest_score_date, top_rule_scores

from pick_agent.strategy import load_strategy


def merge_rule_scores(df: pd.DataFrame, *, min_score: float | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    strat = load_strategy()
    as_of = latest_score_date(rule_version=strat.rule_version)
    if not as_of:
        return df
    rules = top_rule_scores(
        trade_date_as_of=as_of,
        rule_version=strat.rule_version,
        top_n=10000,
    )
    rdf = pd.DataFrame(rules)
    if rdf.empty:
        return df
    cols = ["ts_code", "composite_score", "tech_score", "verdict", "rank_rule"]
    rdf = rdf[[c for c in cols if c in rdf.columns]].rename(
        columns={
            "composite_score": "rule_composite_score",
            "rank_rule": "rule_rank",
        }
    )
    out = df.merge(rdf, on="ts_code", how="left")
    if min_score is not None:
        out = out[out["rule_composite_score"].fillna(-1) >= float(min_score)]
    return out


def merge_llm_scores(df: pd.DataFrame, run_id: int) -> pd.DataFrame:
    data = get_run(run_id)
    if not data or df.empty:
        return df
    picks = pd.DataFrame(
        [
            {
                "ts_code": e["ts_code"],
                "llm_rank": e.get("rank"),
                "llm_composite_score": e.get("llm_composite_score"),
                "llm_recommendation": e.get("recommendation"),
            }
            for e in data["entries"]
        ]
    )
    return df.merge(picks, on="ts_code", how="left")


def filter_max_ret_20d(df: pd.DataFrame, max_ret: float) -> pd.DataFrame:
    """剔除 20 日涨幅过高的票（减轻「追高」）。"""
    if df.empty:
        return df
    strat = load_strategy()
    as_of = latest_score_date(rule_version=strat.rule_version)
    if not as_of:
        return df
    from zplan_shared.models import SessionLocal, StockRuleScore, init_db
    from sqlalchemy import select

    init_db()
    codes = df["ts_code"].tolist()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockRuleScore.ts_code, StockRuleScore.features_json).where(
                StockRuleScore.ts_code.in_(codes),
                StockRuleScore.trade_date_as_of == as_of,
                StockRuleScore.rule_version == strat.rule_version,
            )
        ).all()
    ret_map: dict[str, float | None] = {}
    for code, fj in rows:
        if fj:
            try:
                ret_map[code] = json.loads(fj).get("ret_20d")
            except json.JSONDecodeError:
                ret_map[code] = None
    out = df.copy()
    out["ret_20d"] = out["ts_code"].map(ret_map)
    return out[out["ret_20d"].isna() | (out["ret_20d"] <= float(max_ret))]


def run_screen(
    *,
    concept: str | None = None,
    industry: str | None = None,
    name_like: str | None = None,
    min_rule_score: float | None = None,
    max_ret_20d: float | None = None,
    llm_run_id: int | None = None,
    refresh_concept: bool = False,
    output: Path | str | None = None,
) -> dict[str, Any]:
    df = screen_universe(
        concept=concept,
        industry=industry,
        name_like=name_like,
        refresh_concept=refresh_concept,
    )
    if min_rule_score is not None:
        df = merge_rule_scores(df, min_score=min_rule_score)
    if max_ret_20d is not None:
        df = filter_max_ret_20d(df, max_ret_20d)
    if llm_run_id is not None:
        df = merge_llm_scores(df, llm_run_id)

    path = None
    if output and not df.empty:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in (".xlsx", ".xls"):
            df.to_excel(path, index=False, engine="openpyxl")
        else:
            df.to_csv(path, index=False, encoding="utf-8-sig")

    return {"count": len(df), "path": str(path) if path else None, "dataframe": df}
