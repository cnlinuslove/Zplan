"""数据集构建 — 从 labeled events 构建模型训练/验证/测试集。

关键设计：
- 严格按时序切分（不随机），防止前视偏差
- 支持方案 A（纯序列特征）和方案 B（序列 + 上下文）两种特征提取
- 支持滚动窗口 walk-forward 评估
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Sequence

import numpy as np
import pandas as pd

from zplan_shared.patterns.labeling_config import DEFAULT_CONFIG, LabelingConfig, PatternLabel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Dataset configuration
# ─────────────────────────────────────────────


@dataclass
class SplitConfig:
    """数据划分配置。"""

    train_end: date
    """训练集截止日期（含）"""

    val_end: date
    """验证集截止日期（含）"""

    test_end: date | None = None
    """测试集截止日期（含）；None 表示用到最新数据"""

    purge_days: int = 60
    """清除天数：事件的前视窗口与下一划分不应有重叠"""


# ─────────────────────────────────────────────
#  Temporal split
# ─────────────────────────────────────────────


def temporal_split(
    events: pd.DataFrame,
    split: SplitConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按时序将 labeled events 划分为 train/val/test。

    Parameters
    ----------
    events : DataFrame
        需包含 ``event_date`` 列（Date 类型）。
    split : SplitConfig
        划分配置。

    Returns
    -------
    (train_df, val_df, test_df) : tuple[DataFrame, DataFrame, DataFrame]
    """
    if events.empty or "event_date" not in events.columns:
        logger.warning("events 为空或缺少 event_date 列")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df = events.copy()
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date

    train = df[df["event_date"] <= split.train_end].copy()
    val = df[
        (df["event_date"] > split.train_end + timedelta(days=split.purge_days))
        & (df["event_date"] <= split.val_end)
    ].copy()
    test = pd.DataFrame()
    if split.test_end is not None:
        test = df[
            (df["event_date"] > split.val_end + timedelta(days=split.purge_days))
            & (df["event_date"] <= split.test_end)
        ].copy()

    logger.info(
        "时序划分: train=%d, val=%d, test=%d",
        len(train), len(val), len(test),
    )

    return train, val, test


# ─────────────────────────────────────────────
#  Feature extraction for Approach A (pure price)
# ─────────────────────────────────────────────


def extract_sequence_features(
    bars: pd.DataFrame,
    event_date: date,
    *,
    seq_len: int = 60,
    config: LabelingConfig | None = None,
) -> np.ndarray | None:
    """从事件发生前的 K 线提取归一化序列特征（方案 A）。

    提取事件日 T 之前 seq_len 个交易日的 OHLCV 序列，
    归一化到事件日收盘价。

    Parameters
    ----------
    bars : DataFrame
        单票日线（需排序，包含 OHLCV）。
    event_date : date
        事件日期 T。
    seq_len : int
        序列长度（交易日数）。
    config : LabelingConfig, optional

    Returns
    -------
    ndarray shape (seq_len, n_features) or None
        若数据不足则返回 None。
    """
    _ = config or DEFAULT_CONFIG
    if bars.empty:
        return None

    df = _ensure_trade_date_column(bars.sort_values("trade_date"))
    df["trade_date_dt"] = pd.to_datetime(df["trade_date"]).dt.date

    # 只取事件日及之前的 K 线
    before = df[df["trade_date_dt"] <= event_date]
    if len(before) < seq_len + 1:
        return None

    # 取最后 seq_len 根 K 线（包含事件日）
    window = before.tail(seq_len + 1)  # +1 for the event day itself

    # 以事件日收盘价为基准归一化
    ref_close = float(window["close"].iloc[-1])
    if ref_close <= 0:
        return None

    features = _build_sequence_array(window.head(seq_len), ref_close)
    return features


