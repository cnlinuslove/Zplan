from __future__ import annotations

import json

import pandas as pd
import pytest

from zplan_shared.features import add_kdj, add_macd, enrich_bars

from pick_agent.export import build_signal_export
from pick_agent.scoring import quick_technical_score


def _sample_ohlc(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series(range(100, 100 + n), dtype=float, index=idx)
    df = pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000_000,
        },
        index=idx,
    )
    df.index.name = "trade_date"
    return df


def test_kdj_columns():
    enriched = enrich_bars(_sample_ohlc())
    assert "kdj_k" in enriched.columns
    assert enriched["kdj_k"].iloc[-1] == enriched["kdj_k"].iloc[-1]


def test_macd_hist():
    out = add_macd(_sample_ohlc())
    assert "macd_hist" in out.columns


def test_quick_score_range():
    f = {"ma5": 10, "ma20": 9, "ma60": 8, "kdj_k": 60, "kdj_d": 50, "ret_20d": 6, "macd_hist": 0.1}
    s = quick_technical_score(f)
    assert 0 <= s <= 100


def test_signal_export_json_serializable():
    payload = build_signal_export(
        {
            "as_of": "2026-05-18",
            "rule_version": "test",
            "picks": [{"ts_code": "000001", "tech_score": 70, "composite_score": 72, "verdict": "偏多", "signals": []}],
        }
    )
    json.dumps(payload, ensure_ascii=False)
