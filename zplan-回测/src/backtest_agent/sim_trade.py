"""模拟交易引擎：历史批量回放 + 每日纸质交易。

核心组件：
  SimBroker   — 撮合引擎（次日开盘成交、涨跌停过滤、费用、整手）
  SimPortfolio — 账户管理（现金、持仓、净值曲线）
  SimStrategy — 策略适配器（从 pick_entries 加载信号）
  SimEngine   — 主循环（遍历日期 → 重估 → 止损止盈 → 新信号入场）
  SimResult   — 指标计算 + Markdown/JSON 报告

用法::

    cd zplan-回测 && .venv/bin/python main.py sim-trade --run-id 26 --top 5
    cd zplan-回测 && .venv/bin/python main.py sim-trade --from 2026-04-01 --to 2026-06-09 --top 5
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import desc, select

from zplan_shared.market import get_bars, resolve_ts_code
from zplan_shared.models import (
    DailyPrice,
    PickEntry,
    PickLlmEvaluation,
    PickRun,
    SessionLocal,
    init_db,
)
from zplan_shared.exit_strategy import (
    ExitPlan,
    ExitSignal,
    ExitType,
    PositionState,
    compute_exit_signals,
)

# ── A 股费用常数 ────────────────────────────────────────────────
STAMP_DUTY_SELL = 0.0005       # 印花税 0.05%（卖出单向，2024.8 降税后）
COMMISSION_RATE = 0.00025       # 佣金 0.025%（双向）
COMMISSION_MIN = 5.0            # 最低佣金 ¥5
TRANSFER_FEE_RATE = 0.00001    # 过户费 0.001%（双向）
LOT_SIZE = 100                  # 整手


# ── 数据类 ──────────────────────────────────────────────────────


@dataclass
class OrderResult:
    """单笔订单执行结果。"""
    ts_code: str
    name: str
    side: str                      # "buy" | "sell"
    order_price: float
    filled_price: float | None     # None 表示未成交
    shares: int
    filled_value: float | None
    fees: float
    status: str                    # "filled" | "rejected" | "pending"
    reject_reason: str
    signal_source: str             # pick_run_id 或 "manual"
    entry_id: int | None = None    # pick_entries.id


@dataclass
class Position:
    """当前持仓。"""
    ts_code: str
    name: str
    entry_date: date
    entry_price: float
    shares: int
    cost_basis: float              # 含费用的总成本
    stop_loss: float | None = None           # 旧字段保留向后兼容
    take_profit: float | None = None         # 旧字段保留向后兼容
    holding_days_max: int = 5
    holding_days_elapsed: int = 0
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str = ""          # "stop_loss" | "take_profit" | "expired" | "forced"
    exit_plan: ExitPlan | None = None  # 新出场方案（优先于旧三参数）
    highest_close_since_entry: float | None = None  # 用于移动止盈


@dataclass
class EquityPoint:
    """每日净值快照。"""
    trade_date: date
    cash: float
    position_value: float
    total_equity: float
    daily_return_pct: float = 0.0


# ── 撮合引擎 ────────────────────────────────────────────────────


class SimBroker:
    """用历史 K 线模拟交易撮合。

    买入：次日开盘价成交，支持限价单。
    卖出：市价（当日 close）/ 止损价 / 止盈价。
    包含涨跌停过滤、费用计算、整手约束。
    """

    def __init__(self, *, slippage_pct: float = 0.0):
        self.slippage_pct = slippage_pct
        self._bar_cache: dict[str, pd.DataFrame] = {}

    # ── 公开 API ──

    def execute_buy(
        self,
        ts_code: str,
        name: str,
        as_of: date,
        *,
        cash: float,
        predicted_buy: float | None = None,
        max_portion: float = 1.0,
        signal_source: str = "",
    ) -> OrderResult:
        """在 as_of 次日开盘买入。

        Args:
            max_portion: 该信号最多占用现金的比例（等权 = 1/N）。
            predicted_buy: 限价位，若 next_open > predicted_buy 则不成交。
        """
        # 1) 取次日 bar
        next_bar = self._next_trading_bar(ts_code, as_of)
        if next_bar is None:
            return OrderResult(
                ts_code=ts_code, name=name, side="buy",
                order_price=0, filled_price=None, shares=0,
                filled_value=None, fees=0, status="rejected",
                reject_reason="as_of 后无交易日", signal_source=signal_source,
            )

        next_open = float(next_bar["open"])
        next_low = float(next_bar["low"])
        next_high = float(next_bar["high"])
        next_close = float(next_bar["close"])
        next_vol = float(next_bar.get("volume", 0))
        trade_date = self._bar_date(next_bar)

        # 2) 涨跌停检查
        prev_close = self._prev_close(ts_code, trade_date)
        limit_up = round(prev_close * 1.10, 2) if prev_close else None
        limit_down = round(prev_close * 0.90, 2) if prev_close else None

        if limit_up and self._is_limit_locked(next_open, next_high, next_low, next_close, limit_up, next_vol):
            return OrderResult(
                ts_code=ts_code, name=name, side="buy",
                order_price=next_open, filled_price=None, shares=0,
                filled_value=None, fees=0, status="rejected",
                reject_reason=f"一字涨停 ({limit_up})，无法买入", signal_source=signal_source,
            )

        # 3) 限价检查
        entry_price = next_open
        if self.slippage_pct:
            entry_price *= (1.0 + self.slippage_pct)
            entry_price = round(entry_price, 2)

        if predicted_buy is not None and entry_price > predicted_buy:
            return OrderResult(
                ts_code=ts_code, name=name, side="buy",
                order_price=predicted_buy, filled_price=None, shares=0,
                filled_value=None, fees=0, status="rejected",
                reject_reason=f"开盘 ¥{entry_price:.2f} > 限价 ¥{predicted_buy:.2f}",
                signal_source=signal_source,
            )

        # 4) 整手计算
        budget = cash * max_portion
        raw_shares = int(budget / entry_price / LOT_SIZE) * LOT_SIZE
        if raw_shares < LOT_SIZE:
            return OrderResult(
                ts_code=ts_code, name=name, side="buy",
                order_price=entry_price, filled_price=None, shares=0,
                filled_value=None, fees=0, status="rejected",
                reject_reason=f"资金不足（需 ¥{entry_price * LOT_SIZE:.0f}，可用 ¥{budget:.0f}）",
                signal_source=signal_source,
            )

        filled_value = raw_shares * entry_price
        fees = self._calc_fees(filled_value, side="buy")
        total_cost = filled_value + fees
        if total_cost > cash:
            raw_shares = int((cash - fees) / entry_price / LOT_SIZE) * LOT_SIZE
            if raw_shares < LOT_SIZE:
                return OrderResult(
                    ts_code=ts_code, name=name, side="buy",
                    order_price=entry_price, filled_price=None, shares=0,
                    filled_value=None, fees=0, status="rejected",
                    reject_reason="资金不足（含费后）", signal_source=signal_source,
                )
            filled_value = raw_shares * entry_price
            fees = self._calc_fees(filled_value, side="buy")

        return OrderResult(
            ts_code=ts_code, name=name, side="buy",
            order_price=entry_price, filled_price=entry_price,
            shares=raw_shares, filled_value=filled_value, fees=fees,
            status="filled", reject_reason="", signal_source=signal_source,
        )

    def check_exit(
        self,
        position: Position,
        trade_date: date,
    ) -> tuple[float | None, str]:
        """检查持仓是否应出场。

        优先使用 position.exit_plan（新引擎），退回旧三参数逻辑。

        Returns:
            (exit_price, reason)。exit_price 为 None 表示继续持有。
        """
        bar = self._bar_on_date(position.ts_code, trade_date)
        if bar is None:
            return None, ""

        day_low = float(bar["low"])
        day_high = float(bar["high"])
        day_close = float(bar["close"])

        # 检查一字跌停（无法卖出）
        prev_close = self._prev_close(position.ts_code, trade_date)
        limit_down = round(prev_close * 0.90, 2) if prev_close else None
        if limit_down and self._is_limit_locked(
            float(bar["open"]), day_high, day_low, day_close,
            limit_down, float(bar.get("volume", 0)),
        ):
            return None, ""  # 跌停锁死，卖不掉，延后

        # ── 新引擎：ExitPlan 优先 ──
        if position.exit_plan is not None:
            bars = self._load_bars(position.ts_code)
            if not bars.empty:
                pos_state = PositionState(
                    ts_code=position.ts_code,
                    entry_price=position.entry_price,
                    entry_date=position.entry_date,
                    shares=position.shares,
                    cost_basis=position.cost_basis,
                    holding_days_elapsed=position.holding_days_elapsed,
                    highest_close_since_entry=position.highest_close_since_entry,
                )
                signals = compute_exit_signals(
                    pos_state, position.exit_plan, bars, trade_date,
                    prev_close=prev_close,
                )
                if signals:
                    sig = signals[0]  # 最高优先级
                    # 更新最高收盘价追踪
                    idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
                    window = bars.copy()
                    window.index = idx
                    entry_ts = pd.Timestamp(position.entry_date)
                    trade_ts = pd.Timestamp(trade_date)
                    recent = window[(window.index > entry_ts) & (window.index <= trade_ts)]
                    if not recent.empty:
                        position.highest_close_since_entry = max(
                            position.highest_close_since_entry or 0,
                            float(recent["close"].max()),
                        )
                    return sig.exit_price, sig.reason

        # ── 旧引擎：三参数逻辑（向后兼容）──
        # 止损优先（保守原则）
        if position.stop_loss is not None and day_low <= position.stop_loss:
            return position.stop_loss, "stop_loss"
        # 止盈
        if position.take_profit is not None and day_high >= position.take_profit:
            return position.take_profit, "take_profit"
        # 持仓期满
        if position.holding_days_elapsed >= position.holding_days_max:
            return day_close, "expired"

        return None, ""

    def get_bars_window(
        self, ts_code: str, start: date, end: date,
    ) -> pd.DataFrame:
        """获取某只股票在日期区间内的日线。"""
        bars = self._load_bars(ts_code)
        if bars.empty:
            return bars
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        bars = bars.copy()
        bars.index = idx
        return bars[(bars.index >= pd.Timestamp(start)) & (bars.index <= pd.Timestamp(end))]

    # ── 内部方法 ──

    def _next_trading_bar(self, ts_code: str, as_of: date) -> pd.Series | None:
        bars = self._load_bars(ts_code)
        if bars.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        bars = bars.copy()
        bars.index = idx
        after = bars[bars.index > pd.Timestamp(as_of)]
        if after.empty:
            return None
        return after.iloc[0]

    def _bar_on_date(self, ts_code: str, trade_date: date) -> pd.Series | None:
        bars = self._load_bars(ts_code)
        if bars.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        on = bars[idx == pd.Timestamp(trade_date)]
        if on.empty:
            return None
        return on.iloc[0]

    def _prev_close(self, ts_code: str, before: date) -> float | None:
        bars = self._load_bars(ts_code)
        if bars.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        before_ts = pd.Timestamp(before)
        prev = bars[idx < before_ts]
        if prev.empty:
            return None
        return float(prev["close"].iloc[-1])

    def _load_bars(self, ts_code: str) -> pd.DataFrame:
        code = resolve_ts_code(ts_code)
        if code not in self._bar_cache:
            self._bar_cache[code] = get_bars(code)
        return self._bar_cache[code]

    @staticmethod
    def _bar_date(bar: pd.Series) -> date:
        idx = bar.name
        if isinstance(idx, pd.Timestamp):
            return idx.date()
        if isinstance(idx, datetime):
            return idx.date()
        return date.fromisoformat(str(idx)[:10])

    @staticmethod
    def _is_limit_locked(
        open_: float, high: float, low: float, close: float,
        limit_price: float, volume: float,
    ) -> bool:
        """一字板：开高低收同价 + 无成交量。"""
        return (
            open_ == high == low == close == limit_price
            and volume < 100  # 接近零量
        )

    @staticmethod
    def _calc_fees(trade_value: float, *, side: str) -> float:
        commission = max(trade_value * COMMISSION_RATE, COMMISSION_MIN)
        transfer = trade_value * TRANSFER_FEE_RATE
        stamp = trade_value * STAMP_DUTY_SELL if side == "sell" else 0.0
        return round(commission + transfer + stamp, 4)

    @staticmethod
    def _pre_close_static(bars: pd.DataFrame, as_of: date) -> float | None:
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        on_or_before = bars[idx <= pd.Timestamp(as_of)]
        if on_or_before.empty:
            return None
        return float(on_or_before["close"].iloc[-1])


# ── 账户管理 ────────────────────────────────────────────────────


class SimPortfolio:
    """模拟账户：现金 + 持仓 + 净值曲线。"""

    def __init__(self, initial_capital: float = 100_000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}           # ts_code → Position
        self.closed_positions: list[Position] = []
        self.equity_curve: list[EquityPoint] = []
        self._broker = SimBroker()

    @property
    def broker(self) -> SimBroker:
        return self._broker

    @property
    def total_equity(self) -> float:
        return self.cash + self._position_value()

    @property
    def position_count(self) -> int:
        return len(self.positions)

    # ── 操作 ──

    def buy(
        self,
        ts_code: str,
        name: str,
        as_of: date,
        *,
        predicted_buy: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        holding_days_max: int = 5,
        max_portion: float = 1.0,
        signal_source: str = "",
        entry_id: int | None = None,
        exit_plan: ExitPlan | None = None,
    ) -> OrderResult:
        """提交买单并更新账户。"""
        if ts_code in self.positions:
            return OrderResult(
                ts_code=ts_code, name=name, side="buy",
                order_price=0, filled_price=None, shares=0,
                filled_value=None, fees=0, status="rejected",
                reject_reason="已持有该标的", signal_source=signal_source,
            )

        result = self._broker.execute_buy(
            ts_code=ts_code, name=name, as_of=as_of,
            cash=self.cash, predicted_buy=predicted_buy,
            max_portion=max_portion, signal_source=signal_source,
        )
        result.entry_id = entry_id

        if result.status == "filled" and result.filled_price is not None:
            self.positions[ts_code] = Position(
                ts_code=ts_code, name=name,
                entry_date=self._broker._next_trading_bar(ts_code, as_of).name.date()
                if self._broker._next_trading_bar(ts_code, as_of) is not None
                else as_of + timedelta(days=1),
                entry_price=result.filled_price,
                shares=result.shares,
                cost_basis=result.filled_value + result.fees,
                stop_loss=stop_loss,
                take_profit=take_profit,
                holding_days_max=holding_days_max,
                holding_days_elapsed=0,
                exit_plan=exit_plan,
                highest_close_since_entry=result.filled_price,
            )

        return result

    def close_position(
        self, ts_code: str, exit_date: date, exit_price: float, reason: str,
    ) -> float:
        """平仓并返回实现盈亏。"""
        pos = self.positions.pop(ts_code, None)
        if pos is None:
            return 0.0

        exit_value = pos.shares * exit_price
        fees = self._broker._calc_fees(exit_value, side="sell")
        proceeds = exit_value - fees
        realized_pnl = proceeds - pos.cost_basis

        pos.exit_date = exit_date
        pos.exit_price = exit_price
        pos.exit_reason = reason
        self.closed_positions.append(pos)

        self.cash += proceeds
        return realized_pnl

    def force_close_all(self, trade_date: date) -> list[tuple[str, float, str]]:
        """强制平掉所有持仓（按当日收盘价）。"""
        results: list[tuple[str, float, str]] = []
        # 复制 keys 避免遍历时修改 dict
        for ts_code in list(self.positions.keys()):
            bar = self._broker._bar_on_date(ts_code, trade_date)
            if bar is not None:
                exit_px = float(bar["close"])
                pnl = self.close_position(ts_code, trade_date, exit_px, "forced")
                results.append((ts_code, pnl, "forced"))
        return results

    def update_daily(
        self, trade_date: date, *,
        prev_total: float | None = None,
    ) -> EquityPoint:
        """按当日收盘价重估持仓，记录净值快照。"""
        # 推进持仓天数
        for pos in self.positions.values():
            pos.holding_days_elapsed += 1

        pos_value = self._position_value()
        total = self.cash + pos_value
        daily_ret = 0.0
        if prev_total is not None and prev_total > 0:
            daily_ret = round((total - prev_total) / prev_total * 100, 6)

        ep = EquityPoint(
            trade_date=trade_date,
            cash=round(self.cash, 2),
            position_value=round(pos_value, 2),
            total_equity=round(total, 2),
            daily_return_pct=daily_ret,
        )
        self.equity_curve.append(ep)
        return ep

    # ── 内部 ──

    def _position_value(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            bars = self._broker._load_bars(pos.ts_code)
            if bars.empty:
                total += pos.cost_basis  # 无行情则按成本估值
                continue
            idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
            bars = bars.copy()
            bars.index = idx
            total += pos.shares * float(bars["close"].iloc[-1])
        return round(total, 2)


# ── 策略适配器 ──────────────────────────────────────────────────


class SimStrategy:
    """从 pick_entries / pick_runs 生成交易信号。"""

    def __init__(
        self,
        *,
        top_n: int = 5,
        min_llm_score: float | None = None,
        min_rule_score: float | None = None,
        holding_days: int = 5,
        stop_loss_pct: float = -0.05,
        take_profit_pct: float | None = None,
        exclude_tags: list[str] | None = None,
        sizing_mode: str = "equal",  # "equal" | "score_weighted" | "risk_parity"
        entry_rule: str = "next_open",  # "next_open" | "limit"
        exit_plan: ExitPlan | None = None,  # 新：统一出场方案
        exit_plan_key: str | None = None,   # 方案 key（从 strategy.yaml 加载）
        per_pick_exit_plans: bool = True,   # 是否使用逐票 LLM 推荐的方案
    ):
        self.top_n = top_n
        self.min_llm_score = min_llm_score
        self.min_rule_score = min_rule_score
        self.holding_days = holding_days
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.exclude_tags = exclude_tags or []
        self.sizing_mode = sizing_mode
        self.entry_rule = entry_rule
        self.exit_plan = exit_plan
        self.exit_plan_key = exit_plan_key
        self.per_pick_exit_plans = per_pick_exit_plans

    def load_picks_from_run(
        self, run_id: int,
    ) -> list[dict[str, Any]]:
        """从指定 pick_run 加载信号。"""
        init_db()
        with SessionLocal() as session:
            run = session.get(PickRun, run_id)
            if run is None:
                return []

            entries = session.execute(
                select(PickEntry)
                .where(PickEntry.run_id == run_id)
                .order_by(PickEntry.rank_in_run, PickEntry.id)
                .limit(self.top_n * 2)  # 多拿一些做过滤
            ).scalars().all()

            # 加载失败标签
            entry_ids = [e.id for e in entries]
            tag_map: dict[int, list[str]] = {}
            if entry_ids and self.exclude_tags:
                evals = session.execute(
                    select(PickLlmEvaluation)
                    .where(PickLlmEvaluation.entry_id.in_(entry_ids))
                ).scalars().all()
                for ev in evals:
                    tags = json.loads(ev.failure_tags_json or "[]")
                    tag_map[ev.entry_id] = tags

            signals: list[dict[str, Any]] = []
            for e in entries:
                # 过滤
                if self.min_llm_score and (e.llm_composite_score or 0) < self.min_llm_score:
                    continue
                if self.min_rule_score and (e.rule_composite_score or 0) < self.min_rule_score:
                    continue
                if self.exclude_tags:
                    tags = tag_map.get(e.id, [])
                    if any(t in tags for t in self.exclude_tags):
                        continue

                # 取 as_of 当日真实收盘价（不依赖 pick_entry.close_price，避免数据不一致）
                actual_close = self._get_close_on_date(e.ts_code, run.trade_date_as_of)
                entry_price = actual_close if actual_close else (e.close_price or 0)
                stop_loss = None
                take_profit = None

                # ── 老参数：仅在没有 ExitPlan 时生效 ──
                signal_exit_plan = self.exit_plan
                if self.per_pick_exit_plans and e.exit_plan_json:
                    try:
                        plan_dict = json.loads(e.exit_plan_json)
                        signal_exit_plan = ExitPlan.from_dict(plan_dict)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass

                if signal_exit_plan is None:
                    # 无 ExitPlan → 用旧三参数兜底
                    if entry_price > 0:
                        if self.stop_loss_pct:
                            stop_loss = round(entry_price * (1.0 + self.stop_loss_pct), 2)
                        if self.take_profit_pct:
                            take_profit = round(entry_price * (1.0 + self.take_profit_pct), 2)

                signal: dict[str, Any] = {
                    "entry_id": e.id,
                    "ts_code": e.ts_code,
                    "name": e.name or e.ts_code,
                    "rank": e.rank_in_run,
                    "llm_score": e.llm_composite_score,
                    "rule_score": e.rule_composite_score,
                    "close_price": entry_price,
                    "predicted_buy": e.predicted_buy_price,
                    "predicted_target": e.predicted_target_price,
                    "predicted_stop": e.predicted_stop_loss,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "recommendation": e.recommendation,
                }

                # ── 出场方案（逐票 > 策略默认）──
                signal["exit_plan"] = signal_exit_plan

                signals.append(signal)

                if len(signals) >= self.top_n:
                    break

        return signals

    def get_weights(self, signals: list[dict[str, Any]]) -> list[float]:
        """根据 sizing_mode 返回每笔信号的资金权重。"""
        n = len(signals)
        if n == 0:
            return []

        if self.sizing_mode == "score_weighted":
            scores = [s.get("llm_score") or s.get("rule_score") or 50 for s in signals]
            # softmax-like: exp(score/20)
            weights = [math.exp(s / 20) for s in scores]
            total = sum(weights)
            return [w / total for w in weights]

        # equal（默认）: 每只等权
        return [1.0 / n] * n

    def load_all_llm_runs(
        self,
        from_date: date | None = None,
        to_date: date | None = None,
        *,
        rule_version: str | None = None,
        variant_label: str | None = None,
        llm_only: bool = False,
    ) -> list[dict[str, Any]]:
        """加载时间范围内所有 LLM pick runs 的元信息。"""
        init_db()
        with SessionLocal() as session:
            stmt = (
                select(PickRun)
                .where(
                    PickRun.run_kind.in_(["llm_top300", "scan"]),
                    PickRun.llm_enabled.is_(True),
                )
                .order_by(PickRun.trade_date_as_of, PickRun.id)
            )
            if from_date:
                stmt = stmt.where(PickRun.trade_date_as_of >= from_date)
            if to_date:
                stmt = stmt.where(PickRun.trade_date_as_of <= to_date)
            if rule_version:
                stmt = stmt.where(PickRun.rule_version == rule_version)
            if variant_label:
                stmt = stmt.where(PickRun.variant_label == variant_label)

            runs = session.execute(stmt).scalars().all()

        result = []
        for r in runs:
            if r.trade_date_as_of is None:
                continue
            # llm_only: 仅保留有 LLM 评分的 run（排除纯规则扫描）
            if llm_only:
                has_llm = self._run_has_llm_scores(r.id)
                if not has_llm:
                    continue
            result.append({
                "run_id": r.id,
                "trade_date_as_of": r.trade_date_as_of,
                "rule_version": r.rule_version,
                "variant_label": r.variant_label or "",
                "run_kind": r.run_kind,
            })
        return result

    @staticmethod
    def _run_has_llm_scores(run_id: int) -> bool:
        """检查 run 是否有 LLM 评分（非纯规则扫描）。"""
        init_db()
        with SessionLocal() as session:
            row = session.execute(
                select(PickEntry.llm_composite_score)
                .where(PickEntry.run_id == run_id, PickEntry.llm_composite_score.isnot(None))
                .limit(1)
            ).scalar_one_or_none()
        return row is not None

    @staticmethod
    def _get_close_on_date(ts_code: str, as_of) -> float | None:
        """取某日真实收盘价。"""
        if as_of is None:
            return None
        bars = get_bars(ts_code)
        if bars.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        bars = bars.copy()
        bars.index = idx
        on = bars[bars.index == pd.Timestamp(as_of)]
        if on.empty:
            return None
        return float(on["close"].iloc[-1])


# ── 主引擎 ──────────────────────────────────────────────────────


class SimEngine:
    """模拟交易主循环。"""

    def __init__(
        self,
        strategy: SimStrategy,
        initial_capital: float = 100_000.0,
        *,
        max_positions: int = 5,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.portfolio: SimPortfolio | None = None

    def run_single(
        self, run_id: int, *, label: str = "",
    ) -> dict[str, Any]:
        """对单个 pick_run 执行模拟。"""
        signals = self.strategy.load_picks_from_run(run_id)
        if not signals:
            return {"ok": False, "message": f"run_id={run_id} 无可用信号", "run_id": run_id}

        # 确定基准日期
        init_db()
        with SessionLocal() as session:
            run = session.get(PickRun, run_id)
        as_of = run.trade_date_as_of if run else None
        rule_version = run.rule_version if run else ""
        if as_of is None:
            return {"ok": False, "message": f"run_id={run_id} 无 trade_date_as_of", "run_id": run_id}

        pf = SimPortfolio(initial_capital=self.initial_capital)
        self.portfolio = pf

        weights = self.strategy.get_weights(signals)

        # 买入日 = as_of + 1
        buy_results: list[OrderResult] = []
        for i, sig in enumerate(signals):
            # 持有天数：有 exit_plan 时由其 TIME_EXIT 规则控制，否则用策略默认
            holding_days = self.strategy.holding_days
            ep = sig.get("exit_plan")
            if ep:
                for rule in ep.rules:
                    if rule.rule_type == ExitType.TIME_EXIT:
                        holding_days = rule.params.get("max_holding_days", holding_days)
                        break

            result = pf.buy(
                ts_code=sig["ts_code"], name=sig["name"], as_of=as_of,
                predicted_buy=sig["predicted_buy"] if self.strategy.entry_rule == "limit" else None,
                stop_loss=sig["stop_loss"],
                take_profit=sig["take_profit"],
                holding_days_max=holding_days,
                max_portion=weights[i],
                signal_source=f"run{run_id}",
                entry_id=sig.get("entry_id"),
                exit_plan=ep,
            )
            buy_results.append(result)
            if pf.position_count >= self.max_positions:
                break

        filled = [r for r in buy_results if r.status == "filled"]
        if not filled:
            return {
                "ok": True, "run_id": run_id, "as_of": str(as_of),
                "signals": len(signals), "filled": 0,
                "rejected_reasons": [r.reject_reason for r in buy_results if r.reject_reason],
                "metrics": None, "trades": [], "equity_curve": [],
            }

        # 确定入场日（取第一个成交单的交易日）
        first_code = filled[0].ts_code
        first_bar = pf.broker._next_trading_bar(first_code, as_of)
        if first_bar is None:
            entry_date = as_of + timedelta(days=1)
        else:
            entry_date = SimBroker._bar_date(first_bar)

        # 起始净值
        pf.update_daily(entry_date, prev_total=self.initial_capital)

        # 模拟后续交易日
        all_trade_dates = self._collect_trade_dates(
            [s["ts_code"] for s in signals], entry_date,
            self.strategy.holding_days + 3,
        )

        prev_total = pf.total_equity
        for td in all_trade_dates:
            # 检查出场
            for ts_code in list(pf.positions.keys()):
                exit_px, reason = pf.broker.check_exit(pf.positions[ts_code], td)
                if exit_px is not None:
                    pf.close_position(ts_code, td, exit_px, reason)

            # 记录净值
            ep = pf.update_daily(td, prev_total=prev_total)
            prev_total = pf.total_equity

            # 全部平仓则提前结束
            if not pf.positions:
                break

        # 持仓期满仍未平仓 → 强制平仓
        if pf.positions:
            last_date = all_trade_dates[-1] if all_trade_dates else entry_date
            pf.force_close_all(last_date)
            pf.update_daily(last_date, prev_total=prev_total)

        return self._build_result(
            run_id=run_id, as_of=str(as_of), label=label,
            rule_version=rule_version,
            signals_total=len(signals), filled=len(filled),
            buy_results=buy_results,
        )

    def run_batch(
        self,
        from_date: date | None = None,
        to_date: date | None = None,
        *,
        rule_version: str | None = None,
        variant_label: str | None = None,
        llm_only: bool = False,
    ) -> dict[str, Any]:
        """跨期批量回放：遍历所有历史 LLM pick runs。"""
        runs = self.strategy.load_all_llm_runs(
            from_date, to_date,
            rule_version=rule_version, variant_label=variant_label,
            llm_only=llm_only,
        )
        if not runs:
            return {"ok": False, "message": "指定区间无 LLM pick runs"}

        all_results: list[dict[str, Any]] = []
        for r in runs:
            res = self.run_single(r["run_id"], label=r.get("variant_label") or "")
            all_results.append(res)

        return self._aggregate_batch(all_results)

    # ── 内部 ──

    def _collect_trade_dates(
        self, ts_codes: list[str], after: date, max_days: int,
    ) -> list[date]:
        """收集各标的在 after 之后的交易日并集。"""
        all_dates: set[date] = set()
        for code in ts_codes:
            bars = self.portfolio.broker._load_bars(code) if self.portfolio else pd.DataFrame()
            if bars.empty:
                continue
            idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
            after_ts = pd.Timestamp(after)
            for d in idx[idx > after_ts][:max_days]:
                all_dates.add(d.date())
        return sorted(all_dates)

    def _build_result(
        self,
        run_id: int, as_of: str, label: str,
        rule_version: str = "",
        signals_total: int = 0, filled: int = 0,
        buy_results: list[OrderResult] | None = None,
    ) -> dict[str, Any]:
        buy_results = buy_results or []
        pf = self.portfolio
        if pf is None:
            return {"ok": False, "message": "portfolio 未初始化"}

        trades: list[dict[str, Any]] = []
        for r in buy_results:
            if r.status != "filled":
                trades.append({
                    "ts_code": r.ts_code, "name": r.name,
                    "status": "rejected", "reason": r.reject_reason,
                    "entry_id": None,
                })
                continue
            # 找对应平仓
            closed = [p for p in pf.closed_positions if p.ts_code == r.ts_code]
            if closed:
                pos = closed[-1]
                ret_pct = round(
                    (pos.exit_price - pos.entry_price) / pos.entry_price * 100, 2,
                ) if pos.exit_price else None
                trades.append({
                    "ts_code": r.ts_code, "name": r.name,
                    "status": "closed",
                    "entry_id": r.entry_id,
                    "entry_date": str(pos.entry_date),
                    "entry_price": pos.entry_price,
                    "shares": pos.shares,
                    "cost": round(pos.cost_basis, 2),
                    "exit_date": str(pos.exit_date) if pos.exit_date else "",
                    "exit_price": pos.exit_price,
                    "exit_reason": pos.exit_reason,
                    "return_pct": ret_pct,
                    "holding_days": pos.holding_days_elapsed,
                    "fees": round(r.fees + (
                        SimBroker._calc_fees(pos.shares * (pos.exit_price or 0), side="sell")
                        if pos.exit_price else 0
                    ), 2),
                })

        # 指标
        closed_trades = [t for t in trades if t.get("status") == "closed" and t.get("return_pct") is not None]
        wins = [t for t in closed_trades if (t["return_pct"] or 0) > 0]
        losses = [t for t in closed_trades if (t["return_pct"] or 0) < 0]

        final_equity = pf.total_equity
        total_return = round((final_equity - self.initial_capital) / self.initial_capital * 100, 2)

        daily_rets = [ep.daily_return_pct for ep in pf.equity_curve if ep.daily_return_pct != 0]
        sharpe = 0.0
        if daily_rets and len(daily_rets) >= 2:
            mean_ret = statistics.mean(daily_rets)
            std_ret = statistics.stdev(daily_rets) if len(daily_rets) >= 2 else 0
            sharpe = round(mean_ret / std_ret * math.sqrt(252), 4) if std_ret > 0 else 0.0

        max_dd = 0.0
        peak = self.initial_capital
        for ep in pf.equity_curve:
            if ep.total_equity > peak:
                peak = ep.total_equity
            dd = (peak - ep.total_equity) / peak * 100
            if dd > max_dd:
                max_dd = round(dd, 2)

        gross_gain = sum(t["return_pct"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["return_pct"] for t in losses)) if losses else 0
        profit_factor = round(gross_gain / gross_loss, 2) if gross_loss > 0 else float("inf")

        metrics = {
            "initial_capital": self.initial_capital,
            "final_equity": round(final_equity, 2),
            "total_return_pct": total_return,
            "win_rate": round(len(wins) / len(closed_trades), 4) if closed_trades else 0,
            "win_count": len(wins),
            "loss_count": len(losses),
            "avg_win_pct": round(statistics.mean([t["return_pct"] for t in wins]), 2) if wins else 0,
            "avg_loss_pct": round(statistics.mean([t["return_pct"] for t in losses]), 2) if losses else 0,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": max_dd,
            "expectancy": round(
                (len(wins) / len(closed_trades) * (statistics.mean([t["return_pct"] for t in wins]) if wins else 0))
                - (len(losses) / len(closed_trades) * abs(statistics.mean([t["return_pct"] for t in losses]) if losses else 0))
                , 2,
            ) if closed_trades else 0,
        }

        return {
            "ok": True,
            "run_id": run_id,
            "label": label,
            "rule_version": rule_version,
            "as_of": as_of,
            "signals_total": signals_total,
            "filled": filled,
            "max_positions": self.max_positions,
            "metrics": metrics,
            "trades": trades,
            "equity_curve": [
                {"date": str(ep.trade_date), "equity": ep.total_equity, "daily_ret": ep.daily_return_pct}
                for ep in pf.equity_curve
            ],
        }

    def _aggregate_batch(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        ok_results = [r for r in results if r.get("ok")]
        if not ok_results:
            return {"ok": False, "message": "所有 run 均失败", "results": results}

        all_trades: list[dict[str, Any]] = []
        all_metrics: list[dict[str, Any]] = []

        for r in ok_results:
            metrics = r.get("metrics")
            if metrics:
                metrics["run_id"] = r["run_id"]
                metrics["label"] = r.get("label", "")
                metrics["rule_version"] = r.get("rule_version", "")
                metrics["as_of"] = r.get("as_of", "")
                all_metrics.append(metrics)
            all_trades.extend(r.get("trades") or [])

        closed = [t for t in all_trades if t.get("status") == "closed" and t.get("return_pct") is not None]
        wins = [t for t in closed if (t["return_pct"] or 0) > 0]

        # 聚合指标
        total_rets = [m["total_return_pct"] for m in all_metrics]
        win_rates = [m["win_rate"] for m in all_metrics]

        aggregate = {
            "total_runs": len(ok_results),
            "total_trades": len(closed),
            "avg_return_per_run_pct": round(statistics.mean(total_rets), 2) if total_rets else 0,
            "median_return_per_run_pct": round(statistics.median(total_rets), 2) if total_rets else 0,
            "avg_win_rate": round(statistics.mean(win_rates), 4) if win_rates else 0,
            "total_win_rate": round(len(wins) / len(closed), 4) if closed else 0,
            "best_run_return": round(max(total_rets), 2) if total_rets else 0,
            "worst_run_return": round(min(total_rets), 2) if total_rets else 0,
            "per_run_metrics": all_metrics,
            "all_trades": all_trades,
        }

        return {
            "ok": True,
            "aggregate": aggregate,
            "raw_results": ok_results,
        }


# ── 报告输出 ────────────────────────────────────────────────────


def format_sim_report(
    result: dict[str, Any], *,
    title: str = "",
    with_root_cause: bool = False,
) -> str:
    """格式化模拟结果为 Markdown 报告。

    Args:
        with_root_cause: 附加根因分析（亏损交易逐只归因）。
    """
    if not result.get("ok"):
        return f"# 模拟交易\n\n❌ {result.get('message', '失败')}"

    # 批量聚合模式
    agg = result.get("aggregate")
    if agg:
        md = _format_aggregate_markdown(agg, title)
        if with_root_cause:
            from backtest_agent.root_cause import RootCauseEngine, format_root_cause_report
            engine = RootCauseEngine()
            rc = engine.analyze_sim_result({"trades": agg.get("all_trades", [])})
            if rc.get("ok"):
                md += "\n\n" + format_root_cause_report(rc)
        return md

    # 单 run 模式
    md = _format_single_markdown(result, title)
    if with_root_cause:
        from backtest_agent.root_cause import RootCauseEngine, format_root_cause_report
        engine = RootCauseEngine()
        rc = engine.analyze_sim_result(result)
        if rc.get("ok"):
            md += "\n\n" + format_root_cause_report(rc)
    return md


def _format_single_markdown(result: dict[str, Any], title: str) -> str:
    metrics = result.get("metrics") or {}
    trades = result.get("trades") or []

    lines = [
        f"# {title or '模拟交易报告'}",
        "",
        f"- Run ID: **{result.get('run_id')}** | as_of: **{result.get('as_of')}**",
        f"- 信号数: {result.get('signals_total')} | 成交: **{result.get('filled')}**",
        "",
        "## 绩效指标",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 初始资金 | ¥{metrics.get('initial_capital', 0):,.0f} |",
        f"| 最终净值 | ¥{metrics.get('final_equity', 0):,.2f} |",
        f"| 总收益率 | **{metrics.get('total_return_pct', 0):+.2f}%** |",
        f"| 胜率 | {metrics.get('win_rate', 0):.0%} ({metrics.get('win_count')}/{metrics.get('win_count',0)+metrics.get('loss_count',0)}) |",
        f"| 平均盈利 | {metrics.get('avg_win_pct', 0):+.2f}% |",
        f"| 平均亏损 | {metrics.get('avg_loss_pct', 0):+.2f}% |",
        f"| Profit Factor | {metrics.get('profit_factor', 0)} |",
        f"| 夏普比率 | {metrics.get('sharpe_ratio', 0)} |",
        f"| 最大回撤 | {metrics.get('max_drawdown_pct', 0):.2f}% |",
        f"| 期望值 | {metrics.get('expectancy', 0):+.2f}% |",
        "",
        "## 交易明细",
        "",
    ]

    if trades:
        lines.append("| # | 标的 | 入场日 | 入场价 | 出场日 | 出场价 | 收益 | 原因 |")
        lines.append("|---|------|--------|--------|--------|--------|------|------|")
        for i, t in enumerate(trades):
            if t.get("status") == "rejected":
                lines.append(f"| {i+1} | {t['name']}({t['ts_code']}) | — | — | — | — | ❌ | {t.get('reason','')} |")
            else:
                ret = t.get("return_pct")
                ret_str = f"**{ret:+.2f}%**" if ret is not None else "—"
                lines.append(
                    f"| {i+1} | {t['name']}({t['ts_code']}) "
                    f"| {t.get('entry_date','')} | ¥{t.get('entry_price',0):.2f} "
                    f"| {t.get('exit_date','')} | ¥{t.get('exit_price',0) or '—'} "
                    f"| {ret_str} | {t.get('exit_reason','')} |"
                )
    else:
        lines.append("（无交易记录）")

    lines.extend(["", "---", f"*生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*"])
    return "\n".join(lines)


def _format_aggregate_markdown(agg: dict[str, Any], title: str) -> str:
    lines = [
        f"# {title or '跨期批量回放报告'}",
        "",
        f"- 回放轮次: **{agg.get('total_runs')}** | 总交易数: **{agg.get('total_trades')}**",
        "",
        "## 聚合指标",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 每轮均收益 | **{agg.get('avg_return_per_run_pct', 0):+.2f}%** |",
        f"| 每轮中位收益 | {agg.get('median_return_per_run_pct', 0):+.2f}% |",
        f"| 均胜率 | {agg.get('avg_win_rate', 0):.0%} |",
        f"| 总胜率 | {agg.get('total_win_rate', 0):.0%} |",
        f"| 最佳轮 | {agg.get('best_run_return', 0):+.2f}% |",
        f"| 最差轮 | {agg.get('worst_run_return', 0):+.2f}% |",
        "",
        "## 逐轮收益",
        "",
        "| Run | as_of | 策略版本 | Label | 收益 | 胜率 |",
        "|-----|-------|----------|-------|------|------|",
    ]

    for m in agg.get("per_run_metrics") or []:
        rv = (m.get("rule_version") or "")[:25]
        lines.append(
            f"| {m.get('run_id')} | {m.get('as_of','')} | {rv} | {m.get('label','')} "
            f"| **{m.get('total_return_pct', 0):+.2f}%** "
            f"| {m.get('win_rate', 0):.0%} |"
        )

    lines.extend(["", "---", f"*生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*"])
    return "\n".join(lines)