def _build_sequence_array(window: pd.DataFrame, ref_close: float) -> np.ndarray:
    """构建归一化序列特征矩阵。

    每行（每个时间步）包含以下特征：
    - norm_open, norm_high, norm_low, norm_close (除以 ref_close)
    - log_return (对数收益率)
    - range_pct (日内振幅 %)
    - body_pct (实体占振幅比，带符号)
    - volume_ratio (成交量 / 20日均量)
    """
    n = len(window)
    feats = np.zeros((n, 8), dtype=np.float32)

    close = window["close"].values.astype(np.float64)
    open_ = window["open"].values.astype(np.float64) if "open" in window.columns else close
    high = window["high"].values.astype(np.float64) if "high" in window.columns else close
    low = window["low"].values.astype(np.float64) if "low" in window.columns else close
    volume = window["volume"].values.astype(np.float64) if "volume" in window.columns else np.ones(n)

    # 归一化 OHLC
    feats[:, 0] = open_ / ref_close
    feats[:, 1] = high / ref_close
    feats[:, 2] = low / ref_close
    feats[:, 3] = close / ref_close

    # 对数收益率
    log_ret = np.zeros(n, dtype=np.float32)
    log_ret[1:] = np.log(np.maximum(close[1:], 1e-8) / np.maximum(close[:-1], 1e-8))
    feats[:, 4] = log_ret

    # 日内振幅
    h_l_range = high - low
    feats[:, 5] = np.where(ref_close > 0, h_l_range / ref_close * 100, 0)

    # 实体占比（带符号：阳线正、阴线负）
    body = close - open_
    feats[:, 6] = np.where(
        h_l_range > 1e-8,
        body / h_l_range,
        0,
    )

    # 成交量比（简易版本：除以事件日成交量）
    vol_ref = volume[-1] if volume[-1] > 0 else 1.0
    feats[:, 7] = np.where(vol_ref > 0, volume / vol_ref, 1.0)

    return feats.astype(np.float32)


# ─────────────────────────────────────────────
#  Feature extraction for Approach A (aggregated)
# ─────────────────────────────────────────────


