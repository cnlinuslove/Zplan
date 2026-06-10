#!/usr/bin/env python3
"""综合复盘：当日 TOP10 表现 + 策略回测诊断 + T+1 规划，合并为一条企微推送。

合并了 evening_review.py（同日表现）和 backtest_review_push.py（多窗口回测验证），
避免两条消息内容同质化。保持 ≤3800 字节企微 markdown 限制。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/evening_combined_review.py
    cd zplan-资讯 && .venv/bin/python scripts/evening_combined_review.py --dry-run
    cd zplan-资讯 && .venv/bin/python scripts/evening_combined_review.py --run-id 42

调度: run_full_pipeline.sh 盘后管道完成后自动调用。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
NEWS_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(NEWS_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))  # so we can import evening_review as a module

from sqlalchemy import desc, select, text

from zplan_shared.models import (
    DailyPrice,
    MarketForecast,
    PickEntry,
    PickRun,
    SessionLocal,
    StockList,
    init_db,
)
from zplan_shared.pick_llm_eval import FAIL_TAG_LABELS, evaluate_llm_run
from zplan_shared.pick_predictions import list_outcomes, validate_entries
from wechat_push import push_wechat_markdown

BEIJING_TZ = timezone(timedelta(hours=8))
WECHAT_SAFE_LIMIT = 3800
DEFAULT_HORIZONS = [5, 10, 20]

_INDEX_ORDER = ["000001", "399001", "399006", "000688", "000300", "000905", "000852"]
_INDEX_NAMES = {
    "000001": "上证指数", "399001": "深证成指", "399006": "创业板指",
    "000688": "科创50", "000300": "沪深300", "000905": "中证500",
    "000852": "中证1000",
}


# ═══ 数据加载（独立实现，不依赖 evening_review.py 的 akshare 调用）═══


def _get_latest_trade_date(session) -> date | None:
    r = session.execute(text("SELECT MAX(trade_date) FROM daily_prices")).fetchone()
    if r and r[0]:
        d = r[0]
        return date.fromisoformat(d) if isinstance(d, str) else d
    return None


def _load_top10_from_run(session, run_id: int | None = None):
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
        .order_by(PickEntry.rank_in_run, PickEntry.final_composite_score.desc().nullslast())
        .limit(10)
    ).scalars().all()
    return list(entries), run, run.trade_date_as_of


def _get_today_performance(session, codes: list[str], today: date) -> dict[str, dict]:
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
        if pct_val is None and close_val is not None and prev_close and prev_close != 0:
            pct_val = round((close_val - prev_close) / prev_close * 100, 2)
        result[r[0]] = {
            "open": float(r[1]) if r[1] is not None else None,
            "high": float(r[2]) if r[2] is not None else None,
            "low": float(r[3]) if r[3] is not None else None,
            "close": close_val,
            "pct_chg": pct_val,
        }
    return result


def _get_index_today(session, trade_date: date) -> list[dict[str, Any]]:
    """从 daily_index 表拉取七大指数当日行情（pct_chg 为 NULL 时从 close 自算）。"""
    d = trade_date.strftime("%Y-%m-%d")
    placeholders = ",".join(f":c{i}" for i in range(len(_INDEX_ORDER)))
    params = {f"c{i}": c for i, c in enumerate(_INDEX_ORDER)}
    params["d"] = d
    rows = session.execute(
        text(
            f"SELECT di.index_code, di.close, di.pct_chg, dip.close AS prev_close "
            f"FROM daily_index di "
            f"LEFT JOIN daily_index dip ON di.index_code = dip.index_code "
            f"  AND dip.trade_date = DATE(:d, '-1 day') "
            f"WHERE di.index_code IN ({placeholders}) AND di.trade_date = :d"
        ),
        params,
    ).fetchall()
    results = []
    for code, close, pct_chg, prev_close in rows:
        name = _INDEX_NAMES.get(code, code)
        close_val = float(close) if close is not None else None
        pct_val = float(pct_chg) if pct_chg is not None else None
        if pct_val is None and close_val is not None and prev_close is not None:
            prev_val = float(prev_close)
            if prev_val > 0:
                pct_val = round((close_val - prev_val) / prev_val * 100, 2)
        results.append({
            "code": code, "name": name,
            "close": close_val, "pct_chg": pct_val,
        })
    return results


def _compute_market_fallback(session, trade_date: date) -> dict[str, Any]:
    """全A股聚合：中位数涨跌 + 涨跌家数。"""
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
                "FROM daily_prices dp JOIN daily_prices dp_prev ON dp.ts_code = dp_prev.ts_code "
                "  AND dp_prev.trade_date = DATE(:d, '-1 day') "
                "WHERE dp.trade_date = :d AND dp.market = 'a' "
                "  AND dp.close IS NOT NULL AND dp_prev.close IS NOT NULL "
                "ORDER BY pct LIMIT 1 OFFSET :off"
            ),
            {"d": d, "off": total // 2},
        ).fetchone()
        median = round(float(mr[0]), 2) if mr and mr[0] else 0.0
    return {
        "total": total, "up_n": int(r[1]) if r else 0,
        "down_n": int(r[2]) if r else 0,
        "avg_pct": float(r[3]) if r and r[3] else 0.0,
        "median_pct": median,
    }


def _buy_price_reached(perf: dict, entry: PickEntry) -> tuple[bool, str]:
    predicted = entry.predicted_buy_price
    if predicted is None:
        return False, "无建议买入价"
    low = perf.get("low")
    if low is None:
        return False, "无日内最低价"
    if low <= predicted:
        return True, f"触及(¥{low:.2f}≤¥{predicted:.2f})"
    else:
        gap_pct = (low - predicted) / predicted * 100
        return False, f"未触及(低¥{low:.2f}高{gap_pct:+.1f}%)"


def _analyze_entry(entry: PickEntry, perf: dict | None, industry: str) -> dict[str, Any]:
    name = entry.name or entry.ts_code
    code = entry.ts_code
    score = entry.final_composite_score or entry.rule_composite_score
    if perf is None:
        return {
            "ts_code": code, "name": name, "score": score,
            "verdict": entry.verdict or "", "pct_chg": None, "close": None,
            "buy_reached": None, "buy_note": "无行情", "industry": industry,
            "predicted_buy": entry.predicted_buy_price,
            "predicted_target": entry.predicted_target_price,
            "predicted_stop": entry.predicted_stop_loss,
        }
    pct = perf.get("pct_chg")
    close = perf.get("close")
    buy_reached, buy_note = _buy_price_reached(perf, entry)
    return {
        "ts_code": code, "name": name, "score": score,
        "verdict": entry.verdict or "", "pct_chg": pct, "close": close,
        "open": perf.get("open"), "high": perf.get("high"), "low": perf.get("low"),
        "buy_reached": buy_reached, "buy_note": buy_note, "industry": industry,
        "predicted_buy": entry.predicted_buy_price,
        "predicted_target": entry.predicted_target_price,
        "predicted_stop": entry.predicted_stop_loss,
    }


def _compute_summary(analyzed: list[dict[str, Any]]) -> dict[str, Any]:
    with_data = [a for a in analyzed if a["pct_chg"] is not None]
    if not with_data:
        return {"total": len(analyzed), "with_data": 0, "up_count": 0, "down_count": 0,
                "flat_count": 0, "win_rate": 0.0, "avg_return": 0.0,
                "best": None, "worst": None, "buy_reached_count": 0, "verdict_accuracy": ""}
    up = [a for a in with_data if (a["pct_chg"] or 0) > 0]
    down = [a for a in with_data if (a["pct_chg"] or 0) < 0]
    flat = [a for a in with_data if (a["pct_chg"] or 0) == 0]
    returns = [a["pct_chg"] for a in with_data if a["pct_chg"] is not None]
    avg_ret = sum(returns) / len(returns) if returns else 0.0
    best = max(with_data, key=lambda a: a["pct_chg"] or -999)
    worst = min(with_data, key=lambda a: a["pct_chg"] or 999)
    buy_reached = sum(1 for a in with_data if a.get("buy_reached"))
    correct = 0
    for a in with_data:
        v = a.get("verdict", "")
        p = a["pct_chg"] or 0
        if v in ("看空", "偏空", "回避"):
            if p < 0: correct += 1
        else:
            if p > 0: correct += 1
    verdict_acc = f"{correct}/{len(with_data)}" if with_data else "N/A"
    return {
        "total": len(analyzed), "with_data": len(with_data),
        "up_count": len(up), "down_count": len(down), "flat_count": len(flat),
        "win_rate": len(up) / len(with_data) * 100 if with_data else 0,
        "avg_return": avg_ret, "best": best, "worst": worst,
        "buy_reached_count": buy_reached, "verdict_accuracy": verdict_acc,
    }


# ═══ 当日表现（复用 evening_review 函数）══════════════════════════════

def _build_today_section(
    analyzed: list[dict[str, Any]],
    summary: dict[str, Any],
    index_data: list[dict[str, Any]],
    market_fallback: dict[str, Any] | None,
    today_label: str,
) -> list[str]:
    """构建「今日表现」段落 — 紧凑版大盘 + TOP10 概要。"""
    lines: list[str] = []

    # ── 大盘一行 ──
    if index_data:
        parts = []
        for ix in index_data[:4]:  # 只显示前 4 个指数
            pct = ix.get("pct_chg")
            if pct is not None:
                arrow = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
                parts.append(f"{ix['name']} {arrow}{pct:+.2f}%")
        if parts:
            lines.append("🏛 大盘: " + " · ".join(parts))
    elif market_fallback:
        total = market_fallback.get("total", 0)
        up_n = market_fallback.get("up_n", 0)
        down_n = market_fallback.get("down_n", 0)
        med = market_fallback.get("median_pct", 0)
        med_arrow = "🔺" if med > 0 else ("🔻" if med < 0 else "➖")
        lines.append(f"🏛 全市场 {total}只 · ↑{up_n}/↓{down_n} · 中位{med_arrow}{med:+.2f}%")

    # ── TOP10 概要一行 ──
    avg_ret = summary["avg_return"]
    win_rate = summary["win_rate"]
    reached = summary.get("buy_reached_count", 0)
    with_data = summary.get("with_data", 0)
    emoji = "🟢" if avg_ret > 0 else ("🔴" if avg_ret < 0 else "⚪")
    lines.append(
        f"📊 TOP10: {emoji}均**{avg_ret:+.2f}%** · 胜{win_rate:.0f}%({summary['up_count']}/{with_data}) · 触及买入{reached}/{with_data}"
    )

    # ── 逐只一行（只标极端）──
    best = summary.get("best")
    worst = summary.get("worst")
    if best and (best.get("pct_chg") or 0) > 3:
        lines.append(f"🏆 {best['name']}({best['ts_code']}) +{best['pct_chg']:+.2f}%")
    if worst and (worst.get("pct_chg") or 0) < -3:
        lines.append(f"📉 {worst['name']}({worst['ts_code']}) {worst['pct_chg']:+.2f}%")

    # ── 逐只紧凑表 ──
    with_data_list = [a for a in analyzed if a.get("pct_chg") is not None]
    if with_data_list:
        lines.append("")
        lines.append("| # | 股票 | 评分 | 收盘 | 今涨跌 | 触及 |")
        lines.append("|---|------|------|------|--------|------|")
        for i, a in enumerate(analyzed[:10]):
            name = (a.get("name") or a.get("ts_code", "?"))[:8]
            code = a.get("ts_code", "?")
            score = a.get("score")
            score_s = f"{score:.0f}" if score is not None else "--"
            close = a.get("close")
            close_s = f"¥{close:.2f}" if close is not None else "--"
            pct = a.get("pct_chg")
            if pct is not None:
                arrow = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
                pct_s = f"{arrow}{pct:+.1f}%"
            else:
                pct_s = "--"
            buy_reached = a.get("buy_reached")
            touch_s = "✅" if buy_reached else ("—" if buy_reached is None else "⏳")
            lines.append(f"| {i+1} | {name}({code}) | {score_s} | {close_s} | {pct_s} | {touch_s} |")

    return lines


# ═══ 策略诊断（复用 backtest 数据）════════════════════════════════════

def _build_strategy_section(
    llm_eval: dict[str, Any],
    outcomes_by_horizon: dict[int, list[dict[str, Any]]],
    top_n: int,
) -> list[str]:
    """构建「策略诊断」段落 — 多窗口收益 + 失败标签。"""
    lines: list[str] = []
    s = llm_eval.get("summary") or {}
    total = s.get("total", 0)
    fail_n = s.get("fail", 0)
    fail_rate = s.get("fail_rate") or 0

    # ── 多窗口收益表 ──
    lines.append("| 窗口 | 均收益 | 胜率 | 峰值均 |")
    lines.append("|------|--------|------|--------|")
    for h in DEFAULT_HORIZONS:
        outcomes = outcomes_by_horizon.get(h, [])
        if not outcomes:
            lines.append(f"| T+{h} | — | — | — |")
            continue
        complete = [o for o in outcomes if o.get("status") in ("complete", "partial")]
        if not complete:
            lines.append(f"| T+{h} | — | — | — |")
            continue
        rets = [o.get("return_from_close_pct") for o in complete if o.get("return_from_close_pct") is not None]
        peaks = [o.get("peak_return_pct") for o in complete if o.get("peak_return_pct") is not None]
        avg_ret = sum(rets) / len(rets) if rets else None
        avg_peak = sum(peaks) / len(peaks) if peaks else None
        win = sum(1 for r in rets if r > 0) / len(rets) * 100 if rets else 0
        ret_s = f"{avg_ret:+.1f}%" if avg_ret is not None else "—"
        peak_s = f"{avg_peak:+.1f}%" if avg_peak is not None else "—"
        win_s = f"{win:.0f}%"
        lines.append(f"| T+{h} | {ret_s} | {win_s} | {peak_s} |")

    # ── 失败率 + 标签 ──
    lines.append("")
    pass_n = s.get("pass", 0)
    pending_n = s.get("pending", 0)
    status_line = f"样本{total} · ❌失败{fail_n} · ✅通过{pass_n}"
    if pending_n:
        status_line += f" · ⏳待验证{pending_n}"
    if fail_rate > 0:
        status_line += f" · 失败率**{fail_rate:.0%}**"
    lines.append(status_line)

    # 标签分布（Top 3）
    tag_counts: dict[str, int] = llm_eval.get("tag_counts") or {}
    if tag_counts:
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:3]
        tag_parts = []
        for tag, cnt in top_tags:
            label = FAIL_TAG_LABELS.get(tag, tag)
            tag_parts.append(f"`{tag}`×{cnt}")
        lines.append("🏷 " + " · ".join(tag_parts))

    # ── 逐只失败标签（只列有标签的）──
    entries = llm_eval.get("entries") or []
    failed_entries = [e for e in entries if e.get("failure_tags")]
    if failed_entries:
        lines.append("")
        lines.append("| 股票 | 标签 | 建议 |")
        lines.append("|------|------|------|")
        for e in failed_entries[:5]:
            name = (e.get("name") or e.get("ts_code", "?"))[:8]
            code = e.get("ts_code", "?")
            tags = e.get("failure_tags") or []
            tag_str = ", ".join(FAIL_TAG_LABELS.get(t, t) for t in tags[:2])
            action = _suggest_action(tags, e.get("verdict", ""))
            lines.append(f"| {name}({code}) | {tag_str} | {action} |")

    # ── 优化建议（1行）──
    opt = llm_eval.get("optimization") or {}
    priority = opt.get("priority") or []
    if priority:
        tag, cnt = priority[0]
        label = FAIL_TAG_LABELS.get(tag, tag)
        lines.append(f"🔧 优先优化: [{tag}] {label}（{cnt}次）")

    return lines


def _suggest_action(tags: list[str], verdict: str) -> str:
    """根据失败标签组合给出后续操作建议（精简版）。"""
    if not tags:
        return "观察"
    actions = []
    if "forward_loss" in tags:
        actions.append("观望等企稳")
    if "momentum_chase" in tags:
        actions.append("等回调")
    if "buy_unreachable" in tags:
        actions.append("等回落买入区")
    if "near_60d_high" in tags:
        actions.append("轻仓试探")
    if "score_inflation" in tags:
        actions.append("深入研判")
    if "generic_bullish" in tags:
        actions.append("需研报")
    if "over_recommendation" in tags:
        actions.append("降档观望")
    if "no_forward_data" in tags:
        actions.append("继续观察")
    if "forward_flat" in tags:
        actions.append("等方向")
    return "；".join(actions[:2]) if actions else "观察"


# ═══ T+1 规划（精简版）══════════════════════════════════════════════

def _build_t1_section(analyzed: list[dict[str, Any]]) -> list[str]:
    """构建「T+1 规划」段落 — 精简版卖出计划 + 继续关注 + 资金规划。"""
    lines: list[str] = []

    bought = [a for a in analyzed if a.get("buy_reached") and (a.get("pct_chg") is not None)]
    not_bought = [a for a in analyzed if not a.get("buy_reached")]

    # 已入场 → 明日操作
    if bought:
        parts = []
        for a in bought:
            name = a.get("name", "?")[:6]
            pct = a.get("pct_chg") or 0
            tgt = a.get("predicted_target")
            if pct > 3:
                action = "止盈"
            elif pct > 0:
                action = "持有"
            elif pct > -3:
                action = "观察"
            else:
                action = "⚠️减仓"
            tgt_s = f"→¥{tgt:.0f}" if tgt else ""
            parts.append(f"{name}{action}{tgt_s}")
        lines.append("📅 已入场: " + " · ".join(parts))

    # 未入场 → 继续关注
    if not_bought:
        still_valid = [a for a in not_bought if (a.get("pct_chg") or 0) < 3][:3]
        if still_valid:
            names = [a.get("name", "?")[:6] for a in still_valid]
            lines.append(f"👀 继续关注: {', '.join(names)}")

    # 资金
    bc = len(bought)
    if bc == 0:
        lines.append("💰 资金: 可用100%")
    elif bc <= 2:
        lines.append(f"💰 资金: 入场{bc}只 · 可用~40%")
    elif bc <= 5:
        lines.append(f"💰 资金: 入场{bc}只 · 可用~20%")
    else:
        lines.append(f"💰 资金: 入场{bc}只 · ⚠️只卖不买")

    return lines


# ═══ 预测验证 ═══════════════════════════════════════════════════════

def _build_verify_section(session, today: date) -> list[str]:
    """构建「昨日预测对照」段落 — 紧凑一行。"""
    try:
        mf = session.execute(
            select(MarketForecast)
            .where(
                MarketForecast.as_of_date < today,
                MarketForecast.verified_at.is_(None),
            )
            .order_by(MarketForecast.as_of_date.desc())
            .limit(1)
        ).scalars().first()

        if not mf:
            return []

        # 用上证指数代表大盘实际
        sh_row = session.execute(
            text(
                "SELECT pct_chg FROM daily_index "
                "WHERE index_code = '000001' AND trade_date = :d"
            ),
            {"d": str(today)},
        ).fetchone()

        if not sh_row or sh_row[0] is None:
            return []

        actual_pct = float(sh_row[0])
        if actual_pct > 0.3:
            actual_dir = "bullish"
        elif actual_pct < -0.3:
            actual_dir = "bearish"
        else:
            actual_dir = "range-bound"

        predicted = mf.market_direction or "?"
        matched = predicted == actual_dir

        direction_map = {"bullish": "看涨", "bearish": "看跌", "range-bound": "震荡"}
        pred_label = direction_map.get(predicted, predicted)
        actual_label = direction_map.get(actual_dir, actual_dir)

        icon = "✅" if matched else "❌"
        return [f"🎯 昨日预测: {icon} 预测{pred_label} vs 实际{actual_label}({actual_pct:+.2f}%)"]

    except Exception:
        return []


# ═══ 主入口 ══════════════════════════════════════════════════════════

def _should_skip(today: date | None) -> tuple[bool, str]:
    """周末 / 数据未就绪 跳过。"""
    if today is None:
        return True, "daily_prices 无数据"
    beijing_now = datetime.now(BEIJING_TZ)
    if beijing_now.weekday() >= 5:
        return True, "周末跳过"
    today_beijing = beijing_now.date()
    if today < today_beijing:
        return True, f"当日行情未入库（最新{today} vs 北京{today_beijing}）"
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

    # ═══ 1. 当日表现数据 ══════
    with SessionLocal() as session:
        today = _get_latest_trade_date(session)

        should_skip, skip_reason = _should_skip(today)
        if should_skip and not force:
            print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] 跳过: {skip_reason}")
            return
        elif should_skip and force:
            print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ⚠️ 强制运行（{skip_reason}）")

        entries, run, pick_date = _load_top10_from_run(session, run_id)

        if not entries:
            print("⚠️ 无选股记录，跳过综合复盘")
            return

        codes = [e.ts_code for e in entries]
        # 行业
        industry_map: dict[str, str] = {}
        rows = session.execute(
            select(StockList.ts_code, StockList.industry)
            .where(StockList.ts_code.in_(codes))
        ).all()
        industry_map = {r.ts_code: (r.industry or "") for r in rows}

        # 今日行情 + 指数（从 daily_index 秒查，不走 akshare）
        perf_map = _get_today_performance(session, codes, today) if today else {}
        index_data = _get_index_today(session, today) if today else []
        market_fallback = _compute_market_fallback(session, today) if (today and not index_data) else None

        pick_label = (
            str(pick_date) if pick_date
            else (run.trade_date_as_of.strftime("%Y-%m-%d") if run and run.trade_date_as_of else "未知")
        )
        today_label = str(today) if today else "无数据"

        # 逐只分析
        analyzed = [
            _analyze_entry(entry, perf_map.get(entry.ts_code), industry_map.get(entry.ts_code, ""))
            for entry in entries
        ]
        summary = _compute_summary(analyzed)

    # ═══ 2. 策略诊断数据（复用 backtest 逻辑）══════
    max_horizon = max(DEFAULT_HORIZONS)
    llm_eval = evaluate_llm_run(run_id=run_id, top_n=10, horizon_days=max_horizon)
    if not llm_eval.get("ok"):
        llm_eval = {"ok": False, "summary": {}, "entries": [], "tag_counts": {}, "optimization": {}}

    resolved_run_id = llm_eval.get("run_id") or (run.id if run else None)
    outcomes_by_horizon: dict[int, list[dict[str, Any]]] = {}
    for h in DEFAULT_HORIZONS:
        try:
            outcomes = list_outcomes(limit=30, horizon_days=h)
            outcomes = [o for o in outcomes if o.get("run_id") == resolved_run_id][:10]
            outcomes_by_horizon[h] = outcomes
        except Exception:
            outcomes_by_horizon[h] = []

    # ═══ 3. 预测验证 ═══
    verify_lines: list[str] = []
    if today:
        with SessionLocal() as s3:
            verify_lines = _build_verify_section(s3, today)

    # ═══ 构建综合 markdown ═══
    beijing_now = datetime.now(BEIJING_TZ)
    date_str = beijing_now.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    # 选股距今
    age_note = ""
    try:
        pick_d = date.fromisoformat(pick_label)
        td = date.fromisoformat(today_label)
        gap = (td - pick_d).days
        if gap >= 7:
            age_note = f" ⚠️选股已过{gap}天"
        elif gap > 3:
            age_note = f" 📎{gap}天前"
    except (ValueError, TypeError):
        pass

    lines = [
        f"## 📋 综合复盘 {date_str} {weekday}",
        f"> 行情日 **{today_label}** · 选股日 {pick_label}{age_note}",
        "",
        "### 📊 今日表现",
        "",
    ]

    # Section 1: 今日表现
    lines.extend(_build_today_section(analyzed, summary, index_data, market_fallback, today_label))

    # Section 2: 策略诊断
    lines.append("")
    lines.append("### 🔬 策略诊断")
    lines.append("")
    if llm_eval.get("ok") is not False:
        s = llm_eval.get("summary") or {}
        # 检查是否全是 no_forward_data（说明选股太新，还没有后续K线）
        tag_counts: dict[str, int] = llm_eval.get("tag_counts") or {}
        if tag_counts.get("no_forward_data", 0) == s.get("total", 0):
            lines.append("> ⏳ 选股日期较新，尚无足够后续 K 线用于回测验证")
        else:
            lines.extend(_build_strategy_section(llm_eval, outcomes_by_horizon, 10))
    else:
        lines.append("> ⚠️ 回测数据不足，跳过策略诊断")

    # Section 3: T+1 规划
    lines.append("")
    lines.append("### 📅 T+1 规划")
    lines.append("")
    lines.extend(_build_t1_section(analyzed))

    # 预测验证
    if verify_lines:
        lines.append("")
        lines.extend(verify_lines)

    lines.append("")
    lines.append("---")
    lines.append("💡 发送「**选股清单**」查看最新推荐 · 发送「**分析 股票名**」查看深度研报")

    markdown = "\n".join(lines)

    if dry_run:
        print("=" * 50)
        print("[DRY RUN] 综合复盘推送预览:")
        print("=" * 50)
        print(markdown)
        print("=" * 50)
        byte_count = len(markdown.encode("utf-8"))
        print(f"字节数: {byte_count} / {WECHAT_SAFE_LIMIT}")
        if byte_count > WECHAT_SAFE_LIMIT:
            print(f"⚠️ 超出企微上限 {byte_count - WECHAT_SAFE_LIMIT} 字节！")
        return

    ok = push_wechat_markdown(markdown)
    if ok:
        print(
            f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ✅ 综合复盘推送成功 "
            f"({summary.get('with_data', 0)}/{summary.get('total', 0)} 只有数据)"
        )
    else:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ❌ 综合复盘推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
