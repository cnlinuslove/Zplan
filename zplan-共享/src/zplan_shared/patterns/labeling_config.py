"""标签生成阈值配置 — 所有阈值均可通过环境变量覆盖。

设计原则：
- 标签来自未来价格走势（forward return），非主观判断
- 多时间窗口（5/10/20 天）捕获不同时间尺度的诱多/诱空
- 中间地带排除（不训练），减少标签噪音
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Sequence


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name, "").strip()
    if val:
        return float(val)
    return default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    if val:
        return int(val)
    return default


@dataclass
class LabelingConfig:
    """标签生成配置。

    可通过环境变量覆盖关键阈值（如 ``PATTERN_RUNUP_THRESHOLD=0.10``）。
    """

    # ── 局部极值检测 ──────────────────────────────────────────
    runup_threshold: float = _env_float("PATTERN_RUNUP_THRESHOLD", 0.08)
    """上涨幅度阈值：从 T-20 到 T 的涨幅 >= 该值才算「峰」"""

    drawdown_threshold: float = _env_float("PATTERN_DRAWDOWN_THRESHOLD", -0.08)
    """下跌幅度阈值：从 T-20 到 T 的跌幅 <= 该值才算「谷」"""

    peak_lookback: int = _env_int("PATTERN_PEAK_LOOKBACK", 20)
    """极值检测回溯窗口（交易日）"""

    peak_local_window: int = _env_int("PATTERN_PEAK_LOCAL_WINDOW", 3)
    """局部极值检测半窗口：close[T] 是 [T-3, T+3] 内的最大/最小值"""

    min_bars_before: int = _env_int("PATTERN_MIN_BARS_BEFORE", 60)
    """事件前最少 K 线数（确保有足够的形成期）"""

    min_bars_after: int = _env_int("PATTERN_MIN_BARS_AFTER", 20)
    """事件后最少 K 线数（确保能计算 forward return）"""

    # ── 标签分配阈值（多时间窗口）──────────────────────────────
    horizons: Sequence[int] = field(default_factory=lambda: [5, 10, 20])
    """多时间窗口（交易日）"""

    # 上涨后（峰）标签
    upward_real_rally: float = _env_float("PATTERN_UPWARD_REAL_RALLY", 0.05)
    """峰后 forward_return >= +5% → 真的拉升"""

    upward_bull_trap: float = _env_float("PATTERN_UPWARD_BULL_TRAP", -0.05)
    """峰后 forward_return <= -5% → 诱多"""

    # 下跌后（谷）标签
    downward_real_breakdown: float = _env_float("PATTERN_DOWNWARD_REAL_BREAKDOWN", -0.05)
    """谷后 forward_return <= -5% → 真的崩盘"""

    downward_panic_shakeout: float = _env_float("PATTERN_DOWNWARD_PANIC_SHAKEOUT", 0.05)
    """谷后 forward_return >= +5% → 恐慌洗盘"""

    # 各 horizon 的阈值缩放因子（短窗口用更宽松的阈值）
    horizon_threshold_scale: dict[int, float] = field(default_factory=lambda: {
        5: 0.6,   # 5 天 → 阈值 × 0.6
        10: 1.0,  # 10 天 → 原始阈值
        20: 1.5,  # 20 天 → 阈值 × 1.5
    })

    # ── 质量控制 ──────────────────────────────────────────────
    min_formation_movement_pct: float = _env_float("PATTERN_MIN_FORMATION_MOVEMENT", 0.03)
    """形成期最小涨跌幅：3% 以下的波动不算有效事件"""

    min_atr_pct: float = _env_float("PATTERN_MIN_ATR_PCT", 0.5)
    """最低 ATR%：排除低波动无量震荡"""

    max_events_per_stock_per_year: int = _env_int("PATTERN_MAX_EVENTS_PER_STOCK", 24)
    """每只股票每年最多事件数（去重后）"""

    event_dedup_window: int = _env_int("PATTERN_EVENT_DEDUP_WINDOW", 10)
    """同类型事件去重窗口（交易日）：10 天内两个峰只保留第一个"""

    # ── 标签置信度层 ──────────────────────────────────────────
    high_confidence_margin: float = _env_float("PATTERN_HIGH_CONFIDENCE_MARGIN", 0.02)
    """forward return 偏离阈值 >= 2% → 高置信度标签"""


# 全局默认配置
DEFAULT_CONFIG = LabelingConfig()


# 标签枚举
class EventType:
    PEAK = "peak"
    TROUGH = "trough"


class PatternLabel:
    REAL_RALLY = "REAL_RALLY"            # 真的拉升
    BULL_TRAP = "BULL_TRAP"              # 诱多
    REAL_BREAKDOWN = "REAL_BREAKDOWN"    # 真的崩盘
    PANIC_SHAKEOUT = "PANIC_SHAKEOUT"    # 恐慌洗盘
    AMBIGUOUS = "AMBIGUOUS"              # 中间地带（不训练）

    @classmethod
    def all_labels(cls) -> list[str]:
        return [cls.REAL_RALLY, cls.BULL_TRAP, cls.REAL_BREAKDOWN, cls.PANIC_SHAKEOUT]

    @classmethod
    def upward_labels(cls) -> list[str]:
        return [cls.REAL_RALLY, cls.BULL_TRAP]

    @classmethod
    def downward_labels(cls) -> list[str]:
        return [cls.REAL_BREAKDOWN, cls.PANIC_SHAKEOUT]

    @classmethod
    def is_trap(cls, label: str) -> bool:
        return label in (cls.BULL_TRAP, cls.PANIC_SHAKEOUT)