def extract_aggregated_features(
    bars: pd.DataFrame,
    event_date: date,
    *,
    seq_len: int = 60,
    config: LabelingConfig | None = None,
) -> dict[str, float] | None:
    """从事件前 K 线提取聚合统计特征（用于 LightGBM 表格模型）。

    对 60 天的序列计算均值、标准差、偏度、峰度、斜率等统计量，
    生成 ~50 维的平坦特征向量。

    Parameters
    ----------
    bars : DataFrame
        单票日线（需排序，包含 OHLCV + 可选的 enriched columns）。
    event_date : date
        事件日期 T。
    seq_len : int
        序列长度。
    config : LabelingConfig, optional

    Returns
    -------
    dict or None
        特征名 → 特征值的映射，若数据不足返回 None。
    """
    _ = config or DEFAULT_CONFIG
    if bars.empty:
        return None

    df = _ensure_trade_date_column(bars.sort_values("trade_date"))
    df["trade_date_dt"] = pd.to_datetime(df["trade_date"]).dt.date
    before = df[df["trade_date_dt"] <= event_date]

    if len(before) < seq_len:
        return None

    window = before.tail(seq_len)
    close = window["close"].values.astype(np.float64)

    if len(close) < 10 or close[-1] <= 0:
        return None

    ref_close = close[-1]

    # 日收益率
    daily_ret = np.diff(close) / np.where(close[:-1] > 0, close[:-1], 1) * 100
    log_ret = np.log(np.maximum(close[1:], 1e-8) / np.maximum(close[:-1], 1e-8))
    cum_ret = (close[-1] / np.where(close[0] > 0, close[0], 1) - 1) * 100

    # 成交量
    vol = window["volume"].values.astype(np.float64) if "volume" in window.columns else np.ones(len(window))
    vol_ma20 = pd.Series(vol).rolling(20, min_periods=5).mean().values
    vol_ratio = np.where(vol_ma20 > 0, vol / vol_ma20, 1.0)

    # 日内振幅
    if "high" in window.columns and "low" in window.columns:
        daily_range = (window["high"] - window["low"]).values / np.where(close > 0, close, 1) * 100
    else:
        daily_range = np.abs(daily_ret)

    # 实体占比
    if "open" in window.columns:
        body = (close - window["open"].values) / np.where(
            (window["high"] - window["low"]).values > 0,
            (window["high"] - window["low"]).values,
            1,
        )
    else:
        body = np.zeros(len(window))

    features: dict[str, float] = {}

    # ── 收益率统计 ──
    features.update(_stat_dict(daily_ret, "ret_daily"))
    features.update(_stat_dict(log_ret, "ret_log"))
    features["ret_cum_60d"] = round(cum_ret, 4)

    # 分阶段收益
    third = len(close) // 3
    if third > 0:
        features["ret_early"] = round(
            (close[third] / np.where(close[0] > 0, close[0], 1) - 1) * 100, 4
        )
        if 2 * third < len(close):
            features["ret_mid"] = round(
                (close[2 * third] / np.where(close[third] > 0, close[third], 1) - 1) * 100, 4
            )
            features["ret_late"] = round(
                (close[-1] / np.where(close[2 * third] > 0, close[2 * third], 1) - 1) * 100, 4
            )

    # ── 价格位置特征 ──
    features["price_vs_60d_high"] = round(close[-1] / np.max(close) * 100, 4)
    features["price_vs_60d_low"] = round(close[-1] / np.min(close) * 100, 4)
    features["drawdown_60d"] = round((close[-1] / np.max(close) - 1) * 100, 4)
    features["runup_60d"] = round((close[-1] / np.min(close) - 1) * 100, 4)

    # ── 均线特征（若有 enriched bars）──
    for ma_name in ["ma5", "ma10", "ma20", "ma60"]:
        if ma_name in window.columns:
            ma_vals = window[ma_name].values
            last_ma = float(ma_vals[-1])
            if not np.isnan(last_ma) and last_ma > 0:
                features[f"close_vs_{ma_name}"] = round((close[-1] / last_ma - 1) * 100, 4)
                features[f"{ma_name}_slope_5d"] = round(
                    (ma_vals[-1] / np.where(ma_vals[-6] > 0, ma_vals[-6], 1) - 1) * 100, 4
                    if len(ma_vals) >= 6 else 0, 4,
                )

    # ── 波动率特征 ──
    features["volatility_20d"] = round(float(np.std(daily_ret[-20:])), 4) if len(daily_ret) >= 20 else 0.0
    features["volatility_60d"] = round(float(np.std(daily_ret)), 4)

    # ── 成交量特征 ──
    features.update(_stat_dict(vol_ratio, "vol_ratio"))
    features["vol_ratio_last"] = round(float(vol_ratio[-1]), 4) if len(vol_ratio) > 0 else 1.0
    features["vol_trend"] = round(_linear_slope(vol_ratio[-20:]), 4) if len(vol_ratio) >= 20 else 0.0

    # ── 振幅特征 ──
    features.update(_stat_dict(daily_range, "range"))
    features["range_last"] = round(float(daily_range[-1]), 4) if len(daily_range) > 0 else 0.0

    # ── 实体/影线特征 ──
    features.update(_stat_dict(body, "body"))
    features["body_last"] = round(float(body[-1]), 4) if len(body) > 0 else 0.0

    # ── 趋势强度特征 ──
    features["trend_strength"] = round(
        abs(cum_ret) / (features.get("volatility_60d", 1.0) + 1e-8), 4
    )
    features["price_monotonicity"] = round(
        np.sum(np.diff(close) > 0) / max(len(close) - 1, 1), 4
    )

    # ── 近端加速度 ──
    if len(close) >= 5:
        ret_last5 = (close[-1] / np.where(close[-6] > 0, close[-6], 1) - 1) * 100
        ret_prev5 = (close[-6] / np.where(close[-11] > 0, close[-11], 1) - 1) * 100 if len(close) >= 11 else 0
        features["acceleration_5d"] = round(ret_last5 - ret_prev5, 4)

    # ── 技术指标快照（若有 enriched bars）──
    for col in ["rsi14", "macd_dif", "macd_hist", "kdj_k", "kdj_j", "atr_pct"]:
        if col in window.columns:
            val = window[col].iloc[-1]
            if not pd.isna(val):
                features[col] = round(float(val), 4)

    # 剔除 NaN / Inf
    return {k: v for k, v in features.items() if np.isfinite(v)}


