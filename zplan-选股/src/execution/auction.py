"""集合竞价监控 — T 日 9:25 触发，拉取竞价数据并对比建议买入价。

数据源优先级: 东财实时行情 > 腾讯 tick > 从分时 Parquet 推断
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from execution.plan import ExecutionPlan, load_latest_picks, load_plan_snapshot, save_plan_snapshot

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))

# 快照文件路径（跨脚本续传状态）
SNAPSHOT_DIR = "/tmp/zplan-execution"


def _snapshot_path(date_str: str) -> str:
    return f"{SNAPSHOT_DIR}/execution_{date_str}.json"


def _fetch_auction_prices(codes: list[str]) -> dict[str, dict[str, float | None]]:
    """批量拉取集合竞价/实时价格。

    Returns:
        {ts_code: {"price": float, "volume": float}}
    """
    result: dict[str, dict[str, float | None]] = {}

    try:
        import akshare as ak

        # 方法1：东财实时行情（含竞价结果）
        try:
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get("代码", ""))
                    if code in codes:
                        price = row.get("最新价")
                        vol = row.get("成交量")
                        result[code] = {
                            "price": float(price) if price is not None else None,
                            "volume": float(vol) if vol is not None else None,
                        }
        except Exception:
            logger.debug("东财实时行情拉取失败", exc_info=True)

        # 方法2：对未获取到的，用腾讯 tick 兜底
        missing = [c for c in codes if c not in result]
        if missing:
            for code in missing[:5]:  # 限制兜底调用次数
                try:
                    df_tick = ak.stock_zh_a_tick_tx(code=code, symbol="sz" if code.startswith(("0", "3")) else "sh")
                    if df_tick is not None and not df_tick.empty:
                        last = df_tick.iloc[-1]
                        result[code] = {
                            "price": float(last.get("price", last.get("成交价", 0))),
                            "volume": float(last.get("volume", last.get("成交量", 0))),
                        }
                except Exception:
                    logger.debug("tick兜底失败: %s", code, exc_info=True)

    except ImportError:
        logger.warning("akshare 未安装，无法获取竞价数据")
    except Exception:
        logger.warning("竞价数据拉取失败", exc_info=True)

    return result


def _classify_auction(plan: ExecutionPlan, auction_price: float | None) -> tuple[str, str]:
    """根据竞价价 vs 调整买入价 分类。

    Returns:
        (signal, reason)
        signal: BUY_AUCTION | WAIT_OPEN | SKIP_CHASE | CHECK_NEWS
    """
    buy = plan.adjusted_buy or plan.predicted_buy
    if buy is None or auction_price is None:
        return "NO_DATA", "无竞价/买入价数据"

    gap_pct = (auction_price - buy) / buy * 100

    if gap_pct <= 0.5:  # 竞价 ≤ 买入价+0.5%
        return "🟢 BUY_AUCTION", f"竞价¥{auction_price:.2f} ≤ 买入价¥{buy:.2f}，可挂单"
    elif gap_pct <= 1.0:
        return "🟡 WAIT_OPEN", f"竞价¥{auction_price:.2f} 略高买入价{gap_pct:+.1f}%，等开盘确认"
    elif gap_pct <= 2.0:
        return "🟠 SKIP_CHASE", f"竞价¥{auction_price:.2f} 高买入价{gap_pct:+.1f}%，不建议追"
    elif gap_pct < -2.0:
        return "🔴 CHECK_NEWS", f"竞价¥{auction_price:.2f} 低于买入价{gap_pct:+.1f}%，检查是否有利空"
    else:
        return "🔴 SKIP_CHASE", f"竞价¥{auction_price:.2f} 远高买入价{gap_pct:+.1f}%，今日放弃"


def run_auction_check(
    top_n: int = 10,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """T 日 9:25 集合竞价检查。"""
    beijing_now = datetime.now(BEIJING_TZ)
    today_str = beijing_now.strftime("%Y-%m-%d")
    weekend = beijing_now.weekday() >= 5

    if weekend:
        return {"ok": True, "skipped": True, "reason": "周末跳过"}

    # 加载 picks + 恢复盘前快照
    plans = load_latest_picks(top_n=top_n)
    if not plans:
        return {"ok": False, "error": "无选股数据"}

    spath = _snapshot_path(today_str)
    load_plan_snapshot(plans, spath)

    # 拉取竞价数据
    codes = [p.ts_code for p in plans]
    auction_data = _fetch_auction_prices(codes)

    # 逐只分析
    for plan in plans:
        ad = auction_data.get(plan.ts_code, {})
        plan.auction_price = ad.get("price")
        plan.auction_volume = ad.get("volume")
        if plan.adjusted_buy or plan.predicted_buy:
            buy = plan.adjusted_buy or plan.predicted_buy
            if plan.auction_price and buy:
                plan.auction_vs_buy_pct = round((plan.auction_price - buy) / buy * 100, 2)

    # 保存快照
    save_plan_snapshot(plans, spath)

    markdown = _format_auction_markdown(plans, today_str, len(auction_data))

    return {"ok": True, "date": today_str, "plans": plans, "markdown": markdown}


def _format_auction_markdown(
    plans: list[ExecutionPlan],
    today_str: str,
    fetched_count: int,
) -> str:
    """格式化竞价简报。"""
    beijing_now = datetime.now(BEIJING_TZ)
    time_str = beijing_now.strftime("%H:%M")

    buy_now = []
    wait = []
    skip = []
    no_data = []

    for p in plans:
        if p.auction_price is None:
            no_data.append(p)
            continue
        buy = p.adjusted_buy or p.predicted_buy or 0
        gap = (p.auction_price - buy) / buy * 100 if buy else 0
        if gap <= 0.5:
            buy_now.append(p)
        elif gap <= 1.5:
            wait.append(p)
        else:
            skip.append(p)

    lines = [
        f"## 🔔 Z-Plan 竞价快报",
        f"> {today_str} {time_str} · 已获取 {fetched_count}/{len(plans)} 只竞价数据",
        "",
    ]

    # ── 可买入 ──
    if buy_now:
        lines.append("### 🟢 可挂单买入")
        lines.append("")
        lines.append("| 标的 | 竞价价 | 建议买入 | 偏离% | 操作 |")
        lines.append("|------|--------|----------|-------|------|")
        for p in buy_now:
            buy = p.adjusted_buy or p.predicted_buy or 0
            gap = (p.auction_price - buy) / buy * 100 if buy else 0
            lines.append(
                f"| **{p.name}**({p.ts_code}) | ¥{p.auction_price:.2f} | ¥{buy:.2f} | {gap:+.1f}% | 挂单¥{buy:.2f} |"
            )
        lines.append("")

    # ── 等待开盘 ──
    if wait:
        lines.append("### 🟡 等待开盘确认")
        lines.append("")
        lines.append("| 标的 | 竞价价 | 建议买入 | 偏离% | 操作 |")
        lines.append("|------|--------|----------|-------|------|")
        for p in wait:
            buy = p.adjusted_buy or p.predicted_buy or 0
            gap = (p.auction_price - buy) / buy * 100 if buy else 0
            lines.append(
                f"| {p.name}({p.ts_code}) | ¥{p.auction_price:.2f} | ¥{buy:.2f} | {gap:+.1f}% | 等9:30开盘确认 |"
            )
        lines.append("")

    # ── 不建议追 ──
    if skip:
        lines.append("### 🔴 不建议追高")
        lines.append("")
        lines.append("| 标的 | 竞价价 | 建议买入 | 偏离% | 操作 |")
        lines.append("|------|--------|----------|-------|------|")
        for p in skip:
            buy = p.adjusted_buy or p.predicted_buy or 0
            gap = (p.auction_price - buy) / buy * 100 if buy else 0
            lines.append(
                f"| {p.name}({p.ts_code}) | ¥{p.auction_price:.2f} | ¥{buy:.2f} | {gap:+.1f}% | 今日放弃/等回调 |"
            )
        lines.append("")

    # ── 无数据 ──
    if no_data:
        names = "、".join(p.name or p.ts_code for p in no_data[:5])
        lines.append(f"> ⚪ 无竞价数据: {names}（可能停牌或数据源暂不可用）")
        lines.append("")

    lines.append("---")
    lines.append("⏰ 9:30 开盘后会有行动清单推送")

    return "\n".join(lines)
