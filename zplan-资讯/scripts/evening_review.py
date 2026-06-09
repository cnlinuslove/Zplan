#!/usr/bin/env python3
"""盘后 TOP10 效果复盘：对比早间推荐与实际走势，分析偏差原因。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/evening_review.py
    cd zplan-资讯 && .venv/bin/python scripts/evening_review.py --run-id 42   # 指定 run_id
    cd zplan-资讯 && .venv/bin/python scripts/evening_review.py --dry-run     # 仅打印不推送

调度: 盘后管道完成后自动调用，或单独 launchd 18:00 触发。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(os.environ.get("ZPLAN_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(NEWS_ROOT))

from sqlalchemy import desc, select, text

from zplan_shared.models import (
    DailyPrice,
    PickEntry,
    PickRun,
    SessionLocal,
    StockList,
    init_db,
)
from wechat_push import push_wechat_markdown

BEIJING_TZ = timezone(timedelta(hours=8))


def _get_latest_trade_date(session) -> date | None:
    """最新交易日（daily_prices）。"""
    r = session.execute(text("SELECT MAX(trade_date) FROM daily_prices")).fetchone()
    if r and r[0]:
        d = r[0]
        if isinstance(d, str):
            return date.fromisoformat(d)
        return d
    return None


def _load_top10_from_run(session, run_id: int | None = None) -> tuple[list[PickEntry], PickRun | None, date | None]:
    """加载 TOP10 entries 及关联的 run。

    Args:
        run_id: 指定 run_id，不传则取最新。
    Returns:
        (entries, run, pick_date) — pick_date 是选股当日（用于匹配今日行情）
    """
    if run_id:
        run = session.get(PickRun, run_id)
    else:
        run = session.execute(
            select(PickRun)
            .where(PickRun.run_kind.in_(["scan", "llm_top300"]))
            .order_by(desc(PickRun.created_at_utc))
            .limit(1)
        ).scalars().first()

    if not run:
        return [], None, None

    entries = session.execute(
        select(PickEntry)
        .where(PickEntry.run_id == run.id)
        .order_by(
            PickEntry.rank_in_run,
            PickEntry.final_composite_score.desc().nullslast(),
        )
        .limit(10)
    ).scalars().all()

    pick_date = run.trade_date_as_of
    return list(entries), run, pick_date


def _get_today_performance(
    session,
    codes: list[str],
    today: date,
) -> dict[str, dict[str, float | None]]:
    """获取今日行情：open, high, low, close, pct_chg, volume, pre_close。"""
    if not codes or not today:
        return {}

    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": c for i, c in enumerate(codes)}
    params["d"] = today.strftime("%Y-%m-%d")

    rows = session.execute(
        text(
            f"SELECT ts_code, open, high, low, close, pct_chg, volume "
            f"FROM daily_prices "
            f"WHERE ts_code IN ({placeholders}) AND trade_date = :d"
        ),
        params,
    ).fetchall()

    result = {}
    for r in rows:
        # pre_close 可由 close 和 pct_chg 反推
        close_val = float(r[4]) if r[4] is not None else None
        pct_val = float(r[5]) if r[5] is not None else None
        pre_close = None
        if close_val is not None and pct_val is not None:
            pre_close = round(close_val / (1 + pct_val / 100), 2)
        result[r[0]] = {
            "open": float(r[1]) if r[1] is not None else None,
            "high": float(r[2]) if r[2] is not None else None,
            "low": float(r[3]) if r[3] is not None else None,
            "close": close_val,
            "pct_chg": pct_val,
            "volume": float(r[6]) if r[6] is not None else None,
            "pre_close": pre_close,
        }
    return result


def _buy_price_reached(
    perf: dict[str, float | None],
    entry: PickEntry,
) -> tuple[bool, str]:
    """检查是否触及建议买入价。"""
    predicted = entry.predicted_buy_price
    if predicted is None:
        return False, "无建议买入价"
    low = perf.get("low")
    if low is None:
        return False, "无日内最低价"
    if low <= predicted:
        return True, f"触及(低¥{low:.2f} ≤ 买¥{predicted:.2f})"
    else:
        gap_pct = (low - predicted) / predicted * 100
        return False, f"未触及(最低¥{low:.2f} vs 建议¥{predicted:.2f}，高{gap_pct:+.1f}%)"


def _analyze_entry(
    entry: PickEntry,
    perf: dict[str, float | None] | None,
    industry: str,
) -> dict[str, Any]:
    """单只标的分析。"""
    name = entry.name or entry.ts_code
    code = entry.ts_code
    score = entry.final_composite_score or entry.rule_composite_score

    if perf is None:
        return {
            "ts_code": code,
            "name": name,
            "score": score,
            "verdict": entry.verdict or "",
            "pct_chg": None,
            "close": None,
            "buy_reached": None,
            "buy_note": "今日无行情数据",
            "industry": industry,
            "predicted_buy": entry.predicted_buy_price,
            "predicted_target": entry.predicted_target_price,
            "predicted_stop": entry.predicted_stop_loss,
        }

    pct = perf.get("pct_chg")
    close = perf.get("close")
    buy_reached, buy_note = _buy_price_reached(perf, entry)

    return {
        "ts_code": code,
        "name": name,
        "score": score,
        "verdict": entry.verdict or "",
        "pct_chg": pct,
        "close": close,
        "open": perf.get("open"),
        "high": perf.get("high"),
        "low": perf.get("low"),
        "buy_reached": buy_reached,
        "buy_note": buy_note,
        "industry": industry,
        "predicted_buy": entry.predicted_buy_price,
        "predicted_target": entry.predicted_target_price,
        "predicted_stop": entry.predicted_stop_loss,
    }


def _compute_summary(analyzed: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总统计。"""
    with_data = [a for a in analyzed if a["pct_chg"] is not None]
    if not with_data:
        return {
            "total": len(analyzed),
            "with_data": 0,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "best": None,
            "worst": None,
            "buy_reached_count": 0,
            "verdict_accuracy": "",
        }

    up = [a for a in with_data if (a["pct_chg"] or 0) > 0]
    down = [a for a in with_data if (a["pct_chg"] or 0) < 0]
    flat = [a for a in with_data if (a["pct_chg"] or 0) == 0]

    returns = [a["pct_chg"] for a in with_data if a["pct_chg"] is not None]
    avg_ret = sum(returns) / len(returns) if returns else 0.0

    best = max(with_data, key=lambda a: a["pct_chg"] or -999)
    worst = min(with_data, key=lambda a: a["pct_chg"] or 999)

    buy_reached = sum(1 for a in with_data if a.get("buy_reached"))

    # 选股方向准确性：TOP10 本身隐含看多，上涨 = 方向正确
    # "看空"/"偏空"/"回避" 为逆势推荐（一般不应出现在 TOP10）
    correct = 0
    wrong_signal = 0  # 看空却上涨 or 看多却下跌
    for a in with_data:
        v = a.get("verdict", "")
        p = a["pct_chg"] or 0
        if v in ("看空", "偏空", "回避"):
            # 逆势推荐：下跌才算正确
            if p < 0:
                correct += 1
            else:
                wrong_signal += 1
        else:
            # 中性 / 偏多 / 关注 / 强烈关注：上涨才算正确
            if p > 0:
                correct += 1
            else:
                wrong_signal += 1
    verdict_acc = f"{correct}/{len(with_data)}" if with_data else "N/A"

    return {
        "total": len(analyzed),
        "with_data": len(with_data),
        "up_count": len(up),
        "down_count": len(down),
        "flat_count": len(flat),
        "win_rate": len(up) / len(with_data) * 100 if with_data else 0,
        "avg_return": avg_ret,
        "best": best,
        "worst": worst,
        "buy_reached_count": buy_reached,
        "verdict_accuracy": verdict_acc,
    }