def _stat_dict(arr: np.ndarray, prefix: str) -> dict[str, float]:
    """计算数组的常用统计量，返回 {prefix}_{stat}: value}。"""
    clean = arr[np.isfinite(arr)]
    if len(clean) == 0:
        return {f"{prefix}_mean": 0.0, f"{prefix}_std": 0.0}

    result: dict[str, float] = {
        f"{prefix}_mean": round(float(np.mean(clean)), 6),
        f"{prefix}_std": round(float(np.std(clean)), 6),
        f"{prefix}_min": round(float(np.min(clean)), 6),
        f"{prefix}_max": round(float(np.max(clean)), 6),
    }

    if len(clean) >= 3:
        try:
            from scipy import stats as _stats
            result[f"{prefix}_skew"] = round(float(_stats.skew(clean)), 6)
            result[f"{prefix}_kurt"] = round(float(_stats.kurtosis(clean)), 6)
        except ImportError:
            # Fallback: simple moment-based approximation
            z = (clean - clean.mean()) / (clean.std() + 1e-8)
            result[f"{prefix}_skew"] = round(float(np.mean(z ** 3)), 6)
            result[f"{prefix}_kurt"] = round(float(np.mean(z ** 4) - 3), 6)
        except Exception:
            result[f"{prefix}_skew"] = 0.0
            result[f"{prefix}_kurt"] = 0.0

    return result


def _linear_slope(y: np.ndarray) -> float:
    """计算序列的线性趋势斜率。"""
    clean = y[np.isfinite(y)]
    if len(clean) < 2:
        return 0.0
    x = np.arange(len(clean))
    slope = np.polyfit(x, clean, 1)[0]
    return float(slope)


# ─────────────────────────────────────────────
#  Full dataset construction
# ─────────────────────────────────────────────


def build_event_dataset(
    labeled_events: pd.DataFrame,
    history: pd.DataFrame,
    *,
    approach: str = "A",
    seq_len: int = 60,
    exclude_ambiguous: bool = True,
    config: LabelingConfig | None = None,
) -> tuple[np.ndarray, np.ndarray, pd.Index] | tuple[np.ndarray, np.ndarray, np.ndarray, pd.Index]:
    """从 labeled events + history 构建完整训练矩阵。

    Parameters
    ----------
    labeled_events : DataFrame
        ``scan_all_stocks()`` 的输出。
    history : DataFrame
        全市场日线长表（需含 ``ts_code``, ``trade_date``, OHLCV）。
    approach : str
        ``"A"`` = 聚合特征 (LightGBM),
        ``"A_seq"`` = 序列特征 (LSTM),
        ``"B"`` = 聚合 + 上下文（需额外数据）。
    seq_len : int
        序列长度。
    exclude_ambiguous : bool
        是否排除 AMBIGUOUS 标签（默认 True）。
    config : LabelingConfig, optional

    Returns
    -------
    - approach="A" 或 "B"： (X, y, index)
        X: 特征矩阵 (n_samples, n_features)
        y: 标签编码 (n_samples,)
        index: 样本标识（ts_code + event_date + horizon）
    - approach="A_seq"： (X_seq, X_agg, y, index)
        X_seq: 序列特征 (n_samples, seq_len, 8)
        X_agg: 聚合特征 (n_samples, n_agg_features)
        y: 标签编码 (n_samples,)
        index: 样本标识
    """
    cfg = config or DEFAULT_CONFIG

    if labeled_events.empty or history.empty:
        logger.warning("输入数据为空")
        if approach == "A_seq":
            return (
                np.empty((0, seq_len, 8), dtype=np.float32),
                np.empty((0, 1), dtype=np.float32),
                np.array([], dtype=np.int64),
                pd.Index([]),
            )
        return (
            np.empty((0, 1), dtype=np.float32),
            np.array([], dtype=np.int64),
            pd.Index([]),
        )

    # 过滤标签
    events = labeled_events.copy()
    if exclude_ambiguous:
        events = events[events["label"] != PatternLabel.AMBIGUOUS].copy()
    if events.empty:
        logger.warning("排除 AMBIGUOUS 后无有效标签")
        if approach == "A_seq":
            return (
                np.empty((0, seq_len, 8), dtype=np.float32),
                np.empty((0, 1), dtype=np.float32),
                np.array([], dtype=np.int64),
                pd.Index([]),
            )
        return (
            np.empty((0, 1), dtype=np.float32),
            np.array([], dtype=np.int64),
            pd.Index([]),
        )

    # 标签编码
    label_map = {
        PatternLabel.REAL_RALLY: 0,
        PatternLabel.BULL_TRAP: 1,
        PatternLabel.REAL_BREAKDOWN: 2,
        PatternLabel.PANIC_SHAKEOUT: 3,
    }
    events["label_code"] = events["label"].map(label_map)
    events = events.dropna(subset=["label_code"])
    events["label_code"] = events["label_code"].astype(np.int64)

    # 按 ts_code 缓存 bars（避免 reset_index 时列名歧义）
    code_bars: dict[str, pd.DataFrame] = {}
    for code in events["ts_code"].unique():
        grp = history[history["ts_code"] == code]
        if not grp.empty:
            df = grp.sort_values("trade_date").copy()
            # trade_date 可能既是 index 又是 column，先处理
            if "trade_date" in df.index.names:
                df = df.reset_index()
            code_bars[code] = df

    # 构建特征
    if approach == "A_seq":
        return _build_seq_dataset(events, code_bars, seq_len, cfg)
    else:
        return _build_agg_dataset(events, code_bars, seq_len, cfg)


