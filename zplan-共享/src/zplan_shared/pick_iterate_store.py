"""迭代轮次持久化（JSONL + 快照）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zplan_shared.config import ZPLAN_ROOT

INDEX_NAME = "iterations.jsonl"


def iteration_dir() -> Path:
    d = Path(ZPLAN_ROOT) / "backtest_review" / "iterations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return iteration_dir() / INDEX_NAME


def append_iteration(record: dict[str, Any]) -> Path:
    """追加索引并写入当轮快照。"""
    iid = str(record["iteration_id"])
    snap = iteration_dir() / f"{iid}.json"
    snap.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with _index_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps({"iteration_id": iid, "pick_run_id": record.get("pick_run_id"), "phase": record.get("phase"), "created_at_utc": record.get("created_at_utc"), "metrics": record.get("metrics")}, ensure_ascii=False, default=str) + "\n")
    return snap


def list_iterations(*, limit: int = 20) -> list[dict[str, Any]]:
    idx = _index_path()
    if not idx.is_file():
        return []
    lines = idx.read_text(encoding="utf-8").strip().splitlines()
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-limit:]):
        if line.strip():
            out.append(json.loads(line))
    return out


def load_iteration(iteration_id: str) -> dict[str, Any] | None:
    p = iteration_dir() / f"{iteration_id}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def compare_iterations(
    older: dict[str, Any] | None,
    newer: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not older or not newer:
        return None
    om = older.get("metrics") or {}
    nm = newer.get("metrics") or {}
    keys = (
        "fail_rate",
        "mean_delta",
        "mean_fwd_return",
        "mean_llm",
        "llm_worse_than_rule",
    )
    deltas: dict[str, Any] = {}
    for k in keys:
        o, n = om.get(k), nm.get(k)
        if o is not None and n is not None:
            try:
                deltas[k] = round(float(n) - float(o), 4)
            except (TypeError, ValueError):
                pass
    return {
        "older_id": older.get("iteration_id"),
        "newer_id": newer.get("iteration_id"),
        "older_run": older.get("pick_run_id"),
        "newer_run": newer.get("pick_run_id"),
        "deltas": deltas,
        "improved": (
            (deltas.get("fail_rate") is not None and deltas["fail_rate"] < 0)
            or (deltas.get("mean_fwd_return") is not None and deltas["mean_fwd_return"] > 0)
        ),
    }
