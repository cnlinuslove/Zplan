"""近端分时 Parquet 存储（Phase A.1）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from zplan_shared.config import PARQUET_ROOT

INTRADAY_COLUMNS = (
    "bar_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pct_chg",
    "turnover_rate",
    "source",
    "ingested_at",
)


def intraday_parquet_path(ts_code: str, period: str) -> Path:
    return PARQUET_ROOT / "intraday" / f"period={period}" / f"ts_code={ts_code}.parquet"


def normalize_intraday_df(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["ts_code", "period", *INTRADAY_COLUMNS])

    out = df.copy()
    time_col = "时间" if "时间" in out.columns else "bar_time"
    out["bar_time"] = pd.to_datetime(out[time_col], errors="coerce")
    out = out.dropna(subset=["bar_time"])

    rename = {
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "pct_chg",
        "换手率": "turnover_rate",
    }
    for src, dst in rename.items():
        if src in out.columns:
            out[dst] = pd.to_numeric(out[src], errors="coerce")

    ingested_at = datetime.utcnow()
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover_rate"):
        if col not in out.columns:
            out[col] = None
    out["source"] = source
    out["ingested_at"] = ingested_at
    return out[
        ["bar_time", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover_rate", "source", "ingested_at"]
    ]


def upsert_intraday_parquet(ts_code: str, period: str, df: pd.DataFrame) -> int:
    normalized = normalize_intraday_df(df, source="akshare_em")
    if normalized.empty:
        return 0

    path = intraday_parquet_path(ts_code, period)
    path.parent.mkdir(parents=True, exist_ok=True)

    normalized = normalized.sort_values("bar_time").drop_duplicates(subset=["bar_time"], keep="last")
    if path.exists():
        existing = pd.read_parquet(path)
        if "bar_time" in existing.columns:
            existing["bar_time"] = pd.to_datetime(existing["bar_time"])
        merged = pd.concat([existing, normalized], ignore_index=True)
        merged = merged.sort_values("bar_time").drop_duplicates(subset=["bar_time"], keep="last")
    else:
        merged = normalized

    merged.to_parquet(path, index=False)
    return len(normalized)


def read_intraday_parquet(
    ts_code: str,
    period: str,
    *,
    start: datetime | pd.Timestamp | None = None,
    end: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    path = intraday_parquet_path(ts_code, period)
    if not path.exists():
        return pd.DataFrame(columns=list(INTRADAY_COLUMNS))

    df = pd.read_parquet(path)
    df["bar_time"] = pd.to_datetime(df["bar_time"])
    if start is not None:
        df = df[df["bar_time"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["bar_time"] <= pd.Timestamp(end)]
    return df.sort_values("bar_time")