def _analyze_deviation_reasons(
    analyzed: list[dict[str, Any]],
    summary: dict[str, Any],
    pick_date: date | None,
    today: date | None,
) -> list[str]:
    """分析偏差原因，返回要点列表。"""
    reasons: list[str] = []

    with_data = [a for a in analyzed if a["pct_chg"] is not None]
    if not with_data:
        reasons.append("⚠️ 今日无行情数据，可能为非交易日")
        return reasons

    avg_ret = summary["avg_return"]
    win_rate = summary["win_rate"]
    up = summary["up_count"]
    down = summary["down_count"]
    total = summary["with_data"]

    # 1. 整体方向判断
    if avg_ret > 1.0:
        reasons.append(f"✅ TOP10 整体跑赢，平均涨幅 **{avg_ret:+.2f}%**，胜率 {win_rate:.0f}% ({up}/{total})")
    elif avg_ret > 0:
        reasons.append(f"📊 TOP10 整体微盈，平均涨幅 **{avg_ret:+.2f}%**，胜率 {win_rate:.0f}% ({up}/{total})")
    elif avg_ret > -1.0:
        reasons.append(f"📊 TOP10 整体微亏，平均涨幅 **{avg_ret:+.2f}%**，胜率 {win_rate:.0f}% ({up}/{total})")
    else:
        reasons.append(f"⚠️ TOP10 整体承压，平均涨幅 **{avg_ret:+.2f}%**，胜率 {win_rate:.0f}% ({up}/{total})")

    # 2. 个股极端表现
    best = summary.get("best")
    worst = summary.get("worst")
    if best and (best["pct_chg"] or 0) > 3:
        reasons.append(
            f"🏆 最佳: **{best['name']}**({best['ts_code']}) "
            f"涨幅 **{best['pct_chg']:+.2f}%** · {best.get('verdict', '')}"
        )
    if worst and (worst["pct_chg"] or 0) < -3:
        reasons.append(
            f"📉 最差: **{worst['name']}**({worst['ts_code']}) "
            f"跌幅 **{worst['pct_chg']:+.2f}%** · {worst.get('verdict', '')}"
        )

    # 3. 建议买入价触及率
    with_buy = [a for a in with_data if a.get("predicted_buy") is not None]
    if with_buy:
        reached = summary.get("buy_reached_count", 0)
        reach_rate = reached / len(with_buy) * 100
        if reach_rate >= 50:
            reasons.append(f"🎯 建议买入价触及率 **{reached}/{len(with_buy)}** ({reach_rate:.0f}%)，定价合理")
        else:
            reasons.append(
                f"⚠️ 建议买入价触及率仅 **{reached}/{len(with_buy)}** ({reach_rate:.0f}%)，"
                f"建议价可能偏保守"
            )

    # 4. 评级准确性
    reasons.append(f"📋 方向判断准确率: {summary.get('verdict_accuracy', 'N/A')}")

    # 5. 数据时效提示
    if pick_date and today:
        gap = (today - pick_date).days
        if gap > 3:
            reasons.append(f"⏰ 选股数据距今 {gap} 天，信号可能衰减")

    return reasons


