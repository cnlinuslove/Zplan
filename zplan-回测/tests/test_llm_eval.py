from __future__ import annotations

import pandas as pd
import pytest

from zplan_shared.pick_llm_eval import diagnose_entry, build_optimization_map


class _Entry:
    id = 1
    ts_code = "000001"
    name = "测试"
    rank_in_run = 1
    llm_composite_score = 92.0
    rule_composite_score = 84.0
    recommendation = "推荐"
    close_price = 10.5
    predicted_buy_price = 9.8
    analysis_process_json = (
        '{"ret_20d": 12.5, "high_60d_pct": 0.95, '
        '"llm_brief": {"trend": "均线多头排列，技术形态强劲", "recommendation": "推荐"}}'
    )
    report_json = None
    created_at_utc = None


class _Run:
    id = 8
    trade_date_as_of = __import__("datetime").date(2026, 5, 21)


def test_diagnose_momentum_chase(monkeypatch: pytest.MonkeyPatch):
    idx = pd.date_range("2026-04-01", periods=40, freq="B")
    close = pd.Series([10.0] * 40, index=idx)
    bars = pd.DataFrame(
        {"open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1e6},
        index=idx,
    )

    monkeypatch.setattr("zplan_shared.pick_llm_eval.get_bars", lambda code, **kw: bars)
    out = diagnose_entry(_Entry(), _Run(), horizon_days=5)
    assert "momentum_chase" in out["failure_tags"]
    assert out["verdict"] in ("fail", "pending", "inconclusive")


def test_optimization_map_has_prompt_hints():
    rows = [{"score_delta": 8, "failure_tags": ["momentum_chase", "score_inflation"]}]
    opt = build_optimization_map({"momentum_chase": 5, "score_inflation": 4}, rows)
    assert opt["where_to_change"]["prompt"]
