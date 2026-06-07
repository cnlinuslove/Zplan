"""标签生成核心逻辑 — 检测局部极值点并基于未来走势分配标签。

这是整个模式学习系统最关键的文件：标签质量直接决定模型能学到什么。

工作流程：
1. 从 daily_prices 加载全市场历史数据
2. 逐票检测局部极值点（峰/谷）
3. 计算多时间窗口的 forward return
4. 根据 forward return 分配标签（真的拉升/诱多/真的崩盘/恐慌洗盘）
5. 质检过滤（低波动、去重、样本均衡）
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from zplan_shared.patterns.labeling_config import (
    DEFAULT_CONFIG,
    EventType,
    LabelingConfig,
    PatternLabel,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  局部极值检测
# ─────────────────────────────────────────────


def detect_events(
    bars: pd.DataFrame,
    *,
    config: LabelingConfig | None = None,
) -> pd.DataFrame:
    """在单票日线 OHLCV 上检测局部极值点。

    Parameters
    ----------
    bars : DataFrame
        需包含 ``trade_date``, ``close``, ``volume`` 列；索引可以是任意值。
    config : LabelingConfig, optional

    Returns
    -------
    DataFrame
        列：``event_date``, ``event_type`` ("peak"/"trough"),
        ``formation_start``, ``runup_pct``, ``atr_pct``, ``confidence_score``,
        ``close_at_event``, ``volume_at_event``
    """
    cfg = config or DEFAULT_CONFIG
    if bars.empty or "close" not in bars.columns:
        return pd.DataFrame()

    df = bars.sort_values("trade_date").reset_index(drop=True).copy()
    n = len(df)

    if n < cfg.min_bars_before + cfg.min_bars_after:
        return pd.DataFrame()

    close = df["close"].values
    trade_dates = df["trade_date"].values

    # 计算 ATR% 用于波动率过滤
    if "high" in df.columns and "low" in df.columns:
        tr = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                np.abs(df["high"] - df["close"].shift(1)),
                np.abs(df["low"] - df["close"].shift(1)),
            ),
        )
        atr14 = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().values
        atr_pct = np.where(close > 0, atr14 / close * 100, 0)
    else:
        # 缺少 OHLC 全字段时回退：用日收益率的波动率近似
        ret = np.abs(np.diff(close) / np.where(close[:-1] > 0, close[:-1], 1) * 100)
        atr_pct = np.zeros(n)
        # 填充：14 天滚动均值
        for i in range(14, n):
            atr_pct[i] = np.mean(ret[max(0, i - 14) : i])

    events: list[dict[str, Any]] = []

    w = cfg.peak_local_window

    for i in range(cfg.peak_lookback + w, n - cfg.min_bars_after - w):
        local_slice = close[i - w : i + w + 1]
        is_peak = close[i] == local_slice.max()
        is_trough = close[i] == local_slice.min()

        if not is_peak and not is_trough:
            continue

        # 形成期涨跌幅（从 T-peak_lookback 到 T）
        lookback_start = max(0, i - cfg.peak_lookback)
        if lookback_start >= i:
            continue
        prev_close = close[lookback_start]
        if prev_close <= 0 or close[i] <= 0:
            continue
        runup = (close[i] / prev_close - 1) * 100

        # 阈值检查
        if is_peak and runup < cfg.runup_threshold * 100:
            continue
        if is_trough and runup > cfg.drawdown_threshold * 100:
            continue

        # 波动率过滤
        event_atr = atr_pct[i]
        if event_atr < cfg.min_atr_pct:
            continue

        # 形成期涨跌幅绝对值不够大
        if abs(runup) < cfg.min_formation_movement_pct * 100:
            continue

        event_type = EventType.PEAK if is_peak else EventType.TROUGH

        events.append({
            "event_date": _to_date(trade_dates[i]),
            "event_type": event_type,
            "formation_start": _to_date(trade_dates[lookback_start]),
            "runup_pct": round(runup, 2),
            "atr_pct": round(float(event_atr), 2),
            "close_at_event": float(close[i]),
            "volume_at_event": float(df["volume"].iloc[i]) if "volume" in df.columns else None,
            "event_idx": i,
        })

    events_df = pd.DataFrame(events)
    if events_df.empty:
        return events_df

    # 去重：同类型事件在 dedup_window 内只保留第一个
    events_df = _dedup_events(events_df, cfg.event_dedup_window)

    return events_df.sort_values("event_date").reset_index(drop=True)


def _to_date(val: Any) -> date:
    """安全转换为 date 类型。"""
    if isinstance(val, date):
        return val
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, str):
        return pd.Timestamp(val).date()
    return date.fromordinal(1)


def _dedup_events(events_df: pd.DataFrame, window: int) -> pd.DataFrame:
    """同类型事件去重：window 个交易日内只保留第一个。"""
    if events_df.empty or "event_date" not in events_df.columns:
        return events_df

    df = events_df.sort_values("event_date").reset_index(drop=True)
    keep: list[int] = []
    last_date: dict[str, date] = {}

    for idx, row in df.iterrows():
        event_date = _to_date(row["event_date"])
        etype = row["event_type"]
        if etype in last_date:
            delta = (event_date - last_date[etype]).days
            if delta <= window:
                continue
        keep.append(idx)
        last_date[etype] = event_date

    return df.iloc[keep].reset_index(drop=True)


# ─────────────────────────────────────────────
#  前向标签分配
# ─────────────────────────────────────────────


def assign_labels(
    bars: pd.DataFrame,
    events: pd.DataFrame,
    *,
    config: LabelingConfig | None = None,
) -> pd.DataFrame:
    """为检测到的事件分配标签（基于 forward return）。

    Parameters
    ----------
    bars : DataFrame
        单票日线（需含 ``trade_date``, ``close``）。
    events : DataFrame
        ``detect_events()`` 的输出。
    config : LabelingConfig, optional

    Returns
    -------
    DataFrame
        每个 (event, horizon) 一行，增加 ``horizon_days``, ``forward_return``,
        ``label``, ``label_confidence`` 列。
    """
    cfg = config or DEFAULT_CONFIG
    if events.empty or bars.empty:
        return pd.DataFrame()

    bars_sorted = bars.sort_values("trade_date").reset_index(drop=True)
    close = bars_sorted["close"].values
    trade_dates = bars_sorted["trade_date"].values
    n = len(bars_sorted)

    labels: list[dict[str, Any]] = []

    for _, evt in events.iterrows():
        event_idx = evt.get("event_idx")
        if event_idx is None or pd.isna(event_idx):
            # 通过 event_date 查找索引
            event_date = _to_date(evt["event_date"])
            matches = [
                j for j, d in enumerate(trade_dates) if _to_date(d) == event_date
            ]
            if not matches:
                continue
            event_idx = matches[0]

        event_idx = int(event_idx)
        event_type = str(evt["event_type"])
        event_close = close[event_idx]

        for horizon in cfg.horizons:
            forward_idx = event_idx + horizon
            if forward_idx >= n:
                continue

            forward_close = close[forward_idx]
            if event_close <= 0:
                continue
            fwd_return = (forward_close / event_close - 1) * 100

            # 按时间窗口缩放阈值
            scale = cfg.horizon_threshold_scale.get(horizon, 1.0)

            label = _classify_return(
                event_type=event_type,
                fwd_return=fwd_return,
                scale=scale,
                cfg=cfg,
            )

            confidence = _label_confidence(
                event_type=event_type,
                fwd_return=fwd_return,
                label=label,
                scale=scale,
                cfg=cfg,
            )

            labels.append({
                **{k: v for k, v in evt.items() if k != "event_idx"},
                "horizon_days": horizon,
                "forward_return": round(fwd_return, 2),
                "label": label,
                "label_confidence": round(confidence, 2),
                "horizon_end_date": _to_date(trade_dates[forward_idx]),
            })

    return pd.DataFrame(labels)


def _classify_return(
    event_type: str,
    fwd_return: float,
    scale: float,
    cfg: LabelingConfig,
) -> str:
    """根据 event_type 和 forward return 分配标签。"""
    if event_type == EventType.PEAK:
        rally_threshold = cfg.upward_real_rally * 100 * scale
        trap_threshold = cfg.upward_bull_trap * 100 * scale

        if fwd_return >= rally_threshold:
            return PatternLabel.REAL_RALLY
        elif fwd_return <= trap_threshold:
            return PatternLabel.BULL_TRAP
        else:
            return PatternLabel.AMBIGUOUS

    elif event_type == EventType.TROUGH:
        breakdown_threshold = cfg.downward_real_breakdown * 100 * scale
        recovery_threshold = cfg.downward_panic_shakeout * 100 * scale

        if fwd_return <= breakdown_threshold:
            return PatternLabel.REAL_BREAKDOWN
        elif fwd_return >= recovery_threshold:
            return PatternLabel.PANIC_SHAKEOUT
        else:
            return PatternLabel.AMBIGUOUS

    return PatternLabel.AMBIGUOUS


def _label_confidence(
    event_type: str,
    fwd_return: float,
    label: str,
    scale: float,
    cfg: LabelingConfig,
) -> float:
    """标签置信度：forward return 偏离阈值越远，置信度越高。

    Returns 0.0-1.0，其中 1.0 表示高置信度。
    """
    if label == PatternLabel.AMBIGUOUS:
        return 0.0

    margin = cfg.high_confidence_margin * 100  # 转换为百分点

    if event_type == EventType.PEAK:
        rally_threshold = cfg.upward_real_rally * 100 * scale
        trap_threshold = cfg.upward_bull_trap * 100 * scale

        if label == PatternLabel.REAL_RALLY:
            excess = fwd_return - rally_threshold
            return min(1.0, excess / margin) if margin > 0 else 0.5
        else:
            excess = trap_threshold - fwd_return
            return min(1.0, excess / margin) if margin > 0 else 0.5

    elif event_type == EventType.TROUGH:
        breakdown_threshold = cfg.downward_real_breakdown * 100 * scale
        recovery_threshold = cfg.downward_panic_shakeout * 100 * scale

        if label == PatternLabel.REAL_BREAKDOWN:
            excess = breakdown_threshold - fwd_return
            return min(1.0, excess / margin) if margin > 0 else 0.5
        else:
            excess = fwd_return - recovery_threshold
            return min(1.0, excess / margin) if margin > 0 else 0.5

    return 0.0


# ─────────────────────────────────────────────
#  全市场扫描
# ─────────────────────────────────────────────


def scan_all_stocks(
    history: pd.DataFrame,
    *,
    config: LabelingConfig | None = None,
    max_stocks: int | None = None,
) -> pd.DataFrame:
    """从全市场日线长表生成标签。

    Parameters
    ----------
    history : DataFrame
        长表格式（``ts_code``, ``trade_date``, OHLCV），
        来自 ``market.get_history_window()``。
    config : LabelingConfig, optional
    max_stocks : int, optional
        限制处理股票数量（用于快速验证）。

    Returns
    -------
    DataFrame
        所有股票的 labeled events，每行一个 (stock, event, horizon)。
    """
    cfg = config or DEFAULT_CONFIG
    if history.empty or "ts_code" not in history.columns:
        logger.warning("history 为空或缺少 ts_code 列")
        return pd.DataFrame()

    codes = sorted(history["ts_code"].unique())
    if max_stocks:
        codes = codes[:max_stocks]

    all_labels: list[pd.DataFrame] = []
    stats = {"total": len(codes), "with_events": 0, "labeled": 0, "skipped": 0}

    for i, code in enumerate(codes):
        grp = history[history["ts_code"] == code]
        bars = grp.sort_values("trade_date").set_index("trade_date")
        bars = bars.reset_index()  # trade_date → column, 避免列名歧义

        events = detect_events(bars, config=cfg)
        if events.empty:
            stats["skipped"] += 1
            continue

        stats["with_events"] += 1

        # 每年最多 N 个事件
        events = _limit_events_per_year(events, cfg.max_events_per_stock_per_year)

        labeled = assign_labels(bars.reset_index(drop=True), events, config=cfg)
        if not labeled.empty:
            labeled["ts_code"] = code
            all_labels.append(labeled)
            stats["labeled"] += 1

        if (i + 1) % 500 == 0:
            logger.info(
                "扫描进度: %d/%d 只股票, 已标注: %d",
                i + 1,
                stats["total"],
                stats["labeled"],
            )

    logger.info(
        "扫描完成: %d 只股票, %d 只有事件, %d 只有标签, %d 跳过",
        stats["total"],
        stats["with_events"],
        stats["labeled"],
        stats["skipped"],
    )

    if not all_labels:
        return pd.DataFrame()

    result = pd.concat(all_labels, ignore_index=True)
    logger.info("总标签数: %d (去重后)", len(result))

    return result


def _limit_events_per_year(events: pd.DataFrame, max_per_year: int) -> pd.DataFrame:
    """每年每种事件类型最多保留 max_per_year 个（按 runup 绝对值排序，取极端）。"""
    if events.empty or "event_date" not in events.columns:
        return events

    df = events.copy()
    df["year"] = pd.to_datetime(df["event_date"]).dt.year

    kept: list[int] = []
    for (year, etype), grp in df.groupby(["year", "event_type"], sort=False):
        sorted_grp = grp.sort_values("runup_pct", key=abs, ascending=False)
        kept.extend(sorted_grp.head(max_per_year).index.tolist())

    return df.loc[kept].drop(columns=["year"]).reset_index(drop=True)


# ─────────────────────────────────────────────
#  标签摘要统计
# ─────────────────────────────────────────────


def label_summary(labeled: pd.DataFrame) -> dict[str, Any]:
    """标签分布摘要（训练前必看）。"""
    if labeled.empty or "label" not in labeled.columns:
        return {"total": 0, "distribution": {}, "notes": "no labels"}

    dist = labeled["label"].value_counts().to_dict()
    total = int(labeled["label"].value_counts().sum())
    non_ambiguous = sum(v for k, v in dist.items() if k != PatternLabel.AMBIGUOUS)

    horizons = (
        labeled["horizon_days"].unique().tolist()
        if "horizon_days" in labeled.columns
        else []
    )

    by_horizon = {}
    if "horizon_days" in labeled.columns and "label" in labeled.columns:
        for h in horizons:
            h_df = labeled[labeled["horizon_days"] == h]
            by_horizon[str(h)] = h_df["label"].value_counts().to_dict()

    confidence_stats = {}
    if "label_confidence" in labeled.columns:
        high_conf = labeled[labeled["label_confidence"] >= 0.7]
        confidence_stats = {
            "mean": round(float(labeled["label_confidence"].mean()), 3),
            "median": round(float(labeled["label_confidence"].median()), 3),
            "high_conf_pct": round(len(high_conf) / max(len(labeled), 1) * 100, 1),
        }

    # 各类别的平均 forward return
    fwd_by_label = {}
    if "forward_return" in labeled.columns and "label" in labeled.columns:
        for lbl in PatternLabel.all_labels():
            lbl_df = labeled[labeled["label"] == lbl]
            if not lbl_df.empty:
                fwd_by_label[lbl] = {
                    "count": len(lbl_df),
                    "mean_fwd_return": round(float(lbl_df["forward_return"].mean()), 2),
                    "std_fwd_return": round(float(lbl_df["forward_return"].std()), 2),
                }

    return {
        "total": total,
        "labeled_non_ambiguous": non_ambiguous,
        "distribution": {k: int(v) for k, v in dist.items()},
        "by_horizon": by_horizon,
        "forward_by_label": fwd_by_label,
        "confidence": confidence_stats,
        "class_balance": {
            "upward": {
                "total": sum(
                    v
                    for k, v in dist.items()
                    if k in (PatternLabel.REAL_RALLY, PatternLabel.BULL_TRAP)
                ),
                "real_rally": int(dist.get(PatternLabel.REAL_RALLY, 0)),
                "bull_trap": int(dist.get(PatternLabel.BULL_TRAP, 0)),
                "trap_ratio": round(
                    int(dist.get(PatternLabel.BULL_TRAP, 0))
                    / max(
                        int(dist.get(PatternLabel.REAL_RALLY, 0))
                        + int(dist.get(PatternLabel.BULL_TRAP, 0)),
                        1,
                    )
                    * 100,
                    1,
                ),
            },
            "downward": {
                "total": sum(
                    v
                    for k, v in dist.items()
                    if k in (PatternLabel.REAL_BREAKDOWN, PatternLabel.PANIC_SHAKEOUT)
                ),
                "real_breakdown": int(dist.get(PatternLabel.REAL_BREAKDOWN, 0)),
                "panic_shakeout": int(dist.get(PatternLabel.PANIC_SHAKEOUT, 0)),
                "shakeout_ratio": round(
                    int(dist.get(PatternLabel.PANIC_SHAKEOUT, 0))
                    / max(
                        int(dist.get(PatternLabel.REAL_BREAKDOWN, 0))
                        + int(dist.get(PatternLabel.PANIC_SHAKEOUT, 0)),
                        1,
                    )
                    * 100,
                    1,
                ),
            },
        },
    }
