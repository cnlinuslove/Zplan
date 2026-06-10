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

# ── 大盘指数 ──
_INDEX_TX_MAP = {
    "000001": "sh000001", "399001": "sz399001", "399006": "sz399006",
    "000688": "sh000688", "000300": "sh000300", "000905": "sh000905",
    "000852": "sh000852",
}
_INDEX_NAMES = {
    "000001": "上证指数", "399001": "深证成指", "399006": "创业板指",
    "000688": "科创50", "000300": "沪深300", "000905": "中证500",
    "000852": "中证1000",
}
_INDEX_ORDER = ["000001", "399001", "399006", "000688", "000300", "000905", "000852"]


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
            .where(PickRun.run_kind == "llm_top300")
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
    """获取今日行情：open, high, low, close, pct_chg。pct_chg 优先原字段，NULL 则自算。"""
    if not codes or not today:
        return {}

    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": c for i, c in enumerate(codes)}
    params["d"] = today.strftime("%Y-%m-%d")

    rows = session.execute(
        text(
            f"SELECT dp.ts_code, dp.open, dp.high, dp.low, dp.close, dp.pct_chg, "
            f"  dp_prev.close AS prev_close "
            f"FROM daily_prices dp "
            f"LEFT JOIN daily_prices dp_prev ON dp.ts_code = dp_prev.ts_code "
            f"  AND dp_prev.trade_date = DATE(:d, '-1 day') "
            f"WHERE dp.ts_code IN ({placeholders}) AND dp.trade_date = :d"
        ),
        params,
    ).fetchall()

    result = {}
    for r in rows:
        close_val = float(r[4]) if r[4] is not None else None
        pct_val = float(r[5]) if r[5] is not None else None
        prev_close = float(r[6]) if r[6] is not None else None

        # pct_chg 为空时用 prev_close 自算
        if pct_val is None and close_val is not None and prev_close and prev_close != 0:
            pct_val = round((close_val - prev_close) / prev_close * 100, 2)

        # pre_close 用于显示
        pre_close_display = None
        if close_val is not None and pct_val is not None:
            pre_close_display = round(close_val / (1 + pct_val / 100), 2)
        elif prev_close is not None:
            pre_close_display = prev_close

        result[r[0]] = {
            "open": float(r[1]) if r[1] is not None else None,
            "high": float(r[2]) if r[2] is not None else None,
            "low": float(r[3]) if r[3] is not None else None,
            "close": close_val,
            "pct_chg": pct_val,
            "volume": None,
            "pre_close": pre_close_display,
        }
    return result


def _fetch_index_performance(trade_date: date) -> list[dict[str, Any]]:
    """拉取七大指数当日行情（东财优先 → 腾讯兜底）。失败返回空列表。"""
    import akshare as ak

    date_str = trade_date.strftime("%Y-%m-%d")
    start_s = trade_date.strftime("%Y%m%d")
    end_s = start_s
    results: list[dict[str, Any]] = []

    for code in _INDEX_ORDER:
        name = _INDEX_NAMES.get(code, code)
        close_val = None
        pct = None
        got = False

        # 1. 东财 index_zh_a_hist（支持日期范围，只拉 1 天）
        try:
            raw = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_s, end_date=end_s)
            if raw is not None and not raw.empty:
                r = raw.iloc[-1]
                close_val = float(r["收盘"]) if "收盘" in raw.columns and r.get("收盘") is not None else None
                pct = float(r["涨跌幅"]) if "涨跌幅" in raw.columns and r.get("涨跌幅") is not None else None
                got = close_val is not None
        except Exception:
            pass

        # 2. 东财失败 → 腾讯兜底（需全量下载，较慢）
        if not got:
            tx_sym = _INDEX_TX_MAP.get(code)
            if tx_sym:
                try:
                    raw = ak.stock_zh_index_daily_tx(symbol=tx_sym)
                    if raw is not None and not raw.empty:
                        mask = raw["date"].astype(str).str[:10] == date_str
                        matches = raw[mask]
                        if not matches.empty:
                            idx = matches.index[0]
                            r = raw.loc[idx]
                            close_val = float(r["close"])
                            prev_close = float(raw.loc[idx - 1, "close"]) if idx > 0 else None
                            pct = round((close_val - prev_close) / prev_close * 100, 2) if prev_close and prev_close != 0 else None
                            got = True
                except Exception:
                    pass

        if got and close_val is not None:
            results.append({"code": code, "name": name, "close": close_val, "pct_chg": pct})

    return results


