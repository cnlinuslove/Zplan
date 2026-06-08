"""L2 衍生特征：对 ``get_bars`` 结果做向量化技术指标（Phase A.2）。

更新机制：所有指标经 ``enrich_bars`` 一次计算 → ``latest_features`` 导出快照列；
全市场扫描与单票 ``analyze_technical`` 共用同一套字段（见 ``SNAPSHOT_FLOAT_KEYS`` / ``SNAPSHOT_FLAG_KEYS``）。
"""
from __future__ import annotations

import pandas as pd

# 快照导出键（新增 P0 字段时同步维护此处 + enrich_bars/add_p0_signals）
SNAPSHOT_FLOAT_KEYS = (
    "close",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "rsi14",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "atr14",
    "atr_pct",
    "ret_5d",
    "ret_20d",
    "ret_60d",
    "vol_ratio20",
    "close_vs_ma20",
    "ma20_slope_5d",
    "high_60d_pct",
    "drawdown_20d_pct",
    "pct_chg",
    "turnover_rate",
)
SNAPSHOT_FLAG_KEYS = (
    "above_ma20",
    "ma5_cross_ma20",
    "macd_cross_up",
    "vol_breakout",
    "kdj_golden_cross",
    "kdj_death_cross",
)


def _require_ohlc(df: pd.DataFrame) -> None:
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"缺少 OHLC 列: {missing}")


def add_ma(df: pd.DataFrame, *windows: int, prefix: str = "ma") -> pd.DataFrame:
    _require_ohlc(df)
    out = df.copy()
    for w in windows:
        out[f"{prefix}{w}"] = out["close"].rolling(w, min_periods=w).mean()
    return out


def add_ema(df: pd.DataFrame, span: int, *, col: str = "close", name: str | None = None) -> pd.DataFrame:
    _require_ohlc(df)
    out = df.copy()
    out[name or f"ema{span}"] = out[col].ewm(span=span, adjust=False).mean()
    return out


def pct_return(df: pd.DataFrame, n: int, *, col: str = "close") -> pd.Series:
    return df[col].pct_change(n) * 100


