"""每日纸质交易：盘前记录预测 → 盘中跟踪 → 盘后结算。

状态持久化为 JSON 文件，不依赖新增 DB 表。

用法::

    cd zplan-回测 && .venv/bin/python main.py paper-trade morning
    cd zplan-回测 && .venv/bin/python main.py paper-trade close
    cd zplan-回测 && .venv/bin/python main.py paper-trade status
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.market import get_bars, get_panel, latest_trade_date, resolve_ts_code
from zplan_shared.models import PickEntry, PickRun, SessionLocal, init_db
from sqlalchemy import desc, select

import pandas as pd

from backtest_agent.sim_trade import SimBroker, SimPortfolio, Position
from backtest_agent.root_cause import RootCauseEngine, format_root_cause_report
from zplan_shared.exit_strategy import (
    ExitPlan,
    ExitType,
    PositionState,
    compute_exit_signals,
)
from zplan_shared.exit_config import load_exit_config

STATE_DIR = Path(ZPLAN_ROOT).parent / "zplan-回测" / "data"
DEFAULT_STATE_FILE = STATE_DIR / "paper_state.json"

DEFAULT_CAPITAL = 100_000.0
DEFAULT_TOP_N = 5
DEFAULT_HOLDING_DAYS = 5
DEFAULT_STOP_LOSS_PCT = -0.05


# ── 状态管理 ────────────────────────────────────────────────────


def _load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_STATE_FILE
    if not p.exists():
        return _empty_state()
    return json.loads(p.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or DEFAULT_STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat()
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _empty_state() -> dict[str, Any]:
    return {
        "created_at": datetime.now().isoformat(),
        "initial_capital": DEFAULT_CAPITAL,
        "cash": DEFAULT_CAPITAL,
        "positions": {},
        "pending_orders": [],
        "trade_history": [],
        "daily_snapshots": [],
        "last_pick_run_id": None,
        "updated_at": "",
    }


# ── 盘前：生成买单 ──────────────────────────────────────────────


def morning(
    *,
    top_n: int = DEFAULT_TOP_N,
    capital: float = DEFAULT_CAPITAL,
    state_path: Path | None = None,
    run_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """加载最新选股，生成次日模拟买单。"""
    state = _load_state(state_path)

    # 如果账户刚初始化，使用指定资金
    if not state.get("daily_snapshots"):
        state["initial_capital"] = capital
        state["cash"] = capital

    init_db()
    with SessionLocal() as session:
        if run_id:
            run = session.get(PickRun, run_id)
        else:
            run = session.execute(
                select(PickRun)
                .where(PickRun.run_kind.in_(["llm_top300", "scan"]), PickRun.llm_enabled.is_(True))
                .order_by(desc(PickRun.id))
                .limit(1)
            ).scalar_one_or_none()

        if not run:
            return {"ok": False, "message": "无 LLM 选股运行"}

        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run.id)
            .order_by(PickEntry.rank_in_run, PickEntry.id)
            .limit(top_n)
        ).scalars().all()

    as_of = run.trade_date_as_of
    as_of_str = str(as_of) if as_of else "未知"

    # 生成信号
    signals: list[dict[str, Any]] = []
    for e in entries:
        entry_price = e.close_price or 0
        stop_loss = round(entry_price * (1.0 + DEFAULT_STOP_LOSS_PCT), 2) if entry_price > 0 else None
        signals.append({
            "ts_code": e.ts_code,
            "name": e.name or e.ts_code,
            "entry_id": e.id,
            "predicted_buy": e.predicted_buy_price,
            "predicted_target": e.predicted_target_price,
            "predicted_stop": e.predicted_stop_loss,
            "close_price": entry_price,
            "stop_loss": stop_loss,
            "llm_score": e.llm_composite_score,
            "rank": e.rank_in_run,
            "recommendation": e.recommendation,
        })

    # 计算等权分配
    per_stock = state["cash"] / min(top_n, len(signals)) if signals else 0

    orders: list[dict[str, Any]] = []
    for sig in signals:
        raw_shares = int(per_stock / sig["close_price"] / 100) * 100 if sig["close_price"] > 0 else 0
        orders.append({
            "ts_code": sig["ts_code"],
            "name": sig["name"],
            "entry_id": sig["entry_id"],
            "planned_price": sig["close_price"],
            "planned_shares": raw_shares,
            "predicted_buy": sig["predicted_buy"],
            "stop_loss": sig["stop_loss"],
            "predicted_target": sig["predicted_target"],
            "rank": sig["rank"],
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        })

    state["pending_orders"] = orders
    state["last_pick_run_id"] = run.id
    state["last_as_of"] = as_of_str
    state["last_rule_version"] = run.rule_version

    if not dry_run:
        _save_state(state, state_path)

    return {
        "ok": True,
        "run_id": run.id,
        "as_of": as_of_str,
        "rule_version": run.rule_version,
        "orders": orders,
        "cash_available": state["cash"],
        "per_stock_budget": round(per_stock, 2),
        "total_planned": round(sum(o["planned_price"] * o["planned_shares"] for o in orders), 2),
        "dry_run": dry_run,
        "position_count": len(state.get("positions", {})),
    }


# ── 盘后：结算 ──────────────────────────────────────────────────


def close(
    *,
    state_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """检查当日行情，执行买入 + 检查出场 + 更新持仓。"""
    state = _load_state(state_path)

    init_db()
    broker = SimBroker()
    today = latest_trade_date()

    if today is None:
        return {"ok": False, "message": "今日无行情数据，无法结算"}

    today_str = str(today)
    executed: list[dict[str, Any]] = []
    exited: list[dict[str, Any]] = []
    errors: list[str] = []

    # ── 1. 执行 pending 买单 ──
    pending = state.get("pending_orders", [])
    remaining: list[dict[str, Any]] = []

    for order in pending:
        # 取该票今日 bar
        bars = get_bars(order["ts_code"])
        if bars.empty:
            remaining.append(order)
            continue

        idx = pd_datetime_index(bars)
        bars.index = idx
        today_bars = bars[bars.index == pd.Timestamp(today)]
        if today_bars.empty:
            remaining.append(order)
            continue

        bar = today_bars.iloc[0]
        open_price = float(bar["open"])
        day_low = float(bar["low"])
        day_high = float(bar["high"])
        day_close = float(bar["close"])
        day_vol = float(bar.get("volume", 0))

        # 一字涨停检查
        prev_close = _prev_close_from_bars(bars, today)
        limit_up = round(prev_close * 1.10, 2) if prev_close else None
        if limit_up and _is_limit_locked(open_price, day_high, day_low, day_close, limit_up, day_vol):
            errors.append(f"{order['name']}({order['ts_code']}) 一字涨停，买单搁置")
            order["status"] = "skipped_limit_up"
            remaining.append(order)
            continue

        # 限价检查
        entry_price = open_price
        predicted = order.get("predicted_buy")
        if predicted and entry_price > predicted:
            errors.append(
                f"{order['name']}({order['ts_code']}) 开盘 ¥{entry_price:.2f} > 限价 ¥{predicted:.2f}，未成交"
            )
            order["status"] = "rejected_price"
            order["filled_price"] = None
            executed.append(order)
            continue

        # 整手计算
        budget = state["cash"] / max(1, len(pending))
        raw_shares = int(budget / entry_price / 100) * 100
        if raw_shares < 100:
            errors.append(f"{order['name']}({order['ts_code']}) 资金不足")
            order["status"] = "rejected_cash"
            order["filled_price"] = None
            executed.append(order)
            continue

        filled_value = raw_shares * entry_price
        fees = SimBroker._calc_fees(filled_value, side="buy")
        total_cost = filled_value + fees
        if total_cost > state["cash"]:
            raw_shares = int((state["cash"] - fees) / entry_price / 100) * 100
            if raw_shares < 100:
                errors.append(f"{order['name']}({order['ts_code']}) 含费后资金不足")
                order["status"] = "rejected_cash"
                order["filled_price"] = None
                executed.append(order)
                continue
            filled_value = raw_shares * entry_price
            fees = SimBroker._calc_fees(filled_value, side="buy")

        # 成交 — 按实际成交价重算止损止盈
        actual_stop = round(entry_price * (1.0 + DEFAULT_STOP_LOSS_PCT), 2) if entry_price > 0 else None
        actual_target = round(entry_price * 1.15, 2) if entry_price > 0 and not order.get("predicted_target") else order.get("predicted_target")

        # ── 加载出场方案（逐票 > 策略默认）──
        exit_plan_dict = None
        if order.get("exit_plan_json"):
            try:
                exit_plan_dict = json.loads(order["exit_plan_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        if not exit_plan_dict and order.get("entry_id"):
            # 尝试从 PickEntry 加载 exit_plan_json
            try:
                with SessionLocal() as sess:
                    entry = sess.get(PickEntry, order["entry_id"])
                    if entry and entry.exit_plan_json:
                        exit_plan_dict = json.loads(entry.exit_plan_json)
            except Exception:
                pass
        # 如果都没有，尝试从 strategy.yaml 加载默认方案
        if not exit_plan_dict:
            try:
                ec = load_exit_config()
                default_plan = ec.get_default_plan()
                if default_plan.plan_key != "static":
                    exit_plan_dict = default_plan.to_dict()
            except Exception:
                pass

        state["cash"] -= (filled_value + fees)

        pos_key = order["ts_code"]
        state["positions"][pos_key] = {
            "ts_code": order["ts_code"],
            "name": order["name"],
            "entry_date": today_str,
            "entry_price": entry_price,
            "shares": raw_shares,
            "cost_basis": round(filled_value + fees, 2),
            "stop_loss": actual_stop,
            "take_profit": actual_target,
            "holding_days_max": DEFAULT_HOLDING_DAYS,
            "holding_days_elapsed": 0,
            "exit_plan_json": exit_plan_dict,
            "highest_close_since_entry": entry_price,
        }

        order["status"] = "filled"
        order["filled_price"] = entry_price
        order["filled_shares"] = raw_shares
        order["filled_value"] = round(filled_value, 2)
        order["fees"] = round(fees, 2)
        executed.append(order)

    state["pending_orders"] = remaining

    # ── 2. 检查已有持仓出场 ──
    for ts_code in list(state["positions"].keys()):
        pos = state["positions"][ts_code]
        # 推进持仓天数
        pos["holding_days_elapsed"] += 1

        bars = get_bars(ts_code)
        if bars.empty:
            continue

        idx = pd_datetime_index(bars)
        bars.index = idx
        today_bars = bars[bars.index == pd.Timestamp(today)]
        if today_bars.empty:
            continue

        bar = today_bars.iloc[0]
        day_low = float(bar["low"])
        day_high = float(bar["high"])
        day_close = float(bar["close"])

        # 一字跌停检查
        prev_close = _prev_close_from_bars(bars, today)
        limit_down = round(prev_close * 0.90, 2) if prev_close else None
        if limit_down and _is_limit_locked(
            float(bar["open"]), day_high, day_low, day_close, limit_down, float(bar.get("volume", 0)),
        ):
            continue  # 跌停锁死，卖不掉

        exit_price = None
        exit_reason = ""

        # ── 新引擎：ExitPlan 优先 ──
        plan_dict = pos.get("exit_plan_json")
        if plan_dict:
            try:
                exit_plan = ExitPlan.from_dict(plan_dict)
                pos_state = PositionState(
                    ts_code=ts_code,
                    entry_price=pos["entry_price"],
                    entry_date=date.fromisoformat(pos["entry_date"]),
                    shares=pos["shares"],
                    cost_basis=pos["cost_basis"],
                    holding_days_elapsed=pos["holding_days_elapsed"],
                    highest_close_since_entry=pos.get("highest_close_since_entry"),
                )
                signals = compute_exit_signals(pos_state, exit_plan, bars, today)
                if signals:
                    sig = signals[0]
                    exit_price = sig.exit_price
                    exit_reason = sig.reason
                    # 分批止盈：仅部分仓位
                    if sig.sell_ratio < 1.0 and sig.rule_type == ExitType.PARTIAL_TAKE_PROFIT:
                        sell_shares = int(pos["shares"] * sig.sell_ratio / 100) * 100
                        if sell_shares > 0 and sell_shares < pos["shares"]:
                            pos["shares"] -= sell_shares
                            pos["cost_basis"] = round(
                                pos["cost_basis"] * (1.0 - sig.sell_ratio), 2
                            )
                            # 部分平仓，记录但不删除持仓
                            partial_exit_value = sell_shares * exit_price
                            partial_fees = SimBroker._calc_fees(partial_exit_value, side="sell")
                            state["cash"] += (partial_exit_value - partial_fees)
                            exit_price = None  # 不完整清仓
                            exit_reason = ""
                # 更新最高收盘价追踪
                idx = pd_datetime_index(bars)
                bars_idx = bars.copy()
                bars_idx.index = idx
                entry_ts = pd.Timestamp(date.fromisoformat(pos["entry_date"]))
                trade_ts = pd.Timestamp(today)
                recent = bars_idx[(bars_idx.index > entry_ts) & (bars_idx.index <= trade_ts)]
                if not recent.empty:
                    pos["highest_close_since_entry"] = max(
                        pos.get("highest_close_since_entry") or 0,
                        float(recent["close"].max()),
                    )
            except Exception:
                pass  # 解析失败，退回到旧逻辑

        # ── 旧引擎：三参数逻辑（向后兼容）──
        if exit_price is None:
            # 止损优先
            if pos.get("stop_loss") and day_low <= pos["stop_loss"]:
                exit_price = pos["stop_loss"]
                exit_reason = "stop_loss"
            # 止盈
            elif pos.get("take_profit") and day_high >= pos["take_profit"]:
                exit_price = pos["take_profit"]
                exit_reason = "take_profit"
            # 到期
            elif pos["holding_days_elapsed"] >= pos["holding_days_max"]:
                exit_price = day_close
                exit_reason = "expired"

        if exit_price is not None:
            exit_value = pos["shares"] * exit_price
            fees = SimBroker._calc_fees(exit_value, side="sell")
            proceeds = exit_value - fees
            realized_pnl = proceeds - pos["cost_basis"]
            ret_pct = round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)

            pos["exit_date"] = today_str
            pos["exit_price"] = exit_price
            pos["exit_reason"] = exit_reason
            pos["realized_pnl"] = round(realized_pnl, 2)
            pos["return_pct"] = ret_pct

            state["cash"] += proceeds
            state.setdefault("trade_history", []).append(pos)
            exited.append(pos)
            del state["positions"][ts_code]

    # ── 3. 记录每日快照 ──
    pos_value = _calc_position_value(state["positions"])
    total_equity = state["cash"] + pos_value

    prev_snapshots = state.get("daily_snapshots", [])
    prev_total = prev_snapshots[-1]["total_equity"] if prev_snapshots else state["initial_capital"]
    daily_ret = round((total_equity - prev_total) / prev_total * 100, 6) if prev_total > 0 else 0

    snapshot = {
        "date": today_str,
        "cash": round(state["cash"], 2),
        "position_value": round(pos_value, 2),
        "total_equity": round(total_equity, 2),
        "daily_return_pct": daily_ret,
    }
    state.setdefault("daily_snapshots", []).append(snapshot)

    # ── 4. 根因分析（亏损交易） ──
    root_cause_report: dict[str, Any] | None = None
    loss_trades = [t for t in executed + exited
                   if t.get("return_pct") is not None and t["return_pct"] < 0]
    if loss_trades:
        engine = RootCauseEngine()
        rc_input = {"trades": [
            {"ts_code": t["ts_code"], "name": t.get("name", ""),
             "status": "closed", "entry_id": t.get("entry_id"),
             "entry_date": str(today), "exit_date": str(today),
             "entry_price": t.get("entry_price") or t.get("filled_price") or 0,
             "exit_price": t.get("exit_price") or 0,
             "return_pct": t.get("return_pct")}
            for t in loss_trades
        ]}
        root_cause_report = engine.analyze_sim_result(rc_input)

    if not dry_run:
        _save_state(state, state_path)

    return {
        "ok": True,
        "date": today_str,
        "orders_executed": len([o for o in executed if o.get("status") == "filled"]),
        "orders_rejected": len([o for o in executed if o.get("status") != "filled"]),
        "orders_pending": len(remaining),
        "positions_closed": len(exited),
        "positions_open": len(state["positions"]),
        "executed": executed,
        "exited": exited,
        "errors": errors,
        "cash": round(state["cash"], 2),
        "position_value": round(pos_value, 2),
        "total_equity": round(total_equity, 2),
        "daily_return_pct": daily_ret,
        "total_return_pct": round((total_equity - state["initial_capital"]) / state["initial_capital"] * 100, 2),
        "root_cause": root_cause_report,
        "dry_run": dry_run,
    }


# ── 账户状态 ────────────────────────────────────────────────────


def status(state_path: Path | None = None) -> dict[str, Any]:
    """当前账户状态。"""
    state = _load_state(state_path)

    positions = state.get("positions", {})
    pos_value = _calc_position_value(positions)
    total_equity = state["cash"] + pos_value
    total_return = round((total_equity - state["initial_capital"]) / state["initial_capital"] * 100, 2)

    # 为每个持仓计算未实现盈亏
    positions_detail: list[dict[str, Any]] = []
    for ts_code, pos in positions.items():
        bars = get_bars(ts_code)
        current_price = pos["entry_price"]  # fallback
        if not bars.empty:
            current_price = float(bars["close"].iloc[-1])
        unrealized = round((current_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)
        positions_detail.append({
            **pos,
            "current_price": current_price,
            "unrealized_pnl_pct": unrealized,
            "market_value": round(pos["shares"] * current_price, 2),
        })

    # 最近交易
    history = state.get("trade_history", [])[-5:]

    # 累计统计
    all_trades = state.get("trade_history", [])
    wins = [t for t in all_trades if t.get("return_pct", 0) > 0]
    losses = [t for t in all_trades if t.get("return_pct", 0) < 0]

    return {
        "ok": True,
        "created_at": state.get("created_at", ""),
        "last_pick_run_id": state.get("last_pick_run_id"),
        "last_as_of": state.get("last_as_of"),
        "last_rule_version": state.get("last_rule_version"),
        "initial_capital": state["initial_capital"],
        "cash": round(state["cash"], 2),
        "position_value": round(pos_value, 2),
        "total_equity": round(total_equity, 2),
        "total_return_pct": total_return,
        "positions_open": len(positions),
        "positions": positions_detail,
        "pending_orders": len(state.get("pending_orders", [])),
        "total_trades": len(all_trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(all_trades), 4) if all_trades else 0,
        "recent_trades": history,
        "daily_snapshots": state.get("daily_snapshots", [])[-10:],
    }


# ── 格式化 ──────────────────────────────────────────────────────


def format_morning_report(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"# 模拟盘前\n\n❌ {result.get('message')}"

    orders = result.get("orders") or []
    lines = [
        "# 📈 模拟盘前 · 今日计划",
        "",
        f"- Run: **{result.get('run_id')}** | 数据日: **{result.get('as_of')}**",
        f"- 策略: `{result.get('rule_version', '')}`",
        f"- 可用资金: ¥{result.get('cash_available', 0):,.2f}",
        f"- 每只预算: ¥{result.get('per_stock_budget', 0):,.2f}",
        f"- 当前持仓: {result.get('position_count', 0)} 只",
        "",
        "| # | 标的 | 计划价 | 限价 | 股数 | 预计投入 |",
        "|---|------|--------|------|------|----------|",
    ]
    for i, o in enumerate(orders):
        cost = o["planned_price"] * o["planned_shares"]
        limit = f"¥{o['predicted_buy']:.2f}" if o.get("predicted_buy") else "市价"
        lines.append(
            f"| {i+1} | {o['name']}({o['ts_code']}) | ¥{o['planned_price']:.2f} "
            f"| {limit} | {o['planned_shares']} | ¥{cost:,.0f} |"
        )

    lines.extend([
        "",
        f"💰 计划投入: ¥{result.get('total_planned', 0):,.0f}",
        f"{'⚠️ 模拟模式（未实际执行）' if result.get('dry_run') else '✅ 已记录'}",
    ])
    return "\n".join(lines)


def format_close_report(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"# 模拟收盘\n\n❌ {result.get('message')}"

    lines = [
        f"# 🌅 模拟收盘 · {result.get('date')}",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 现金 | ¥{result.get('cash', 0):,.2f} |",
        f"| 持仓市值 | ¥{result.get('position_value', 0):,.2f} |",
        f"| 总权益 | ¥{result.get('total_equity', 0):,.2f} |",
        f"| 日收益 | {result.get('daily_return_pct', 0):+.4f}% |",
        f"| 累计收益 | **{result.get('total_return_pct', 0):+.2f}%** |",
        "",
        f"- 买入成交: **{result.get('orders_executed')}** | 拒绝: {result.get('orders_rejected')} | 待执行: {result.get('orders_pending')}",
        f"- 持仓平仓: {result.get('positions_closed')} | 当前持仓: **{result.get('positions_open')}**",
        "",
    ]

    errors = result.get("errors") or []
    if errors:
        lines.append("### ⚠️ 异常")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    executed = result.get("executed") or []
    if executed:
        lines.append("### 今日成交")
        lines.append("| # | 标的 | 成交价 | 股数 | 金额 | 状态 |")
        lines.append("|---|------|--------|------|------|------|")
        for i, o in enumerate(executed):
            status_icon = "✅" if o.get("status") == "filled" else "❌"
            lines.append(
                f"| {i+1} | {o['name']}({o['ts_code']}) "
                f"| ¥{o.get('filled_price', 0) or 0:.2f} "
                f"| {o.get('filled_shares', 0)} "
                f"| ¥{o.get('filled_value', 0) or 0:,.0f} "
                f"| {status_icon} {o.get('status','')} |"
            )

    exited = result.get("exited") or []
    if exited:
        lines.append("")
        lines.append("### 今日平仓")
        lines.append("| 标的 | 入场价 | 出场价 | 收益 | 原因 |")
        lines.append("|------|--------|--------|------|------|")
        for p in exited:
            lines.append(
                f"| {p.get('name')}({p.get('ts_code')}) "
                f"| ¥{p.get('entry_price', 0):.2f} "
                f"| ¥{p.get('exit_price', 0):.2f} "
                f"| **{p.get('return_pct', 0):+.2f}%** "
                f"| {p.get('exit_reason', '')} |"
            )

    # 根因分析
    rc = result.get("root_cause")
    if rc and rc.get("ok") and rc.get("loss_trades", 0) > 0:
        lines.append("")
        lines.append(format_root_cause_report(rc))

    return "\n".join(lines)


def format_status_report(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"# 模拟账户\n\n❌ {result.get('message')}"

    lines = [
        "# 💼 模拟账户",
        "",
        f"创建于: {result.get('created_at', '')[:10]}",
        f"最近选股: run_id={result.get('last_pick_run_id')} ({result.get('last_as_of')})",
        f"策略: `{result.get('last_rule_version', '')}`",
        "",
        "## 资产",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 初始资金 | ¥{result.get('initial_capital', 0):,.0f} |",
        f"| 现金 | ¥{result.get('cash', 0):,.2f} |",
        f"| 持仓市值 | ¥{result.get('position_value', 0):,.2f} |",
        f"| 总权益 | ¥{result.get('total_equity', 0):,.2f} |",
        f"| 累计收益 | **{result.get('total_return_pct', 0):+.2f}%** |",
        "",
        "## 交易统计",
        "",
        f"| 总交易 | 胜 | 负 | 胜率 |",
        f"|--------|-----|-----|------|",
        f"| {result.get('total_trades', 0)} | {result.get('win_count', 0)} | {result.get('loss_count', 0)} | {result.get('win_rate', 0):.0%} |",
        "",
    ]

    positions = result.get("positions") or []
    if positions:
        lines.append("## 当前持仓")
        lines.append("")
        lines.append("| 标的 | 入场日 | 入场价 | 现价 | 浮盈 | 市值 | 持有天数 |")
        lines.append("|------|--------|--------|------|------|------|----------|")
        for p in positions:
            lines.append(
                f"| {p['name']}({p['ts_code']}) | {p.get('entry_date','')} "
                f"| ¥{p['entry_price']:.2f} | ¥{p.get('current_price', 0):.2f} "
                f"| **{p.get('unrealized_pnl_pct', 0):+.2f}%** "
                f"| ¥{p.get('market_value', 0):,.0f} "
                f"| {p.get('holding_days_elapsed', 0)}/{p.get('holding_days_max', 5)} |"
            )
        lines.append("")

    pending = result.get("pending_orders", 0)
    if pending:
        lines.append(f"⏳ 待执行买单: {pending} 笔")
        lines.append("")

    recent = result.get("recent_trades") or []
    if recent:
        lines.append("## 最近交易")
        lines.append("| 标的 | 入场 | 出场 | 收益 | 原因 |")
        lines.append("|------|------|------|------|------|")
        for t in reversed(recent):
            lines.append(
                f"| {t.get('name')}({t.get('ts_code')}) "
                f"| {t.get('entry_date','')} "
                f"| {t.get('exit_date','')} "
                f"| **{t.get('return_pct', 0):+.2f}%** "
                f"| {t.get('exit_reason','')} |"
            )

    # 净值快照
    snaps = result.get("daily_snapshots") or []
    if len(snaps) >= 2:
        lines.append("")
        lines.append("## 净值曲线")
        lines.append("| 日期 | 权益 | 日收益 |")
        lines.append("|------|------|--------|")
        for s in snaps:
            lines.append(
                f"| {s['date']} | ¥{s['total_equity']:,.2f} | {s.get('daily_return_pct', 0):+.4f}% |"
            )

    return "\n".join(lines)


# ── 辅助 ────────────────────────────────────────────────────────


def _calc_position_value(positions: dict[str, dict]) -> float:
    total = 0.0
    for pos in positions.values():
        bars = get_bars(pos["ts_code"])
        if bars.empty:
            total += pos["cost_basis"]
            continue
        current_price = float(bars["close"].iloc[-1])
        total += pos["shares"] * current_price
    return round(total, 2)


def _prev_close_from_bars(bars, before_date: date) -> float | None:
    if bars.empty:
        return None
    idx = pd_datetime_index(bars)
    bars_copy = bars.copy()
    bars_copy.index = idx
    prev = bars_copy[bars_copy.index < pd.Timestamp(before_date)]
    if prev.empty:
        return None
    return float(prev["close"].iloc[-1])


def _is_limit_locked(open_, high, low, close, limit_price, volume) -> bool:
    return (
        open_ == high == low == close == limit_price
        and volume < 100
    )


def pd_datetime_index(bars) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
