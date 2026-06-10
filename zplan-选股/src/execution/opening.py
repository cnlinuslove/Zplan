"""开盘决策引擎 — T 日 9:30-9:35 触发，对比开盘价 vs 竞价/预测，给出最终操作指令。"""
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


def _fetch_open_prices(codes: list[str]) -> dict[str, dict[str, float | None]]:
    """获取开盘价（9:30 后可用）。"""
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
                            "open": float(row.get("今开")) if row.get("今开") is not None else None,
                            "high": float(row.get("最高")) if row.get("最高") is not None else None,
                            "low": float(row.get("最低")) if row.get("最低") is not None else None,
                            "pct_chg": float(row.get("涨跌幅")) if row.get("涨跌幅") is not None else None,
                            "volume": float(row.get("成交量")) if row.get("成交量") is not None else None,
                        }
        except Exception:
            logger.debug("东财实时行情拉取失败", exc_info=True)
    except ImportError:
        logger.warning("akshare 未安装")
    except Exception:
        logger.warning("开盘价拉取失败", exc_info=True)

    return result


def _decide_open_action(plan: ExecutionPlan, open_data: dict[str, float | None]) -> tuple[str, str]:
    """根据开盘价 + 竞价快照，决定开盘操作。

    Returns:
        (action, reason)
        action: BUY_AT_OPEN | BUY_ON_PULLBACK | WAIT_OBSERVE | SKIP_TODAY | EXIT_SIGNAL
    """
    open_price = open_data.get("price") or open_data.get("open")
    auction_price = plan.auction_price
    buy = plan.adjusted_buy or plan.predicted_buy
    pct_chg = open_data.get("pct_chg")

    if buy is None or open_price is None:
        return "WAIT_OBSERVE", "开盘价或买入价缺失，观望"

    # 开盘方向（竞价 → 开盘）
    if auction_price and open_price:
        open_direction = "高开" if open_price > auction_price else ("低开" if open_price < auction_price else "平开")
    else:
        open_direction = ""

    gap_pct = (open_price - buy) / buy * 100

    # 止损触发检查
    if plan.predicted_stop and open_price < plan.predicted_stop:
        return "EXIT_SIGNAL", f"开盘价¥{open_price:.2f} 跌破止损¥{plan.predicted_stop:.2f}，建议止损"

    # 决策树
    if gap_pct <= 0.3:
        # 开盘价接近或低于买入价 → 直接买
        direction_note = f"，{open_direction}" if open_direction else ""
        return (
            "BUY_AT_OPEN",
            f"开盘¥{open_price:.2f} ≤ 买入价¥{buy:.2f}{direction_note}，可现价买入",
        )
    elif gap_pct <= 1.5:
        # 开盘微涨 → 挂回调单
        return (
            "BUY_ON_PULLBACK",
            f"开盘¥{open_price:.2f} 略高买入价{gap_pct:+.1f}%，挂单¥{buy:.2f}等回调",
        )
    elif gap_pct <= 3.0:
        # 开盘涨较多 → 观望或放弃
        if pct_chg is not None and pct_chg > 2.0:
            return (
                "SKIP_TODAY",
                f"开盘已涨{gap_pct:+.1f}%（日涨幅{pct_chg:+.1f}%），今日放弃追高",
            )
        return (
            "WAIT_OBSERVE",
            f"开盘涨{gap_pct:+.1f}%，等待10:00后回调确认",
        )
    else:
        # 开盘大涨 >3% → 放弃
        return (
            "SKIP_TODAY",
            f"开盘大涨{gap_pct:+.1f}%，坚决不追",
        )


