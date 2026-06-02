"""个股题材/概念标签（东财概念成份缓存 stock_concept_members）。"""
from __future__ import annotations

from zplan_shared.market import get_stock_concepts


def concepts_for_code(ts_code: str, *, limit: int = 8) -> list[str]:
    return get_stock_concepts(ts_code)[:limit]


def attach_concepts(pick: dict, *, limit: int = 8) -> dict:
    code = pick.get("ts_code")
    if not code:
        return pick
    names = concepts_for_code(str(code), limit=limit)
    out = {**pick}
    if names:
        out["concepts"] = names
        out["concepts_str"] = "；".join(names[:6])
        out["concept_count"] = len(names)
    else:
        out["concepts"] = []
        out["concepts_str"] = None
        out["concept_count"] = 0
    return out