def add_macd(
    df: pd.DataFrame,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    _require_ohlc(df)
    out = df.copy()
    ema_fast = out["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = out["close"].ewm(span=slow, adjust=False).mean()
    out["macd_dif"] = ema_fast - ema_slow
    out["macd_dea"] = out["macd_dif"].ewm(span=signal, adjust=False).mean()
    out["macd_hist"] = 2 * (out["macd_dif"] - out["macd_dea"])
    return out


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    _require_ohlc(df)
    out = df.copy()
    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out[f"rsi{period}"] = 100 - (100 / (1 + rs))
    return out


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    _require_ohlc(df)
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out[f"atr{period}"] = tr.ewm(alpha=1 / period, adjust=False).mean()
    return out


def _smooth_cn(prev: float | None, value: float, n: int) -> float:
    if prev is None or pd.isna(prev):
        return float(value)
    return (n - 1) / n * prev + value / n


def add_kdj(df: pd.DataFrame, n: int = 9, k_period: int = 3, d_period: int = 3) -> pd.DataFrame:
    """A 股常用 KDJ(9,3,3)：K/D 为递推平滑，J=3K-2D。"""
    _require_ohlc(df)
    out = df.copy()
    low_n = out["low"].rolling(n, min_periods=n).min()
    high_n = out["high"].rolling(n, min_periods=n).max()
    denom = (high_n - low_n).replace(0, pd.NA)
    rsv = ((out["close"] - low_n) / denom * 100).fillna(50.0)

    k_vals: list[float] = []
    d_vals: list[float] = []
    k_prev: float | None = None
    d_prev: float | None = None
    for r in rsv:
        if pd.isna(r):
            k_vals.append(pd.NA)
            d_vals.append(pd.NA)
            k_prev, d_prev = None, None
            continue
        k_now = _smooth_cn(k_prev, float(r), k_period)
        d_now = _smooth_cn(d_prev, k_now, d_period)
        k_vals.append(k_now)
        d_vals.append(d_now)
        k_prev, d_prev = k_now, d_now

    out["kdj_k"] = k_vals
    out["kdj_d"] = d_vals
    out["kdj_j"] = 3 * out["kdj_k"] - 2 * out["kdj_d"]
    return out


def add_p0_signals(df: pd.DataFrame) -> pd.DataFrame:
    """P0 快照：均线距离/斜率、金叉布尔、新高比、回撤、放量、波动率归一。"""
    if df.empty:
        return df
    out = df.copy()
    close = out["close"]

    if "ma20" in out.columns:
        out["close_vs_ma20"] = (close / out["ma20"] - 1) * 100
        out["ma20_slope_5d"] = out["ma20"].pct_change(5) * 100
        out["above_ma20"] = close > out["ma20"]

    if "ma5" in out.columns and "ma20" in out.columns:
        out["ma5_cross_ma20"] = (out["ma5"].shift(1) <= out["ma20"].shift(1)) & (
            out["ma5"] > out["ma20"]
        )

    if "macd_hist" in out.columns:
        out["macd_cross_up"] = (out["macd_hist"].shift(1) <= 0) & (out["macd_hist"] > 0)

    high_60 = out["high"].rolling(60, min_periods=20).max()
    out["high_60d_pct"] = close / high_60 * 100

    high_20 = out["high"].rolling(20, min_periods=10).max()
    out["drawdown_20d_pct"] = (close / high_20 - 1) * 100

    if "vol_ratio20" in out.columns:
        out["vol_breakout"] = out["vol_ratio20"] >= 1.5

    if "atr14" in out.columns:
        out["atr_pct"] = out["atr14"] / close * 100

    if "kdj_k" in out.columns and "kdj_d" in out.columns:
        out["kdj_golden_cross"] = (out["kdj_k"].shift(1) <= out["kdj_d"].shift(1)) & (
            out["kdj_k"] > out["kdj_d"]
        )
        out["kdj_death_cross"] = (out["kdj_k"].shift(1) >= out["kdj_d"].shift(1)) & (
            out["kdj_k"] < out["kdj_d"]
        )

    return out


def enrich_bars(df: pd.DataFrame) -> pd.DataFrame:
    """常用指标一次补齐（MA / MACD / RSI / KDJ / ATR / 动量 / P0 快照列）。"""
    if df.empty:
        return df
    out = add_ma(df, 5, 10, 20, 60)
    out = add_macd(out)
    out = add_rsi(out, 14)
    out = add_kdj(out)
    out = add_atr(out, 14)
    out["ret_5d"] = pct_return(out, 5)
    out["ret_20d"] = pct_return(out, 20)
    out["ret_60d"] = pct_return(out, 60)
    if "volume" in out.columns:
        out["vol_ma20"] = out["volume"].rolling(20, min_periods=20).mean()
        out["vol_ratio20"] = out["volume"] / out["vol_ma20"]
    return add_p0_signals(out)


def _snapshot_scalar(v, *, as_flag: bool = False) -> float | None:
    if pd.isna(v):
        return None
    if as_flag:
        return 1.0 if bool(v) else 0.0
    return round(float(v), 4)


def feature_flag(features: dict[str, float | None], key: str) -> bool:
    """解读 ``latest_features`` 中的 0/1 布尔快照。"""
    v = features.get(key)
    return v is not None and float(v) >= 0.5


def scan_universe_features(history: pd.DataFrame, *, min_bars: int = 60) -> pd.DataFrame:
    """
    长表日线 → 每票末行指标快照（向量化预筛，避免全市场逐票 ``get_bars``）。

    ``history`` 列需含 ``ts_code``, ``trade_date``, OHLCV 等。
    """
    if history.empty or "ts_code" not in history.columns:
        return pd.DataFrame()

    parts: list[pd.DataFrame] = []
    for code, grp in history.groupby("ts_code", sort=False):
        g = grp.sort_values("trade_date").set_index("trade_date")
        if len(g) < min_bars:
            continue
        enriched = enrich_bars(g)
        snap = latest_features(enriched)
        if not snap:
            continue
        snap["ts_code"] = code
        parts.append(pd.DataFrame([snap]))

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def latest_features(df: pd.DataFrame) -> dict[str, float | None]:
    """取最后一行有效指标快照（布尔列导出为 0.0 / 1.0）。"""
    if df.empty:
        return {}
    row = df.iloc[-1]
    out: dict[str, float | None] = {}
    for k in SNAPSHOT_FLOAT_KEYS:
        if k not in df.columns:
            continue
        out[k] = _snapshot_scalar(row[k])
    for k in SNAPSHOT_FLAG_KEYS:
        if k not in df.columns:
            continue
        out[k] = _snapshot_scalar(row[k], as_flag=True)
    return out


def suggested_price_levels(bars: pd.DataFrame) -> dict[str, float | None]:
    """支撑/阻力与建议价位（近端高低点 + MA20 + ATR）。"""
    if bars.empty:
        return {}
    recent = bars.tail(20)
    close = float(bars["close"].iloc[-1])
    support = float(recent["low"].min())
    resistance = float(recent["high"].max())
    enriched = enrich_bars(bars)
    ma20 = enriched["ma20"].iloc[-1] if "ma20" in enriched.columns else None
    atr = enriched["atr14"].iloc[-1] if "atr14" in enriched.columns else None

    # 主买入价：收盘价 × 0.99，仅留 1% 的 T+1 次日开盘正常滑点
    # 回测验证：0.98 折扣导致 buy_unreachable 率 100%，买入价过于激进
    buy = close * 0.99
    if support > buy:
        buy = min(support, close * 0.995)

    # 深度回调买入价（参考）：底线距市价 ≤3%（原是 5%，改为更贴近市价）
    dip_buy = support
    if ma20 is not None and not pd.isna(ma20):
        dip_buy = min(dip_buy, float(ma20) * 0.98)
    dip_buy = max(dip_buy, close * 0.97)

    target = resistance
    if atr is not None and not pd.isna(atr):
        target = max(target, close + 2 * float(atr))

    stop = support * 0.97 if support else None
    return {
        "close": round(close, 4),
        "support_20d": round(support, 4),
        "resistance_20d": round(resistance, 4),
        "suggested_buy": round(buy, 4),
        "dip_buy": round(dip_buy, 4),
        "target_price": round(target, 4),
        "stop_loss": round(stop, 4) if stop else None,
    }
