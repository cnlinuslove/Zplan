"""盘中监控 — T 日交易时段内，每 30 分钟检查价格触发价位。

触发推送:
- 触及目标价 → 止盈提醒
- 触及止损价 → 止损提醒
- 回调到买入区 → 入场提醒
- 巨量异动 → 关注提醒
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from execution.plan import ExecutionPlan, load_latest_picks, load_plan_snapshot, save_plan_snapshot

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))
SNAPSHOT_DIR = "/tmp/zplan-execution"


def _snapshot_path(date_str: str) -> str:
    return f"{SNAPSHOT_DIR}/execution_{date_str}.json"


def _is_trading_hour(dt: datetime) -> bool:
    """判断是否在 A 股交易时段内（9:30-11:30, 13:00-15:00）。"""
    t = dt.time()
    morning = t >= datetime.strptime("09:30", "%H:%M").time() and t <= datetime.strptime("11:30", "%H:%M").time()
    afternoon = t >= datetime.strptime("13:00", "%H:%M").time() and t <= datetime.strptime("15:00", "%H:%M").time()
    return morning or afternoon


def _fetch_live_prices(codes: list[str]) -> dict[str, dict[str, float | None]]:
    """获取实时价格。"""
    result: dict[str, dict[str, float | None]] = {}
    try:
        import akshare as ak
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get("代码", ""))
                    if code in codes:
                        result[code] = {
                            "price": float(row.get("最新价")) if row.get("最新价") is not None else None,
                            "pct_chg": float(row.get("涨跌幅")) if row.get("涨跌幅") is not None else None,
                            "volume": float(row.get("成交量")) if row.get("成交量") is not None else None,
                            "high": float(row.get("最高")) if row.get("最高") is not None else None,
                            "low": float(row.get("最低")) if row.get("最低") is not None else None,
                        }
        except Exception:
            logger.debug("实时价格拉取失败", exc_info=True)
    except ImportError:
        logger.warning("akshare 未安装")
    except Exception:
        logger.warning("实时价格拉取失败", exc_info=True)
    return result


def _check_price_triggers(plan: ExecutionPlan, live: dict[str, float | None]) -> list[str]:
    """检查价格触发条件，返回触发的事件列表。"""
    events: list[str] = []
    price = live.get("price")
    high = live.get("high")
    low = live.get("low")
    pct = live.get("pct_chg")
    vol = live.get("volume")

    if price is None:
        return events

    name = plan.name or plan.ts_code

    # 目标价触发
    if plan.predicted_target and price >= plan.predicted_target:
        if not plan.hit_target:
            plan.hit_target = True
            events.append(f"🎯 **{name}**({plan.ts_code}) 触及目标价 ¥{plan.predicted_target:.2f}（现价 ¥{price:.2f}），建议止盈")

    # 止损触发
    if plan.predicted_stop and price <= plan.predicted_stop:
        if not plan.hit_stop:
            plan.hit_stop = True
            events.append(f"🛑 **{name}**({plan.ts_code}) 跌破止损价 ¥{plan.predicted_stop:.2f}（现价 ¥{price:.2f}），建议止损")

    # 回调到买入区（仅对观望/等待中的标的）
    buy = plan.adjusted_buy or plan.predicted_buy
    if buy and plan.open_action in ("WAIT_OBSERVE", "BUY_ON_PULLBACK", "SKIP_TODAY", ""):
        if low and low <= buy:
            events.append(f"📉 **{name}**({plan.ts_code}) 回调至 ¥{price:.2f}，接近买入区 ¥{buy:.2f}，可考虑入场")

    # 巨量异动
    if vol and pct:
        if abs(pct) > 5:
            events.append(f"⚠️ **{name}**({plan.ts_code}) 异动 {pct:+.1f}%，放量，请关注")

    return events


def run_intraday_check(
    top_n: int = 10,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """盘中检查（每 30 分钟触发一次）。"""
    beijing_now = datetime.now(BEIJING_TZ)
    today_str = beijing_now.strftime("%Y-%m-%d")
    time_str = beijing_now.strftime("%H:%M")

    if beijing_now.weekday() >= 5:
        return {"ok": True, "skipped": True, "reason": "周末跳过"}

    if not _is_trading_hour(beijing_now):
        return {"ok": True, "skipped": True, "reason": f"非交易时段 ({time_str})"}

    plans = load_latest_picks(top_n=top_n)
    if not plans:
        return {"ok": False, "error": "无选股数据"}

    # 恢复前序快照
    spath = _snapshot_path(today_str)
    load_plan_snapshot(plans, spath)

    # 拉取实时价格
    codes = [p.ts_code for p in plans]
    live_data = _fetch_live_prices(codes)

    # 检查触发
    all_events: list[str] = []
    for plan in plans:
        ld = live_data.get(plan.ts_code, {})
        plan.current_price = ld.get("price")

        events = _check_price_triggers(plan, ld)
        all_events.extend(events)
        if events:
            plan.intraday_notes.extend(events)

    save_plan_snapshot(plans, spath)

    # 只在有事件时推送
    if not all_events:
        return {"ok": True, "date": today_str, "time": time_str, "events": [], "silent": True}

    markdown = _format_intraday_markdown(all_events, today_str, time_str)
    return {"ok": True, "date": today_str, "time": time_str, "events": all_events, "markdown": markdown}


def _format_intraday_markdown(events: list[str], today_str: str, time_str: str) -> str:
    lines = [
        f"## 📡 Z-Plan 盘中信号",
        f"> {today_str} {time_str} · {len(events)} 条触发",
        "",
    ]
    for e in events:
        lines.append(f"- {e}")
    lines.append("")
    lines.append("---")
    lines.append("💡 以上为自动触发信号，请结合盘面综合判断")
    return "\n".join(lines)
