"""出场策略引擎：多类型退出规则 + 交易计划。

提供：
  ExitType     — 退出规则类型枚举
  ExitRule     — 单条退出规则（类型 + 参数 + 激活条件 + 优先级）
  ExitPlan     — 完整出场方案（规则列表 + 元信息）
  ExitSignal   — 规则触发后的信号

核心纯函数：
  compute_exit_signals(position_state, exit_plan, bars, trade_date) → list[ExitSignal]

用法::

    from zplan_shared.exit_strategy import (
        ExitType, ExitRule, ExitPlan, ExitSignal, compute_exit_signals,
    )

    plan = ExitPlan(
        plan_key="atr_trail_2x",
        rules=[
            ExitRule(rule_type=ExitType.ATR_TRAIL, priority=0, params={"atr_multiplier": 2.0}),
            ExitRule(rule_type=ExitType.TIME_EXIT, priority=10, params={"max_holding_days": 20}),
        ],
    )

    signals = compute_exit_signals(
        position_state={"entry_price": 34.10, "entry_date": date(2026,6,9), "shares": 500},
        exit_plan=plan,
        bars=df,  # 含 OHLCV + atr14/ma20 等指标列
        trade_date=date(2026, 6, 10),
    )
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any

import pandas as pd


# ── 枚举 / 常量 ────────────────────────────────────────────────────


class ExitType(str, Enum):
    """出场规则类型。"""

    STATIC_STOP = "static_stop"               # 固定价格/百分比止损
    STATIC_TAKE_PROFIT = "static_take_profit"  # 固定价格/百分比止盈
    TRAILING_STOP = "trailing_stop"            # 从最高收盘价回撤 N% 触发
    ATR_TRAIL = "atr_trail"                   # 最高收盘价 - N×ATR
    MA_STOP = "ma_stop"                       # 收盘价跌破 MA(N)
    TIME_EXIT = "time_exit"                   # 持仓天数到期
    PARTIAL_TAKE_PROFIT = "partial_take_profit"  # 多级目标价分批止盈


# 默认优先级（数字越小越优先触发）
DEFAULT_PRIORITY: dict[ExitType, int] = {
    ExitType.STATIC_STOP: 0,
    ExitType.ATR_TRAIL: 1,
    ExitType.TRAILING_STOP: 1,
    ExitType.MA_STOP: 2,
    ExitType.STATIC_TAKE_PROFIT: 3,
    ExitType.PARTIAL_TAKE_PROFIT: 4,
    ExitType.TIME_EXIT: 10,
}

# 默认参数
DEFAULT_MAX_HOLDING_DAYS = 5
DEFAULT_STOP_LOSS_PCT = -0.05
DEFAULT_TAKE_PROFIT_PCT = 0.15


# ── 数据类 ──────────────────────────────────────────────────────────


@dataclass
class ExitRule:
    """单条出场规则。

    Attributes:
        rule_type: 规则类型。
        priority: 优先级（数字越小越先触发）。默认从 ``DEFAULT_PRIORITY`` 取。
        params: 类型相关参数（atr_multiplier / trail_pct / ma_period / levels / ...）。
        activate_after_min_return: 仅当浮盈超过此比例后才激活（None = 入场即激活）。
    """

    rule_type: ExitType
    priority: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    activate_after_min_return: float | None = None  # e.g. 0.03 = +3%

    def __post_init__(self):
        if self.priority is None:
            self.priority = DEFAULT_PRIORITY.get(self.rule_type, 5)

    @property
    def label(self) -> str:
        """人类可读的规则描述（用于报告）。"""
        p = self.params
        if self.rule_type == ExitType.STATIC_STOP:
            if "stop_pct" in p:
                return f"固定止损 {p['stop_pct']*100:+.1f}%"
            return f"固定止损 ¥{p.get('stop_price', '?')}"
        if self.rule_type == ExitType.STATIC_TAKE_PROFIT:
            if "target_pct" in p:
                return f"固定止盈 {p['target_pct']*100:+.1f}%"
            return f"固定止盈 ¥{p.get('target_price', '?')}"
        if self.rule_type == ExitType.TRAILING_STOP:
            return f"移动止盈 {p.get('trail_pct', 0.08)*100:.0f}%"
        if self.rule_type == ExitType.ATR_TRAIL:
            return f"ATR 追踪 {p.get('atr_multiplier', 2.0)}×"
        if self.rule_type == ExitType.MA_STOP:
            return f"MA{p.get('ma_period', 20)} 止损"
        if self.rule_type == ExitType.TIME_EXIT:
            return f"{p.get('max_holding_days', DEFAULT_MAX_HOLDING_DAYS)}天到期"
        if self.rule_type == ExitType.PARTIAL_TAKE_PROFIT:
            levels = p.get("levels", [])
            return f"分批止盈 ({len(levels)}级)"
        return self.rule_type.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_type": self.rule_type.value,
            "priority": self.priority,
            "params": self.params,
            "activate_after_min_return": self.activate_after_min_return,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExitRule":
        return cls(
            rule_type=ExitType(d["rule_type"]),
            priority=d.get("priority"),
            params=d.get("params", {}),
            activate_after_min_return=d.get("activate_after_min_return"),
        )


@dataclass
class ExitPlan:
    """完整出场方案：一组 ExitRule + 元信息。

    Attributes:
        plan_key: 方案标识（"static", "trailing_8pct", "atr_trail_2x", ...）。
        display_name: 人类可读名称。
        rules: 规则列表（按 priority 排序后应用）。
        description: 方案说明（可选，用于报告）。
    """

    plan_key: str
    display_name: str = ""
    rules: list[ExitRule] = field(default_factory=list)
    description: str = ""

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.plan_key
        # 按优先级排序
        self.rules.sort(key=lambda r: r.priority or 5)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_key": self.plan_key,
            "display_name": self.display_name,
            "rules": [r.to_dict() for r in self.rules],
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExitPlan":
        return cls(
            plan_key=d["plan_key"],
            display_name=d.get("display_name", ""),
            rules=[ExitRule.from_dict(r) for r in d.get("rules", [])],
            description=d.get("description", ""),
        )

    @classmethod
    def static_default(
        cls,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        take_profit_pct: float | None = DEFAULT_TAKE_PROFIT_PCT,
        max_holding_days: int = DEFAULT_MAX_HOLDING_DAYS,
    ) -> "ExitPlan":
        """构建向后兼容的「静态止损止盈」默认方案。"""
        rules: list[ExitRule] = [
            ExitRule(
                rule_type=ExitType.STATIC_STOP,
                priority=0,
                params={"stop_pct": stop_loss_pct},
            ),
            ExitRule(
                rule_type=ExitType.TIME_EXIT,
                priority=10,
                params={"max_holding_days": max_holding_days},
            ),
        ]
        if take_profit_pct is not None:
            rules.insert(1, ExitRule(
                rule_type=ExitType.STATIC_TAKE_PROFIT,
                priority=3,
                params={"target_pct": take_profit_pct},
            ))
        return cls(plan_key="static", display_name="静态止损止盈", rules=rules)

    @classmethod
    def from_legacy(
        cls,
        stop_loss: float | None,
        take_profit: float | None,
        holding_days_max: int,
        *,
        entry_price: float = 0.0,
    ) -> "ExitPlan":
        """从旧三参数（价格而非百分比）构建 ExitPlan。"""
        rules: list[ExitRule] = []
        if stop_loss is not None and entry_price > 0:
            stop_pct = round((stop_loss - entry_price) / entry_price, 4)
            rules.append(ExitRule(
                rule_type=ExitType.STATIC_STOP,
                priority=0,
                params={"stop_pct": stop_pct, "stop_price": stop_loss},
            ))
        if take_profit is not None and entry_price > 0:
            target_pct = round((take_profit - entry_price) / entry_price, 4)
            rules.append(ExitRule(
                rule_type=ExitType.STATIC_TAKE_PROFIT,
                priority=3,
                params={"target_pct": target_pct, "target_price": take_profit},
            ))
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": holding_days_max},
        ))
        return cls(plan_key="legacy", display_name="旧版静态方案", rules=rules)


@dataclass
class ExitSignal:
    """出场信号：某条规则在某个交易日触发。

    Attributes:
        rule_type: 触发规则类型。
        exit_price: 建议出场价。
        reason: 人类可读原因。
        priority: 规则优先级。
        sell_ratio: 卖出比例（1.0 = 全部卖出；<1.0 = 分批止盈）。
        rule_label: 规则标签（用于报告）。
    """

    rule_type: ExitType
    exit_price: float
    reason: str = ""
    priority: int = 0
    sell_ratio: float = 1.0
    rule_label: str = ""

    def __post_init__(self):
        if not self.rule_label:
            self.rule_label = self.rule_type.value


# ── 持仓状态（引擎输入）────────────────────────────────────────────


@dataclass
class PositionState:
    """出场引擎需要的持仓快照。

    与 sim_trade.Position 解耦，纯数据类，任何持仓系统可传入。
    """

    ts_code: str
    entry_price: float
    entry_date: date
    shares: int = 0
    cost_basis: float = 0.0
    holding_days_elapsed: int = 0
    highest_close_since_entry: float | None = None  # 用于移动止盈（None 则自动计算）


# ── 核心引擎 ──────────────────────────────────────────────────────


def compute_exit_signals(
    position: PositionState,
    exit_plan: ExitPlan,
    bars: pd.DataFrame,
    trade_date: date,
    *,
    prev_close: float | None = None,
) -> list[ExitSignal]:
    """评估 ExitPlan 中每条规则，返回当日触发的信号列表。

    Args:
        position: 当前持仓状态。
        exit_plan: 出场方案。
        bars: 该股票完整日线 DataFrame（含 close/high/low + 指标列 atr14/ma20/...）。
        trade_date: 当前交易日。
        prev_close: 前一日收盘（用于一字跌停检测，可选）。

    Returns:
        按 priority 排序的触发信号列表。调用方应取第一个（最高优先级）执行。

    Note:
        调用方负责一字跌停过滤 —— 此函数仅在价格维度评估，不判断流动性。
    """
    if bars.empty:
        return []

    # 对齐索引
    idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
    bars = bars.copy()
    bars.index = idx
    trade_ts = pd.Timestamp(trade_date)

    # 找当日 bar
    on_date = bars[bars.index == trade_ts]
    if on_date.empty:
        return []

    day_open = float(on_date["open"].iloc[0])
    day_high = float(on_date["high"].iloc[0])
    day_low = float(on_date["low"].iloc[0])
    day_close = float(on_date["close"].iloc[0])

    # 计算浮盈（从入场到当日收盘）
    current_return = (day_close - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0

    # 计算最高收盘价（用于移动止盈）
    highest_close = position.highest_close_since_entry
    if highest_close is None:
        # 自动计算：从入场日到当前日期的最高收盘价
        entry_ts = pd.Timestamp(position.entry_date)
        window = bars[(bars.index > entry_ts) & (bars.index <= trade_ts)]
        if not window.empty:
            highest_close = float(window["close"].max())
        else:
            highest_close = day_close
    else:
        # 更新为当日最高
        highest_close = max(highest_close, day_close)

    signals: list[ExitSignal] = []

    for rule in exit_plan.rules:
        # 检查激活条件
        if rule.activate_after_min_return is not None:
            if current_return < rule.activate_after_min_return:
                continue

        signal = _evaluate_rule(
            rule=rule,
            day_open=day_open,
            day_high=day_high,
            day_low=day_low,
            day_close=day_close,
            current_return=current_return,
            highest_close=highest_close,
            position=position,
            bars=bars,
            trade_date=trade_date,
        )
        if signal is not None:
            signals.append(signal)

    # 按 priority 排序
    signals.sort(key=lambda s: s.priority)
    return signals


def _evaluate_rule(
    rule: ExitRule,
    day_open: float,
    day_high: float,
    day_low: float,
    day_close: float,
    current_return: float,
    highest_close: float,
    position: PositionState,
    bars: pd.DataFrame,
    trade_date: date,
) -> ExitSignal | None:
    """评估单条规则是否触发。"""
    p = rule.params

    # ── 固定止损 ──
    if rule.rule_type == ExitType.STATIC_STOP:
        stop_price = _resolve_stop_price(p, position.entry_price)
        if stop_price is not None and day_low <= stop_price:
            return ExitSignal(
                rule_type=rule.rule_type,
                exit_price=stop_price,
                reason=f"触及止损价 ¥{stop_price:.2f}（入场 ¥{position.entry_price:.2f}，"
                       f"亏损 {(stop_price/position.entry_price-1)*100:+.1f}%）",
                priority=rule.priority or 0,
                rule_label=rule.label,
            )

    # ── 固定止盈 ──
    elif rule.rule_type == ExitType.STATIC_TAKE_PROFIT:
        target_price = _resolve_target_price(p, position.entry_price)
        if target_price is not None and day_high >= target_price:
            return ExitSignal(
                rule_type=rule.rule_type,
                exit_price=target_price,
                reason=f"触及止盈价 ¥{target_price:.2f}（入场 ¥{position.entry_price:.2f}，"
                       f"盈利 {(target_price/position.entry_price-1)*100:+.1f}%）",
                priority=rule.priority or 3,
                rule_label=rule.label,
            )

    # ── 移动止盈（从最高收盘价回撤 N%）──
    elif rule.rule_type == ExitType.TRAILING_STOP:
        trail_pct = p.get("trail_pct", 0.08)
        trailing_stop = highest_close * (1.0 - trail_pct)
        if day_low <= trailing_stop:
            return ExitSignal(
                rule_type=rule.rule_type,
                exit_price=trailing_stop,
                reason=f"触发移动止盈：从最高收盘价 ¥{highest_close:.2f} 回撤 {trail_pct*100:.0f}%"
                       f"→ 止损位 ¥{trailing_stop:.2f}",
                priority=rule.priority or 1,
                rule_label=rule.label,
            )

    # ── ATR 追踪止损 ──
    elif rule.rule_type == ExitType.ATR_TRAIL:
        atr_mult = p.get("atr_multiplier", 2.0)
        atr_period = p.get("atr_period", 14)
        atr_col = f"atr{atr_period}" if atr_period != 14 else "atr14"
        # 找当日 ATR 值
        atr_val = _get_indicator_value(bars, trade_date, atr_col)
        if atr_val is not None and atr_val > 0:
            atr_stop = highest_close - atr_mult * atr_val
            if day_low <= atr_stop:
                return ExitSignal(
                    rule_type=rule.rule_type,
                    exit_price=atr_stop,
                    reason=f"触发 ATR 追踪：最高收盘价 ¥{highest_close:.2f} - "
                           f"{atr_mult}×ATR({atr_period})=¥{atr_val:.2f}"
                           f"→ 止损位 ¥{atr_stop:.2f}",
                    priority=rule.priority or 1,
                    rule_label=rule.label,
                )

    # ── 均线止损 ──
    elif rule.rule_type == ExitType.MA_STOP:
        ma_period = p.get("ma_period", 20)
        ma_col = f"ma{ma_period}"
        ma_val = _get_indicator_value(bars, trade_date, ma_col)
        exit_on_cross = p.get("exit_on_cross_below", True)
        if ma_val is not None and ma_val > 0:
            if exit_on_cross and day_close < ma_val:
                return ExitSignal(
                    rule_type=rule.rule_type,
                    exit_price=day_close,
                    reason=f"收盘价 ¥{day_close:.2f} 跌破 MA{ma_period} ¥{ma_val:.2f}",
                    priority=rule.priority or 2,
                    rule_label=rule.label,
                )

    # ── 时间到期 ──
    elif rule.rule_type == ExitType.TIME_EXIT:
        max_days = p.get("max_holding_days", DEFAULT_MAX_HOLDING_DAYS)
        if position.holding_days_elapsed >= max_days:
            return ExitSignal(
                rule_type=rule.rule_type,
                exit_price=day_close,
                reason=f"持仓 {position.holding_days_elapsed} 天 ≥ {max_days} 天到期，市价离场",
                priority=rule.priority or 10,
                rule_label=rule.label,
            )

    # ── 分批止盈 ──
    elif rule.rule_type == ExitType.PARTIAL_TAKE_PROFIT:
        levels = p.get("levels", [])
        trailing_remainder = p.get("trailing_remainder", False)
        for i, level in enumerate(levels):
            at_pct = level.get("at_pct", 0.10)
            sell_ratio = level.get("sell_ratio", 1.0)
            target_price = position.entry_price * (1.0 + at_pct)
            if day_high >= target_price:
                return ExitSignal(
                    rule_type=rule.rule_type,
                    exit_price=target_price,
                    reason=f"分批止盈第{i+1}级：+{at_pct*100:.0f}% → ¥{target_price:.2f}，"
                           f"卖出 {sell_ratio*100:.0f}%",
                    priority=rule.priority or 4,
                    sell_ratio=sell_ratio,
                    rule_label=rule.label,
                )

    return None


# ── 辅助函数 ──────────────────────────────────────────────────────


def _resolve_stop_price(params: dict, entry_price: float) -> float | None:
    """解析止损价（支持百分比或绝对价）。"""
    if "stop_price" in params:
        return float(params["stop_price"])
    if "stop_pct" in params:
        return round(entry_price * (1.0 + float(params["stop_pct"])), 2)
    return None


def _resolve_target_price(params: dict, entry_price: float) -> float | None:
    """解析止盈价（支持百分比或绝对价）。"""
    if "target_price" in params:
        return float(params["target_price"])
    if "target_pct" in params:
        return round(entry_price * (1.0 + float(params["target_pct"])), 2)
    return None


def _get_indicator_value(
    bars: pd.DataFrame, trade_date: date, column: str,
) -> float | None:
    """从 bars 中取指定日期的指标值。"""
    trade_ts = pd.Timestamp(trade_date)
    if column not in bars.columns:
        return None
    on_date = bars[bars.index == trade_ts]
    if on_date.empty:
        return None
    val = on_date[column].iloc[0]
    if pd.isna(val):
        return None
    return float(val)


# ── 工厂函数：从 YAML 配置构建 ExitPlan ────────────────────────────


def build_exit_plan_from_config(
    plan_key: str,
    config: dict[str, Any],
    *,
    entry_price: float = 0.0,
) -> ExitPlan | None:
    """从 strategy.yaml 的 exit.plans.<plan_key> 构建 ExitPlan。

    config 结构::

        {
            "type": "trailing_stop",
            "trail_pct": 0.08,
            "activate_after_min_return": 0.03,
            "max_holding_days": 20,
        }
    """
    if not config:
        return None

    plan_type = config.get("type", plan_key)
    rules: list[ExitRule] = []
    activate_after = config.get("activate_after_min_return")

    # 根据 type 构建规则
    if plan_type == "static":
        rules.append(ExitRule(
            rule_type=ExitType.STATIC_STOP,
            priority=0,
            params={"stop_pct": config.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)},
        ))
        tp = config.get("take_profit_pct")
        if tp is not None:
            rules.append(ExitRule(
                rule_type=ExitType.STATIC_TAKE_PROFIT,
                priority=3,
                params={"target_pct": float(tp)},
            ))
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": config.get("max_holding_days", DEFAULT_MAX_HOLDING_DAYS)},
        ))

    elif plan_type == "trailing_stop":
        rules.append(ExitRule(
            rule_type=ExitType.TRAILING_STOP,
            priority=1,
            params={"trail_pct": config.get("trail_pct", 0.08)},
            activate_after_min_return=activate_after,
        ))
        tp = config.get("take_profit_pct")
        if tp:
            rules.append(ExitRule(
                rule_type=ExitType.STATIC_TAKE_PROFIT,
                priority=3,
                params={"target_pct": float(tp)},
            ))
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": config.get("max_holding_days", 20)},
        ))

    elif plan_type == "atr_trail":
        rules.append(ExitRule(
            rule_type=ExitType.ATR_TRAIL,
            priority=1,
            params={
                "atr_multiplier": config.get("atr_multiplier", 2.0),
                "atr_period": config.get("atr_period", 14),
            },
            activate_after_min_return=activate_after,
        ))
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": config.get("max_holding_days", 20)},
        ))

    elif plan_type == "ma_stop":
        rules.append(ExitRule(
            rule_type=ExitType.MA_STOP,
            priority=2,
            params={
                "ma_period": config.get("ma_period", 20),
                "ma_type": config.get("ma_type", "simple"),
                "exit_on_cross_below": config.get("exit_on_cross_below", True),
            },
        ))
        # 叠加一个保底止损
        if config.get("stop_loss_pct"):
            rules.append(ExitRule(
                rule_type=ExitType.STATIC_STOP,
                priority=0,
                params={"stop_pct": float(config["stop_loss_pct"])},
            ))
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": config.get("max_holding_days", 20)},
        ))

    elif plan_type == "partial_take_profit":
        levels = config.get("levels", [])
        tp_rule = ExitRule(
            rule_type=ExitType.PARTIAL_TAKE_PROFIT,
            priority=4,
            params={
                "levels": levels,
                "trailing_remainder": config.get("trailing_remainder", False),
            },
        )
        rules.append(tp_rule)
        # 止损兜底
        if config.get("stop_loss_pct"):
            rules.append(ExitRule(
                rule_type=ExitType.STATIC_STOP,
                priority=0,
                params={"stop_pct": float(config["stop_loss_pct"])},
            ))
        # 剩余仓位到期出场
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": config.get("max_holding_days", 30)},
        ))

    else:
        # 未知类型 → 退化为静态
        rules.append(ExitRule(
            rule_type=ExitType.STATIC_STOP,
            priority=0,
            params={"stop_pct": DEFAULT_STOP_LOSS_PCT},
        ))
        rules.append(ExitRule(
            rule_type=ExitType.TIME_EXIT,
            priority=10,
            params={"max_holding_days": DEFAULT_MAX_HOLDING_DAYS},
        ))

    return ExitPlan(
        plan_key=plan_key,
        display_name=config.get("display_name", plan_key),
        rules=rules,
        description=config.get("description", ""),
    )


# ── 便捷函数：批量跑多方案对比 ────────────────────────────────────


def simulate_exit_for_pick(
    entry_price: float,
    entry_date: date,
    bars: pd.DataFrame,
    exit_plan: ExitPlan,
    *,
    max_holding_days_override: int | None = None,
) -> dict[str, Any]:
    """对单只票历史 K 线跑一个出场方案，返回完整模拟结果。

    Args:
        entry_price: 入场价。
        entry_date: 入场日期。
        bars: 该票完整日线 DataFrame（已含指标列）。
        exit_plan: 出场方案。
        max_holding_days_override: 覆盖最大持仓天数（用于限制回测窗口）。

    Returns:
        {
            "exit_date": date | None,
            "exit_price": float | None,
            "exit_reason": str,
            "return_pct": float | None,
            "holding_days": int,
            "signals_triggered": [...],
        }
    """
    if bars.empty:
        return {
            "exit_date": None, "exit_price": None, "exit_reason": "无行情数据",
            "return_pct": None, "holding_days": 0, "signals_triggered": [],
        }

    idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
    bars = bars.copy()
    bars.index = idx
    entry_ts = pd.Timestamp(entry_date)

    # 从入场次日开始
    future = bars[bars.index > entry_ts]
    if future.empty:
        return {
            "exit_date": None, "exit_price": None, "exit_reason": "无入场后数据",
            "return_pct": None, "holding_days": 0, "signals_triggered": [],
        }

    max_days = max_holding_days_override or DEFAULT_MAX_HOLDING_DAYS
    # 从规则中提取最大持仓天数
    for rule in exit_plan.rules:
        if rule.rule_type == ExitType.TIME_EXIT:
            max_days = rule.params.get("max_holding_days", max_days)

    position = PositionState(
        ts_code="",
        entry_price=entry_price,
        entry_date=entry_date,
        holding_days_elapsed=0,
    )

    trade_dates = sorted(future.index.unique())[:max_days + 5]

    for td in trade_dates:
        td_date = td.date()
        position.holding_days_elapsed += 1
        signals = compute_exit_signals(position, exit_plan, bars, td_date)
        if signals:
            sig = signals[0]
            ret_pct = round((sig.exit_price - entry_price) / entry_price * 100, 2)
            return {
                "exit_date": str(td_date),
                "exit_price": round(sig.exit_price, 2),
                "exit_reason": sig.reason,
                "return_pct": ret_pct,
                "holding_days": position.holding_days_elapsed,
                "rule_type": sig.rule_type.value,
                "signals_triggered": [
                    {"rule_type": s.rule_type.value, "priority": s.priority, "reason": s.reason}
                    for s in signals
                ],
            }

    # 未触发任何规则 → 按最后一根 bar 收盘价强制平仓
    last_bar = future.iloc[-1]
    last_close = float(last_bar["close"])
    last_date = future.index[-1].date()
    ret_pct = round((last_close - entry_price) / entry_price * 100, 2)
    return {
        "exit_date": str(last_date),
        "exit_price": round(last_close, 2),
        "exit_reason": "未触发任何规则，强制平仓",
        "return_pct": ret_pct,
        "holding_days": len(trade_dates),
        "rule_type": "forced",
        "signals_triggered": [],
    }
