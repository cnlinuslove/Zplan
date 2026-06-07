"""对话质量迭代记录 — JSONL 索引 + 快照，追踪每周审查与优化动作。

数据目录: ``{ZPLAN_ROOT}/backtest_review/chat_iterations/``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zplan_shared.config import ZPLAN_ROOT

INDEX_NAME = "chat_iterations.jsonl"


def _chat_iter_dir() -> Path:
    d = Path(ZPLAN_ROOT) / "backtest_review" / "chat_iterations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return _chat_iter_dir() / INDEX_NAME


def append_chat_iteration(record: dict[str, Any]) -> Path:
    """追加一条对话质量迭代记录（索引 + 完整快照）。"""
    iid = str(record["iteration_id"])
    snap = _chat_iter_dir() / f"{iid}.json"
    snap.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    # 索引行：精简版，方便快速扫描
    index_entry = {
        "iteration_id": iid,
        "created_at_utc": record.get("created_at_utc"),
        "review_days": record.get("review_days"),
        "sample_count": record.get("sample_count"),
        "metrics": record.get("metrics"),
    }
    with _index_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(index_entry, ensure_ascii=False, default=str) + "\n")
    return snap


def list_chat_iterations(*, limit: int = 20) -> list[dict[str, Any]]:
    """列出最近 N 轮对话质量审查索引。"""
    idx = _index_path()
    if not idx.is_file():
        return []
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-limit:]):
        if line.strip():
            out.append(json.loads(line))
    return out


def load_chat_iteration(iteration_id: str) -> dict[str, Any] | None:
    """加载单轮完整快照。"""
    p = _chat_iter_dir() / f"{iteration_id}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def compare_chat_iterations(
    older: dict[str, Any] | None,
    newer: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """对比两轮对话质量，返回变化 delta。"""
    if not older or not newer:
        return None
    om = older.get("metrics") or {}
    nm = newer.get("metrics") or {}

    deltas: dict[str, Any] = {}

    # 综合均分变化
    if "mean_overall" in om and "mean_overall" in nm:
        deltas["mean_overall"] = round(nm["mean_overall"] - om["mean_overall"], 2)

    # 各维度变化
    om_dims = om.get("dim_means") or {}
    nm_dims = nm.get("dim_means") or {}
    dim_deltas = {}
    for dim in set(list(om_dims.keys()) + list(nm_dims.keys())):
        o = om_dims.get(dim, 0)
        n = nm_dims.get(dim, 0)
        if o or n:
            dim_deltas[dim] = round(n - o, 2)
    if dim_deltas:
        deltas["dim_means"] = dim_deltas

    # Bad case 数量变化
    if "bad_case_count" in om and "bad_case_count" in nm:
        deltas["bad_case_count"] = nm["bad_case_count"] - om["bad_case_count"]

    # 判断是否改善
    improved = (
        deltas.get("mean_overall", 0) > 0
        or deltas.get("bad_case_count", 0) < 0
    )

    # 上次优化动作是否生效
    prev_actions = older.get("optimization_actions") or []
    action_results = []
    if prev_actions and dim_deltas:
        for action in prev_actions:
            target_dim = action.get("target_dimension", "")
            action_desc = action.get("action", "")[:80]
            delta = dim_deltas.get(target_dim, 0)
            action_results.append({
                "action": action_desc,
                "target_dimension": target_dim,
                "dim_delta": delta,
                "effective": delta > 0,
            })

    return {
        "older_id": older.get("iteration_id"),
        "newer_id": newer.get("iteration_id"),
        "deltas": deltas,
        "improved": improved,
        "action_results": action_results,
    }


def format_action_history(limit: int = 5) -> str:
    """将近期优化动作历史格式化为 prompt 可用的文本。"""
    iterations = list_chat_iterations(limit=limit)
    if not iterations:
        return "(无历史优化记录)"

    lines = ["## 历史优化记录（最近轮次）"]
    for it in iterations:
        iid = it.get("iteration_id", "?")
        m = it.get("metrics") or {}
        mean = m.get("mean_overall", "?")
        lines.append(f"\n### {iid}（综合 {mean}）")

        # 加载完整快照获取优化动作
        full = load_chat_iteration(iid)
        if full:
            actions = full.get("optimization_actions") or []
            for a in actions:
                dim = a.get("target_dimension", "")
                act = a.get("action", "")
                status = a.get("status", "pending")
                icon = {"applied": "✅", "pending": "⏳", "reverted": "❌"}.get(status, "▪️")
                lines.append(f"- {icon} [{dim}] {act}")

            # 如果后续有对比结果
            comp = full.get("comparison_with_previous")
            if comp:
                improved = "📈 改善" if comp.get("improved") else "📉 未改善"
                lines.append(f"  > 对比上轮: {improved}")
                for ar in comp.get("action_results") or []:
                    eff = "✅ 有效" if ar.get("effective") else "❌ 无效"
                    lines.append(f"  > {eff}: {ar.get('action', '')[:60]}")

    return "\n".join(lines)
