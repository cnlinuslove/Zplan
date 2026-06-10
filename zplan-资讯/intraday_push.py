#!/usr/bin/env python3
"""主动异动推送：盘前集合竞价对比 + (未来)盘中波动监控。

用法::

    python intraday_push.py pre-market    # 9:25 盘前推送（一次性）
    python intraday_push.py monitor       # 盘中监控守护进程（待实现）

依赖：需在 ``zplan-资讯/.env`` 中配置 ``WECHAT_PUSH_WEBHOOK``。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

# 将 zplan-资讯 加入 sys.path（兼容直接 python intraday_push.py 执行）
from pathlib import Path
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from zplan_shared.market import get_realtime_quotes_batch, _is_trading_time
from zplan_shared.models import init_db
from zplan_shared.pick_store import get_run, list_runs

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# ── 盘前推送 ────────────────────────────────────────────────


def _latest_recommended_stocks() -> list[dict[str, Any]]:
    """获取最新一期 llm_top300 中推荐关注/强烈关注的股票列表。"""
    runs = list_runs(limit=30, run_kind="llm_top300")
    # 找最新一条 llm_enabled 且有数据的 run
    target_run_id: int | None = None
    for r in runs:
        if r.get("llm_enabled"):
            target_run_id = r["run_id"]
            break

    if target_run_id is None:
        logger.warning("未找到 llm_top300 且 llm_enabled 的运行记录")
        return []

    data = get_run(target_run_id)
    if not data:
        logger.warning("get_run(%s) 返回空", target_run_id)
        return []

    entries = data.get("entries") or []
    # 筛选推荐/强烈关注 + 有建议买入价的票
    recommended: list[dict[str, Any]] = []
    for e in entries:
        rec = e.get("recommendation") or ""
        buy_price = e.get("predicted_buy_price")
        if rec in ("强烈关注", "关注") and buy_price is not None and buy_price > 0:
            recommended.append(e)

    logger.info(
        "run_id=%s as_of=%s 推荐股 %s 只（筛选后 %s 只）",
        target_run_id,
        data["run"].get("trade_date_as_of"),
        len(entries),
        len(recommended),
    )
    return recommended


def _judge_open_status(
    predicted_buy: float,
    open_price: float,
    pre_close: float | None = None,
) -> tuple[str, str]:
    """对比开盘价与建议买入价 → (状态标签, emoji)。

    Returns:
        (状态描述, 图标字符)
    """
    if predicted_buy <= 0:
        return ("无建议买入价", "⚪")

    diff_pct = (open_price - predicted_buy) / predicted_buy * 100

    if abs(diff_pct) <= 2:
        return (f"符合预期（开盘 ¥{open_price:.2f}，距建议买入价 {diff_pct:+.1f}%），建议分批买入准备", "✅")
    elif diff_pct < -2:
        return (f"低开 {abs(diff_pct):.1f}%，开盘 ¥{open_price:.2f} 低于建议买入价 ¥{predicted_buy:.2f}，可关注低吸机会", "🟢")
    elif diff_pct <= 5:
        return (f"高开 {diff_pct:.1f}%，开盘 ¥{open_price:.2f}，追高需谨慎", "⚠️")
    else:
        return (f"大幅高开 {diff_pct:.1f}%，开盘 ¥{open_price:.2f}，不建议追高", "🔴")


def run_pre_market_push() -> dict[str, Any]:
    """盘前推送主逻辑。

    1. 读取最新推荐股列表
    2. 获取实时开盘价
    3. 对比建议买入价 → 生成推送消息
    """
    init_db()

    stocks = _latest_recommended_stocks()
    if not stocks:
        msg = "📊 今日盘前：无推荐关注标的，跳过推送。"
        _push(msg)
        return {"ok": True, "count": 0, "message": "无推荐标的"}

    codes = [s["ts_code"] for s in stocks]
    quotes = get_realtime_quotes_batch(codes)

    if not quotes:
        # 非交易时段或数据获取失败
        now_str = datetime.now(_CST).strftime("%H:%M")
        logger.info("非交易时段（%s）或无实时数据，跳过推送", now_str)
        return {"ok": True, "count": 0, "message": f"非交易时段（{now_str}），跳过推送"}

    # 按状态分组
    ok_list: list[dict[str, Any]] = []
    low_list: list[dict[str, Any]] = []
    high_list: list[dict[str, Any]] = []
    other_list: list[dict[str, Any]] = []

    for s in stocks:
        code = s["ts_code"]
        name = s.get("name") or code
        q = quotes.get(code)
        if not q or q.get("open") is None:
            continue

        buy = s.get("predicted_buy_price") or 0
        open_price = float(q["open"])
        pre_close = q.get("pre_close")
        pct = q.get("pct_chg") or 0

        entry = {
            "code": code,
            "name": name,
            "open": open_price,
            "pre_close": pre_close,
            "pct_chg": pct,
            "buy": buy,
            "rec": s.get("recommendation") or "",
        }

        diff_pct = (open_price - buy) / buy * 100 if buy > 0 else 0
        if abs(diff_pct) <= 2:
            ok_list.append(entry)
        elif diff_pct < -2:
            low_list.append(entry)
        elif diff_pct <= 5:
            high_list.append(entry)
        else:
            other_list.append(entry)

    # 组装推送消息
    lines = [f"📊 **盘前集合竞价速报**  {datetime.now(_CST).strftime('%m-%d %H:%M')}", ""]

    if ok_list:
        lines.append("## ✅ 符合预期")
        for e in ok_list:
            lines.append(
                f"- **{e['name']}**({e['code']}) "
                f"开盘 ¥{e['open']:.2f}  |  距建议买入价 {(e['open']-e['buy'])/e['buy']*100:+.1f}%  |  "
                f"{e['rec']}"
            )
        lines.append("")

    if low_list:
        lines.append("## 🟢 低开关注")
        for e in low_list:
            lines.append(
                f"- **{e['name']}**({e['code']}) "
                f"开盘 ¥{e['open']:.2f}  |  低于买入价 ¥{e['buy']:.2f}  |  "
                f"{(e['open']-e['buy'])/e['buy']*100:+.1f}%"
            )
        lines.append("")

    if high_list:
        lines.append("## ⚠️ 高开谨慎")
        for e in high_list:
            lines.append(
                f"- **{e['name']}**({e['code']}) "
                f"开盘 ¥{e['open']:.2f}  |  高于买入价 ¥{e['buy']:.2f}  |  "
                f"{(e['open']-e['buy'])/e['buy']*100:+.1f}%"
            )
        lines.append("")

    if other_list:
        lines.append("## 🔴 大幅高开")
        for e in other_list:
            lines.append(
                f"- **{e['name']}**({e['code']}) "
                f"开盘 ¥{e['open']:.2f}  |  高于买入价 ¥{e['buy']:.2f}  |  "
                f"{(e['open']-e['buy'])/e['buy']*100:+.1f}%"
            )
        lines.append("")

    if not any([ok_list, low_list, high_list, other_list]):
        lines.append("⚠️ 今日推荐股暂无有效开盘数据")

    text = "\n".join(lines)
    pushed = _push(text)

    return {
        "ok": True,
        "count": len(ok_list) + len(low_list) + len(high_list) + len(other_list),
        "ok_count": len(ok_list),
        "low_count": len(low_list),
        "high_count": len(high_list),
        "pushed": pushed,
    }


# ── 推送工具 ────────────────────────────────────────────────


def _push(message: str) -> bool:
    """推送到企微群（markdown 格式）。"""
    try:
        from wechat_push import push_wechat_markdown
        return push_wechat_markdown(message)
    except Exception:
        logger.warning("企微推送失败", exc_info=True)
        return False


# ── CLI ─────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("用法: python intraday_push.py <pre-market|monitor>", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "pre-market":
        result = run_pre_market_push()
        print(result)
    elif cmd == "monitor":
        print("盘中监控功能待实现（见 memory pending-features.md）")
        sys.exit(1)
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