def _build_agg_dataset(
    events: pd.DataFrame,
    code_bars: dict[str, pd.DataFrame],
    seq_len: int,
    cfg: LabelingConfig,
) -> tuple[np.ndarray, np.ndarray, pd.Index]:
    """构建聚合特征数据集。"""
    feature_dicts: list[dict[str, float]] = []
    labels: list[int] = []
    indices: list[str] = []

    for _, row in events.iterrows():
        code = str(row["ts_code"])
        bars = code_bars.get(code)
        if bars is None:
            continue

        event_date = _to_date(row["event_date"])
        feats = extract_aggregated_features(
            bars, event_date, seq_len=seq_len, config=cfg
        )
        if feats is None:
            continue

        feature_dicts.append(feats)
        labels.append(int(row["label_code"]))
        indices.append(
            f"{code}_{row['event_date']}_{row['event_type']}_h{int(row['horizon_days'])}"
        )

    if not feature_dicts:
        return (
            np.empty((0, 1), dtype=np.float32),
            np.array([], dtype=np.int64),
            pd.Index([]),
        )

    X = pd.DataFrame(feature_dicts).fillna(0.0)
    y = np.array(labels, dtype=np.int64)
    idx = pd.Index(indices, name="sample")

    logger.info("构建数据集: X=%s, y=%s", X.shape, y.shape)
    return X.values.astype(np.float32), y, idx


def _ensure_trade_date_column(df: pd.DataFrame) -> pd.DataFrame:
    """确保 trade_date 是普通列而非 index（避免 reset_index 歧义）。"""
    if "trade_date" in df.index.names:
        return df.reset_index()
    return df.copy()


def _build_seq_dataset(
    events: pd.DataFrame,
    code_bars: dict[str, pd.DataFrame],
    seq_len: int,
    cfg: LabelingConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, pd.Index]:
    """构建序列特征数据集（用于 LSTM）。"""
    seq_list: list[np.ndarray] = []
    agg_dicts: list[dict[str, float]] = []
    labels: list[int] = []
    indices: list[str] = []

    for _, row in events.iterrows():
        code = str(row["ts_code"])
        bars = code_bars.get(code)
        if bars is None:
            continue

        event_date = _to_date(row["event_date"])

        seq = extract_sequence_features(
            bars, event_date, seq_len=seq_len, config=cfg
        )
        agg = extract_aggregated_features(
            bars, event_date, seq_len=seq_len, config=cfg
        )
        if seq is None or agg is None:
            continue

        seq_list.append(seq)
        agg_dicts.append(agg)
        labels.append(int(row["label_code"]))
        indices.append(
            f"{code}_{row['event_date']}_{row['event_type']}_h{int(row['horizon_days'])}"
        )

    if not seq_list:
        return (
            np.empty((0, seq_len, 8), dtype=np.float32),
            np.empty((0, 1), dtype=np.float32),
            np.array([], dtype=np.int64),
            pd.Index([]),
        )

    X_seq = np.stack(seq_list, axis=0).astype(np.float32)
    X_agg = pd.DataFrame(agg_dicts).fillna(0.0).values.astype(np.float32)
    y = np.array(labels, dtype=np.int64)
    idx = pd.Index(indices, name="sample")

    logger.info("构建序列数据集: X_seq=%s, X_agg=%s, y=%s", X_seq.shape, X_agg.shape, y.shape)
    return X_seq, X_agg, y, idx


def _to_date(val: Any) -> date:
    """安全转换为 date。"""
    if isinstance(val, date):
        return val
    if isinstance(val, pd.Timestamp):
        return val.date()
    return date.fromordinal(1)
