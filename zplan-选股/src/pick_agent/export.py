"""导出扫描结果 / 研报，供回测与外部系统消费。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


def _json_default(obj: object) -> object:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"不可序列化: {type(obj)}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def write_picks_csv(path: Path, picks: list[dict[str, Any]], *, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(picks)
    if not df.empty:
        for k, v in meta.items():
            df[k] = v
    df.to_csv(path, index=False, encoding="utf-8-sig")


def build_signal_export(scan_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent": "pick",
        "as_of": scan_result.get("as_of"),
        "rule_version": scan_result.get("rule_version"),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "signals": [
            {
                "ts_code": p["ts_code"],
                "signal_date": scan_result.get("as_of"),
                "tech_score": p.get("tech_score"),
                "composite_score": p.get("composite_score"),
                "verdict": p.get("verdict"),
                "signals": p.get("signals"),
            }
            for p in scan_result.get("picks") or []
        ],
    }
