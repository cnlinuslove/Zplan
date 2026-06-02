"""加载 ``config/strategy.yaml``。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PKG_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STRATEGY_PATH = _PKG_ROOT / "config" / "strategy.yaml"


@dataclass
class PickStrategy:
    version: str = "1"
    rule_version: str = "pick-default"
    min_bars: int = 60
    min_score: float = 55.0
    min_turnover_rate: float = 0.5
    min_volume: float = 0.0
    exclude_st: bool = True
    exclude_bj: bool = False
    prefilter_top_multiplier: int = 5
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "technical": 0.65,
            "financial": 0.15,
            "news": 0.10,
            "industry_relative": 0.10,
        }
    )
    filters: dict[str, Any] = field(default_factory=dict)
    require_any_signals: list[str] = field(default_factory=list)
    intraday: dict[str, float] = field(default_factory=dict)
    min_panel_rows: int = 300
    max_stale_days: int = 3
    llm_enabled: bool = True
    llm_model: str = "gemini-2.5-pro"
    llm_scan_brief: bool = True
    llm_top_n: int = 300
    llm_batch_size: int = 10
    ranking_mode: str = "llm_primary"
    ranking_llm_weight: float = 0.75
    ranking_rule_weight: float = 0.25
    resort_after_llm: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


def load_strategy(path: Path | str | None = None) -> PickStrategy:
    p = Path(path) if path else DEFAULT_STRATEGY_PATH
    if not p.is_file():
        return PickStrategy()

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    scan = data.get("scan") or {}
    w = data.get("weights") or {}
    intraday = data.get("intraday") or {}
    mh = data.get("market_health") or {}
    llm = data.get("llm") or {}
    rule_init = data.get("rule_init") or {}
    ranking = data.get("ranking") or {}

    return PickStrategy(
        version=str(data.get("version", "1")),
        rule_version=str(data.get("rule_version", "pick-default")),
        min_bars=int(scan.get("min_bars", 60)),
        min_score=float(scan.get("min_score", 55.0)),
        min_turnover_rate=float(scan.get("min_turnover_rate", 0.5)),
        min_volume=float(scan.get("min_volume", 0)),
        exclude_st=bool(scan.get("exclude_st", True)),
        exclude_bj=bool(scan.get("exclude_bj", False)),
        prefilter_top_multiplier=int(scan.get("prefilter_top_multiplier", 5)),
        weights={
            "technical": float(w.get("technical", 0.65)),
            "financial": float(w.get("financial", 0.15)),
            "news": float(w.get("news", 0.10)),
            "industry_relative": float(w.get("industry_relative", 0.10)),
        },
        filters=dict(data.get("filters") or {}),
        require_any_signals=list((data.get("signals") or {}).get("require_any") or []),
        intraday={k: float(v) for k, v in intraday.items()},
        min_panel_rows=int(mh.get("min_panel_rows", 300)),
        max_stale_days=int(mh.get("max_stale_days", 3)),
        llm_enabled=bool(llm.get("enabled", True)),
        llm_model=str(llm.get("model", "gemini-2.5-pro")),
        llm_scan_brief=bool(llm.get("scan_brief", True)),
        llm_top_n=int(rule_init.get("llm_top_n", 300)),
        llm_batch_size=int(rule_init.get("llm_batch_size", 15)),
        ranking_mode=str(ranking.get("mode", "llm_primary")),
        ranking_llm_weight=float(ranking.get("llm_weight", 0.75)),
        ranking_rule_weight=float(ranking.get("rule_weight", 0.25)),
        resort_after_llm=bool(ranking.get("resort_after_llm", True)),
        raw=data,
    )


def final_rank_score(p: dict[str, Any], strategy: PickStrategy) -> float:
    """排序用最终分：默认 LLM 为主，无 LLM 时回退规则分。"""
    rule = float(p.get("rule_composite_score") or p.get("composite_score") or 0)
    llm = p.get("llm_composite_score")
    mode = (strategy.ranking_mode or "llm_primary").lower()
    if mode == "rule_primary":
        return rule
    if mode == "blend":
        lw = strategy.ranking_llm_weight
        rw = strategy.ranking_rule_weight
        llm_v = float(llm) if llm is not None else rule
        return lw * llm_v + rw * rule
    # llm_primary
    if llm is not None:
        return float(llm)
    return rule
