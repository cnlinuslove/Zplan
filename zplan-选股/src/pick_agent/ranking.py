"""选股结果排序（LLM / 规则 / 混合）。"""
from __future__ import annotations

from typing import Any

from pick_agent.strategy import PickStrategy, final_rank_score


def sort_picks_for_rank(picks: list[dict[str, Any]], strategy: PickStrategy) -> list[dict[str, Any]]:
    """按 ``strategy.ranking`` 配置排序，并写入 ``final_rank_score``。"""
    for p in picks:
        p["final_rank_score"] = round(final_rank_score(p, strategy), 2)
    return sorted(
        picks,
        key=lambda x: (
            x.get("final_rank_score") or 0,
            x.get("llm_composite_score") or 0,
            x.get("rule_composite_score") or x.get("composite_score") or 0,
        ),
        reverse=True,
    )


def assign_ranks(picks: list[dict[str, Any]]) -> None:
    for i, p in enumerate(picks, start=1):
        p["rank_in_run"] = i