def _compute_market_fallback(session, trade_date: date) -> dict[str, Any]:
    """全A股聚合（指数拉不到时兜底）：中位数涨跌 + 涨跌家数。"""
    d = trade_date.strftime("%Y-%m-%d")
    r = session.execute(
        text(
            "SELECT COUNT(dp.ts_code), "
            "SUM(CASE WHEN (dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100 > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN (dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100 < 0 THEN 1 ELSE 0 END), "
            "ROUND(AVG((dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100), 2) "
            "FROM daily_prices dp "
            "JOIN daily_prices dp_prev ON dp.ts_code = dp_prev.ts_code "
            "  AND dp_prev.trade_date = DATE(:d, '-1 day') "
            "WHERE dp.trade_date = :d AND dp.market = 'a' "
            "  AND dp.close IS NOT NULL AND dp_prev.close IS NOT NULL"
        ),
        {"d": d},
    ).fetchone()

    total = int(r[0]) if r else 0
    median = 0.0
    if total > 0:
        mr = session.execute(
            text(
                "SELECT (dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100 AS pct "
                "FROM daily_prices dp "
                "JOIN daily_prices dp_prev ON dp.ts_code = dp_prev.ts_code "
                "  AND dp_prev.trade_date = DATE(:d, '-1 day') "
                "WHERE dp.trade_date = :d AND dp.market = 'a' "
                "  AND dp.close IS NOT NULL AND dp_prev.close IS NOT NULL "
                "ORDER BY pct LIMIT 1 OFFSET :off"
            ),
            {"d": d, "off": total // 2},
        ).fetchone()
        median = round(float(mr[0]), 2) if mr and mr[0] else 0.0

    return {
        "total": total,
        "up_n": int(r[1]) if r else 0,
        "down_n": int(r[2]) if r else 0,
        "avg_pct": float(r[3]) if r and r[3] else 0.0,
        "median_pct": median,
    }


def _compute_industry_performance(session, trade_date: date) -> list[dict[str, Any]]:
    """板块表现：按 industry 聚合当日涨跌幅（用 close vs prev_close 自算）。"""
    rows = session.execute(
        text(
            "SELECT sl.industry, COUNT(*) AS n, "
            "ROUND(AVG((dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100), 2), "
            "SUM(CASE WHEN (dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100 > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN (dp.close - dp_prev.close) / NULLIF(dp_prev.close, 0) * 100 < 0 THEN 1 ELSE 0 END) "
            "FROM daily_prices dp "
            "JOIN stock_list sl ON dp.ts_code = sl.ts_code "
            "JOIN daily_prices dp_prev ON dp.ts_code = dp_prev.ts_code "
            "  AND dp_prev.trade_date = DATE(:d, '-1 day') "
            "WHERE dp.trade_date = :d AND dp.market = 'a' "
            "  AND sl.industry IS NOT NULL AND sl.industry != '' "
            "  AND dp.close IS NOT NULL AND dp_prev.close IS NOT NULL "
            "GROUP BY sl.industry HAVING n >= 8 ORDER BY 2 DESC"
        ),
        {"d": trade_date.strftime("%Y-%m-%d")},
    ).fetchall()

    return [
        {"industry": r[0], "stock_cnt": r[1],
         "avg_pct": float(r[2]) if r[2] is not None else 0.0,
         "up_n": r[3], "down_n": r[4]}
        for r in rows
    ]


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
    index_data: list[dict[str, Any]] | None = None,
    industries: list[dict[str, Any]] | None = None,
    market_fallback: dict[str, Any] | None = None,
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

    # ── 大盘总览 ──
    if index_data:
        lines.append("### 🏛️ 大盘总览")
        lines.append("")
        lines.append("| 指数 | 收盘 | 涨跌幅 |")
        lines.append("|------|------|--------|")
        for ix in index_data:
            name = ix["name"]
            close_s = f"{ix['close']:,.2f}" if ix["close"] else "--"
            pct = ix.get("pct_chg")
            if pct is not None:
                arrow = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
                pct_s = f"{arrow} {pct:+.2f}%"
            else:
                pct_s = "--"
            lines.append(f"| {name} | {close_s} | {pct_s} |")
        lines.append("")

    if market_fallback:
        total = market_fallback.get("total", 0)
        up_n = market_fallback.get("up_n", 0)
        down_n = market_fallback.get("down_n", 0)
        med = market_fallback.get("median_pct", 0)
        med_arrow = "🔺" if med > 0 else ("🔻" if med < 0 else "➖")
        if not index_data:
            lines.append("### 🏛️ 大盘总览")
            lines.append("")
        lines.append(f"> 📊 全市场 {total} 只 · 上涨 **{up_n}** / 下跌 **{down_n}** · 中位数 {med_arrow} **{med:+.2f}%**")
        lines.append("")

    # ── 板块表现 ──
    if industries and len(industries) >= 2:
        lines.append("### 🏭 板块表现")
        lines.append("")
        top_n = min(5, len(industries) // 2)
        top = industries[:top_n]
        bottom = list(reversed(industries[-top_n:]))
        lines.append("| 🟢 最强板块 | 涨幅 | 涨/跌 | 🔻 最弱板块 | 涨幅 | 涨/跌 |")
        lines.append("|--------|------|-------|--------|------|-------|")
        for i in range(top_n):
            t = top[i] if i < len(top) else None
            b = bottom[i] if i < len(bottom) else None
            t_str = f"{t['industry']} | {t['avg_pct']:+.2f}% | {t['up_n']}/{t['down_n']}" if t else " | | "
            b_str = f"{b['industry']} | {b['avg_pct']:+.2f}% | {b['up_n']}/{b['down_n']}" if b else " | | "
            lines.append(f"| {t_str} | {b_str} |")
        up_sectors = sum(1 for ind in industries if ind["avg_pct"] > 0)
        dn_sectors = sum(1 for ind in industries if ind["avg_pct"] < 0)
        lines.append(f"> 共 {len(industries)} 个板块 · 上涨 {up_sectors} / 下跌 {dn_sectors}")
        lines.append("")

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

    # ── T+1 规划 ──
    lines.extend(_format_t1_planning(analyzed, summary))
    lines.append("")
    lines.append("---")
    lines.append("💡 发送「**选股清单**」查看最新推荐 · 发送「**分析 股票名**」查看深度研报")

    return "\n".join(lines)


def _format_t1_planning(
    analyzed: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[str]:
    """生成 T+1 日规划段落：明天卖出计划 + 继续关注 + 资金规划。"""
    lines: list[str] = []
    lines.append("### 📅 T+1 明日规划")
    lines.append("")

    # 1. 今日触及买入价的标的 → 明日卖出计划
    bought = [a for a in analyzed if a.get("buy_reached") and (a.get("pct_chg") is not None)]
    if bought:
        lines.append("**今日入场 · 明日卖出计划**")
        lines.append("")
        lines.append("| 标的 | 成本区 | 目标 | 止损 | 明日策略 |")
        lines.append("|------|--------|------|------|----------|")
        for a in bought:
            name = a["name"]
            code = a["ts_code"]
            buy = a.get("predicted_buy")
            tgt = a.get("predicted_target")
            stop = a.get("predicted_stop")
            pct = a.get("pct_chg") or 0

            # 明日策略：根据今日涨跌判断
            if pct > 3:
                strategy = "冲高逐步止盈"
            elif pct > 0:
                strategy = "持有观察，目标价不变"
            elif pct > -3:
                strategy = "关注是否反弹，止损不变"
            else:
                strategy = "⚠️ 接近止损，优先减仓"

            buy_s = f"¥{buy:.2f}" if buy else "--"
            tgt_s = f"¥{tgt:.2f}" if tgt else "--"
            stop_s = f"¥{stop:.2f}" if stop else "--"

            lines.append(f"| {name}({code}) | {buy_s} | {tgt_s} | {stop_s} | {strategy} |")
        lines.append("")

    # 2. 今日未触及的推荐 → 明天是否继续有效
    not_bought = [a for a in analyzed if not a.get("buy_reached")]
    if not_bought:
        # 筛选未大涨的（还有机会）
        still_valid = [a for a in not_bought if (a.get("pct_chg") or 0) < 3]
        if still_valid:
            lines.append("**今日未入场 · 明日继续关注**")
            lines.append("")
            for a in still_valid[:5]:
                name = a["name"]
                code = a["ts_code"]
                buy = a.get("predicted_buy")
                pct = a.get("pct_chg")
                pct_s = f"{pct:+.1f}%" if pct is not None else "--"
                buy_s = f"¥{buy:.2f}" if buy else "--"
                lines.append(f"- {name}({code}) 今日{pct_s} · 买入参考 {buy_s}")
            lines.append("")
            lines.append("> ⚠️ 明日盘前会重新调整买入价，以盘前简报为准")
            lines.append("")

    # 3. 资金规划
    lines.append("**💰 资金规划**")
    lines.append("")
    bought_count = len(bought)
    if bought_count == 0:
        lines.append(f"- 今日未入场，明日可用资金 100%")
    elif bought_count <= 2:
        lines.append(f"- 今日入场 {bought_count} 只，明日以卖出为主，可用资金 ~30-50%")
    elif bought_count <= 5:
        lines.append(f"- 今日入场 {bought_count} 只，明日优先处理持仓，可用资金 ~20%")
    else:
        lines.append(f"- 今日入场 {bought_count} 只（重仓），明日**只卖不买**")

    return lines


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


# ── 预测验证 ──────────────────────────────────────────────────────

def _verify_forecast(session, today: date) -> dict[str, Any] | None:
    """验证最近一次未验证的大盘预测（委托到 forecast_verify 模块）。

    找到 as_of_date < today 且未验证的预测，用今日实际指数走势做对照。
    """
    from scripts.forecast_verify import verify_all_outstanding, _verify_single

    # 先尝试批量验证（处理所有未验证记录）
    outcome = verify_all_outstanding(session)
    results = outcome.get("results", [])
    if not results:
        return None

    # 返回最近一条验证结果（兼容现有 evening_review 下游格式）
    return results[-1] if results else None


def _format_forecast_verification(verification: dict[str, Any] | None) -> list[str]:
    """格式化预测验证结果为 markdown 段落。"""
    if not verification:
        return []

    if verification.get("error"):
        return [f"> ⚠️ 预测验证: {verification['error']}"]

    lines = [
        "### 🎯 昨日预测对照",
        "",
        f"> 预测日期: **{verification['forecast_date']}** · 验证日期: **{verification['verify_date']}**",
        "",
    ]

    matched = verification.get("direction_correct", False)
    predicted = verification.get("predicted_direction", "?")
    actual = verification.get("actual_direction", "?")
    direction_map = {"bullish": "🟢看涨", "bearish": "🔴看跌", "range-bound": "🟡震荡"}

    emoji = "✅" if matched else "❌"
    lines.append(
        f"| 项目 | 预测 | 实际 | 结果 |"
    )
    lines.append(f"|------|------|------|------|")
    lines.append(
        f"| 大盘方向 | {direction_map.get(predicted, predicted)} | "
        f"{direction_map.get(actual, actual)} ({verification.get('actual_pct_chg', 0):+.2f}%) | "
        f"{emoji} {'正确' if matched else '偏差'} |"
    )

    # 各指数对照
    index_results = verification.get("index_results") or []
    if index_results:
        for ix in index_results:
            icon = "✅" if ix.get("correct") else "❌"
            lines.append(
                f"| {ix['name']} | {ix['predicted']} | "
                f"{ix['actual']} ({ix.get('actual_pct', 0):+.2f}%) | {icon} |"
            )

    correct_str = verification.get("index_correct", "N/A")
    lines.append(f"> 指数方向准确率: **{correct_str}**")
    lines.append("")

    return lines


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
        # 板块表现 + 大盘
        industries = _compute_industry_performance(session, today) if today else []
        # 指数拉取放到 session 外面做（纯网络IO）

    # 大盘指数（网络IO，在 session 外执行）
    index_data: list[dict[str, Any]] = []
    market_fallback: dict[str, Any] | None = None
    if today:
        index_data = _fetch_index_performance(today)
        if not index_data:
            # 兜底：全A股聚合
            with SessionLocal() as s2:
                market_fallback = _compute_market_fallback(s2, today)

    # 逐只分析
    analyzed = [
        _analyze_entry(entry, perf_map.get(entry.ts_code), industry_map.get(entry.ts_code, ""))
        for entry in entries
    ]

    summary = _compute_summary(analyzed)
    reasons = _analyze_deviation_reasons(analyzed, summary, pick_date, today)

    pick_label = str(pick_date) if pick_date else (run.trade_date_as_of.strftime("%Y-%m-%d") if run and run.trade_date_as_of else "未知")
    today_label = str(today) if today else "无数据"

    markdown = _format_evening_markdown(
        analyzed, summary, reasons, pick_label, today_label,
        index_data=index_data, industries=industries, market_fallback=market_fallback,
    )

    # ── 预测验证（对照昨日预测 vs 今日实际）──
    forecast_verification = None
    if today:
        with SessionLocal() as s3:
            try:
                forecast_verification = _verify_forecast(s3, today)
            except Exception as exc:
                print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ⚠️ 预测验证失败: {exc}")

    if forecast_verification:
        fv_lines = _format_forecast_verification(forecast_verification)
        # 插入到「T+1 规划」之前
        marker = "### 📅 T+1 明日规划"
        if marker in markdown:
            markdown = markdown.replace(marker, "\n".join(fv_lines) + "\n" + marker)
        else:
            markdown += "\n" + "\n".join(fv_lines)

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