def run_opening_guidance(
    top_n: int = 10,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """T 日 9:30 开盘决策。"""
    beijing_now = datetime.now(BEIJING_TZ)
    today_str = beijing_now.strftime("%Y-%m-%d")

    if beijing_now.weekday() >= 5:
        return {"ok": True, "skipped": True, "reason": "周末跳过"}

    plans = load_latest_picks(top_n=top_n)
    if not plans:
        return {"ok": False, "error": "无选股数据"}

    # 恢复前序快照（盘前 + 竞价）
    spath = _snapshot_path(today_str)
    load_plan_snapshot(plans, spath)

    # 拉取开盘价
    codes = [p.ts_code for p in plans]
    open_data = _fetch_open_prices(codes)

    # 逐只决策
    for plan in plans:
        od = open_data.get(plan.ts_code, {})
        plan.open_price = od.get("price") or od.get("open")

        action, reason = _decide_open_action(plan, od)
        plan.open_action = action
        plan.open_reason = reason

    save_plan_snapshot(plans, spath)

    markdown = _format_opening_markdown(plans, today_str)

    return {"ok": True, "date": today_str, "plans": plans, "markdown": markdown}


def _format_opening_markdown(plans: list[ExecutionPlan], today_str: str) -> str:
    """格式化开盘行动清单。"""
    beijing_now = datetime.now(BEIJING_TZ)
    time_str = beijing_now.strftime("%H:%M")

    buy_list = []
    pullback_list = []
    wait_list = []
    skip_list = []
    exit_list = []

    for p in plans:
        action = p.open_action
        if action == "BUY_AT_OPEN":
            buy_list.append(p)
        elif action == "BUY_ON_PULLBACK":
            pullback_list.append(p)
        elif action == "EXIT_SIGNAL":
            exit_list.append(p)
        elif action == "SKIP_TODAY":
            skip_list.append(p)
        else:
            wait_list.append(p)

    lines = [
        f"## 🎯 Z-Plan 开盘行动清单",
        f"> {today_str} {time_str}",
        "",
    ]

    # ── 立即买入 ──
    if buy_list:
        lines.append("### 🟢 可买入")
        lines.append("")
        for p in buy_list:
            buy = p.adjusted_buy or p.predicted_buy or 0
            lines.append(f"**{p.name}**({p.ts_code}) #{p.rank}")
            lines.append(f"> 开盘 ¥{p.open_price:.2f} · 建议买入 ¥{buy:.2f} · {p.recommendation or ''}")
            lines.append(f"> 💰 **操作：限价 ¥{buy:.2f} 买入**")
            if p.predicted_target:
                lines.append(f"> 🎯 目标 ¥{p.predicted_target:.2f} | 🛑 止损 ¥{p.predicted_stop:.2f}")
            lines.append("")
        lines.append("")

    # ── 等回调 ──
    if pullback_list:
        lines.append("### 🟡 挂单等回调")
        lines.append("")
        for p in pullback_list:
            buy = p.adjusted_buy or p.predicted_buy or 0
            lines.append(f"**{p.name}**({p.ts_code}) #{p.rank}")
            lines.append(f"> 开盘 ¥{p.open_price:.2f} · **挂单价 ¥{buy:.2f}**")
            lines.append(f"> {p.open_reason}")
            lines.append("")
        lines.append("")

    # ── 观望 ──
    if wait_list:
        lines.append("### ⏸️ 观望")
        lines.append("")
        for p in wait_list:
            lines.append(f"**{p.name}**({p.ts_code}) #{p.rank} — {p.open_reason}")
        lines.append("")

    # ── 止损 ──
    if exit_list:
        lines.append("### 🚨 止损信号")
        lines.append("")
        for p in exit_list:
            lines.append(f"**{p.name}**({p.ts_code}) #{p.rank} — {p.open_reason}")
        lines.append("")

    # ── 放弃 ──
    if skip_list:
        lines.append("### 🚫 今日放弃")
        lines.append("")
        for p in skip_list:
            lines.append(f"**{p.name}**({p.ts_code}) #{p.rank} — {p.open_reason}")
        lines.append("")

    lines.append("---")
    lines.append("📌 下一检查点: 10:00 早盘量价确认")
    lines.append("📌 盘中触及目标/止损价会自动推送提醒")

    return "\n".join(lines)
