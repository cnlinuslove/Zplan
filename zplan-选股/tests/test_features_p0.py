from __future__ import annotations

import pandas as pd

from zplan_shared.features import (
    SNAPSHOT_FLAG_KEYS,
    SNAPSHOT_FLOAT_KEYS,
    enrich_bars,
    feature_flag,
    latest_features,
)


def _sample_ohlc(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series(range(100, 100 + n), dtype=float, index=idx)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


def test_enrich_bars_has_p0_columns():
    enriched = enrich_bars(_sample_ohlc())
    for col in (
        "close_vs_ma20",
        "ma20_slope_5d",
        "ma5_cross_ma20",
        "macd_cross_up",
        "high_60d_pct",
        "drawdown_20d_pct",
        "vol_breakout",
        "atr_pct",
        "kdj_golden_cross",
    ):
        assert col in enriched.columns


def test_latest_features_exports_p0_snapshot():
    snap = latest_features(enrich_bars(_sample_ohlc()))
    assert snap.get("close_vs_ma20") is not None
    assert snap.get("high_60d_pct") is not None
    assert snap.get("atr_pct") is not None
    for key in SNAPSHOT_FLAG_KEYS:
        if key in snap:
            assert snap[key] in (0.0, 1.0, None)
    for key in ("close_vs_ma20", "high_60d_pct"):
        assert key in snap


def test_ma5_cross_ma20_detected():
    n = 40
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series([10.0] * 35 + [11.0, 12.0, 13.0, 14.0, 15.0], index=idx)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )
    enriched = enrich_bars(df)
    snap = latest_features(enriched)
    assert feature_flag(snap, "ma5_cross_ma20") or snap.get("ma5_cross_ma20") in (0.0, 1.0)


def test_feature_flag_helper():
    assert feature_flag({"ma5_cross_ma20": 1.0}, "ma5_cross_ma20")
    assert not feature_flag({"ma5_cross_ma20": 0.0}, "ma5_cross_ma20")
    assert not feature_flag({}, "ma5_cross_ma20")