def _format_evening_markdown(
    analyzed: list[dict[str, Any]],
    summary: dict[str, Any],
    reasons: list[str],
    pick_label: str,
    today_label: str,
) -> str:
    """格式化盘后复盘 markdown。"""
    beijing_now = datetime.now(BEIJING_TZ)
    date_str = beijing_now.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    # 计算选股距今时间
    pick_age_note = ""
    try:
        pick_d = date.fromisoformat(pick_label)
        today_d = date.fromisoformat(today_label)
        gap = (today_d - pick_d).days
        if gap > 3:
            pick_age_note = f" ⚠️ 选股已过 {gap} 天，信号可能严重衰减"
        elif gap > 1:
            pick_age_note = f" 📎 选股距今 {gap} 天"
    except (ValueError, TypeError):
        pass

    lines = [
        f"## 🌅 Z-Plan TOP10 盘后复盘",
        f"> 发送日 {date_str} {weekday} · 行情日 **{today_label}** · 选股日 {pick_label}{pick_age_note}",
        "",
    ]

    # ── 汇总面板 ──
    lines.append(f"### 📊 {today_label} 表现总览")
    lines.append("")

    # 选股过期醒目警告
    try:
        pick_d = date.fromisoformat(pick_label)
        today_d = date.fromisoformat(today_label)
        gap = (today_d - pick_d).days
        if gap >= 7:
            lines.append(f"> ⚠️⚠️ **选股数据已过 {gap} 天，严重过期！请尽快运行选股流水线：`make llm-top`**")
            lines.append("")
        elif gap > 3:
            lines.append(f"> ⚠️ 选股距今 {gap} 天，信号可能衰减，建议尽快更新选股")
            lines.append("")
    except (ValueError, TypeError):
        pass
    avg_ret = summary["avg_return"]
    emoji = "🟢" if avg_ret > 0 else ("🔴" if avg_ret < 0 else "⚪")
    lines.append(
        f"| 指标 | 数值 |"
    )
    lines.append(f"|------|------|")
    lines.append(f"| {emoji} 平均涨跌幅 | **{avg_ret:+.2f}%** |")
    lines.append(f"| 📈 上涨 / 📉 下跌 / ➖ 持平 | {summary['up_count']} / {summary['down_count']} / {summary['flat_count']} |")
    lines.append(f"| 🎯 胜率 | {summary['win_rate']:.0f}% ({summary['up_count']}/{summary['with_data']}) |")
    lines.append(f"| 💰 建议买入价触及 | {summary.get('buy_reached_count', '-')}/{summary['with_data']} |")
    lines.append(f"| 🧭 方向判断准确 | {summary.get('verdict_accuracy', 'N/A')} |")
    lines.append("")

    # ── 偏差分析 ──
    lines.append("### 🔍 偏差分析")
    lines.append("")
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")

    # ── 逐只明细 ──
    lines.append("### 📋 逐只明细")
    lines.append("")

    for i, a in enumerate(analyzed):
        name = a["name"]
        code = a["ts_code"]
        pct = a["pct_chg"]
        close = a["close"]
        score = a["score"]
        verdict = a.get("verdict", "")
        buy_reached = a.get("buy_reached")
        buy_note = a.get("buy_note", "")

        if pct is not None:
            arrow = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
            pct_str = f"{arrow} **{pct:+.2f}%**"
            close_str = f"¥{close:.2f}" if close is not None else ""
        else:
            pct_str = "⚪ 无数据"
            close_str = ""

        score_str = f"{score:.0f}分" if score is not None else "--"

        lines.append(
            f"**{i+1}. {name}({code})** {pct_str} {close_str} · 评分 {score_str}"
        )

        detail_parts = []
        if verdict:
            detail_parts.append(f"评级: {verdict}")
        if a.get("predicted_buy"):
            detail_parts.append(f"建议买入 ¥{a['predicted_buy']:.2f}")
        if a.get("predicted_target"):
            detail_parts.append(f"目标 ¥{a['predicted_target']:.2f}")
        if detail_parts:
            lines.append(f"> {' · '.join(detail_parts)}")

        if buy_note:
            icon = "✅" if buy_reached else "⏳"
            lines.append(f"> {icon} {buy_note}")

        if a.get("industry"):
            lines.append(f"> 📌 {a['industry']}")

        lines.append("")

    lines.append("---")
    lines.append("💡 发送「**选股清单**」查看最新推荐 · 发送「**分析 股票名**」查看深度研报")

    return "\n".join(lines)


