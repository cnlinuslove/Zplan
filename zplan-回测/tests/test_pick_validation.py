from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from zplan_shared.features import suggested_price_levels
from zplan_shared.pick_predictions import evaluate_outcome, price_levels_from_report


def _bars(n: int = 80, *, start: str = "2025-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="B")
    close = pd.Series(range(100, 100 + n), dtype=float, index=idx)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 1_000_000,
        },
        index=idx,
    )


def test_suggested_price_levels_has_buy():
    lv = suggested_price_levels(_bars())
    assert lv.get("suggested_buy") is not None
    assert lv["suggested_buy"] <= lv["close"]


def test_price_levels_from_report():
    report = {
        "投资建议": {"建议买入价": 9.5, "目标价": 11.0, "止损参考": 8.8},
        "llm": {"buy_price": 9.8},
    }
    p = price_levels_from_report(report)
    assert p["predicted_buy_price"] == 9.5


class _Entry:
    id = 1
    ts_code = "000001"
    predicted_buy_price = 98.0
    predicted_target_price = 110.0
    predicted_stop_loss = 90.0
    price_source = "rule"
    report_json = None
    analysis_process_json = None
    close_price = 100.0
    created_at_utc = datetime(2025, 6, 1)


class _Run:
    trade_date_as_of = date(2025, 5, 30)


def test_evaluate_outcome_monkeypatch(monkeypatch: pytest.MonkeyPatch):
    bars = _bars(60, start="2025-04-01")
    monkeypatch.setattr(
        "zplan_shared.pick_predictions.get_bars",
        lambda code, **kw: bars,
    )
    out = evaluate_outcome(_Entry(), _Run(), horizon_days=5)
    assert out["status"] in ("complete", "partial", "pending")
    if out["status"] != "pending":
        assert out["min_low"] is not None
        assert "buy_touched" in out
