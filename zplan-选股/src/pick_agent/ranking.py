"""选股结果排序（LLM / 规则 / 混合）+ 行业分散。"""
from __future__ import annotations

from typing import Any

from pick_agent.strategy import PickStrategy, final_rank_score


def _apply_sector_cap(
    picks: list[dict[str, Any]],
    max_per_industry: int,
) -> list[dict[str, Any]]:
    """同一行业最多保留 ``max_per_industry`` 只，按 ``final_rank_score`` 择优。"""
    seen: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for p in picks:
        industry = p.get("industry") or "未知"
        count = seen.get(industry, 0)
        if count >= max_per_industry:
            continue
        seen[industry] = count + 1
        result.append(p)
    return result


def _diversify_final_picks(
    picks: list[dict[str, Any]],
    *,
    max_per_industry: int = 3,
    min_industries: int = 5,
) -> list[dict[str, Any]]:
    """最终展示层：行业限额 + 最少行业覆盖。"""
    if max_per_industry <= 0 and min_industries <= 0:
        return picks

    # Phase 1: 行业限额
    capped = _apply_sector_cap(picks, max_per_industry) if max_per_industry > 0 else list(picks)

    # Phase 2: 确保最少行业覆盖
    if min_industries > 0:
        industries_seen: set[str] = set()
        prioritized: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for p in capped:
            ind = p.get("industry") or "未知"
            if ind not in industries_seen:
                prioritized.append(p)
                industries_seen.add(ind)
            else:
                rest.append(p)
        capped = prioritized + rest

    return capped


def sort_picks_for_rank(picks: list[dict[str, Any]], strategy: PickStrategy) -> list[dict[str, Any]]:
    """按 ``strategy.ranking`` 配置排序，写入 ``final_rank_score``，可选行业分散。"""
    for p in picks:
        p["final_rank_score"] = round(
            p.get("adjusted_score") or final_rank_score(p, strategy), 2
        )

    sorted_picks = sorted(
        picks,
        key=lambda x: (
            x.get("final_rank_score") or 0,
            x.get("rule_composite_score") or x.get("composite_score") or 0,
        ),
        reverse=True,
    )

    # 行业分散：Top N 级别（strategy.max_per_industry）
    if strategy.max_per_industry > 0:
        sorted_picks = _apply_sector_cap(sorted_picks, strategy.max_per_industry)

    return sorted_picks


def assign_ranks(picks: list[dict[str, Any]]) -> None:
    for i, p in enumerate(picks, start=1):
        p["rank_in_run"] = i