def _should_skip_today(today: date | None) -> tuple[bool, str]:
    """检查是否应该跳过复盘。

    核心逻辑：只有当 daily_prices 最新交易日 == 北京时间今天，才说明当日盘后管道已跑完，
    否则说明当日数据尚未入库，不应发送"今日复盘"。
    """
    if today is None:
        return True, "daily_prices 无数据"
    beijing_now = datetime.now(BEIJING_TZ)
    today_beijing = beijing_now.date()
    if beijing_now.weekday() >= 5:
        return True, "周末跳过"
    if today < today_beijing:
        return True, f"当日行情尚未入库（最新交易日: {today}，北京今天: {today_beijing}），等待盘后管道"
    return False, "ok"


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    # 解析 --run-id
    run_id: int | None = None
    for i, arg in enumerate(sys.argv):
        if arg == "--run-id" and i + 1 < len(sys.argv):
            run_id = int(sys.argv[i + 1])
            break

    init_db()

    with SessionLocal() as session:
        today = _get_latest_trade_date(session)

        should_skip, skip_reason = _should_skip_today(today)
        if should_skip and not force:
            print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] 跳过复盘: {skip_reason}")
            return
        elif should_skip and force:
            print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ⚠️ 强制运行（{skip_reason}）")

        entries, run, pick_date = _load_top10_from_run(session, run_id)

        if not entries:
            print("⚠️ 无选股记录，跳过复盘")
            return

        # 行业
        codes = [e.ts_code for e in entries]
        industry_map: dict[str, str] = {}
        rows = session.execute(
            select(StockList.ts_code, StockList.industry)
            .where(StockList.ts_code.in_(codes))
        ).all()
        industry_map = {r.ts_code: (r.industry or "") for r in rows}

        # 今日行情
        perf_map = _get_today_performance(session, codes, today) if today else {}

    # 逐只分析
    analyzed = [
        _analyze_entry(entry, perf_map.get(entry.ts_code), industry_map.get(entry.ts_code, ""))
        for entry in entries
    ]

    summary = _compute_summary(analyzed)
    reasons = _analyze_deviation_reasons(analyzed, summary, pick_date, today)

    pick_label = str(pick_date) if pick_date else (run.trade_date_as_of.strftime("%Y-%m-%d") if run and run.trade_date_as_of else "未知")
    today_label = str(today) if today else "无数据"

    markdown = _format_evening_markdown(analyzed, summary, reasons, pick_label, today_label)

    if dry_run:
        print("=" * 50)
        print("[DRY RUN] 以下为企微推送内容预览:")
        print("=" * 50)
        print(markdown)
        print("=" * 50)
        json_summary = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
        print(f"\n汇总 JSON:\n{json_summary}")
        return

    ok = push_wechat_markdown(markdown)
    if ok:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ✅ 盘后复盘推送成功 ({summary['with_data']}/{summary['total']} 只有数据)")
    else:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ❌ 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
