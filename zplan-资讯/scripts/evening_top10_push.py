#!/usr/bin/env python3
"""盘后 TOP10 选股推送：涨跌幅 + 距高 + 推荐理由 + 操作建议。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/evening_top10_push.py
    cd zplan-资讯 && .venv/bin/python scripts/evening_top10_push.py --dry-run

调度: run_full_pipeline.sh 在大盘预测之后调用。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NEWS_ROOT))

from sqlalchemy import desc, select, text

from zplan_shared.models import (
    DailyPrice,
    MarketForecast,
    PickEntry,
    PickRun,
    SessionLocal,
    StockConceptMember,
    StockList,
    init_db,
)
from wechat_push import push_wechat_markdown

BEIJING_TZ = timezone(timedelta(hours=8))
WECHAT_SAFE_LIMIT = 3800

_SKIP_CONCEPTS = {
    "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
    "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
    "央国企改革", "黑龙江", "深圳特区", "机械设备", "通信", "电子", "计算机",
    "公用事业", "电力", "基础化工", "化学制品", "元件", "通信技术", "通信设备",
}


def _load_top10(session) -> tuple[list[dict[str, Any]], str, int | None]:
    """加载最新 TOP10 + 行业/概念/PE/市值 + 行情数据 + 推荐理由。"""
    run = session.execute(
        select(PickRun)
        .where(PickRun.run_kind.in_(["scan", "llm_top300"]))
        .order_by(desc(PickRun.created_at_utc))
        .limit(1)
    ).scalars().first()

    if not run:
        return [], "", None

    entries = session.execute(
        select(PickEntry)
        .where(PickEntry.run_id == run.id)
        .order_by(PickEntry.rank_in_run, PickEntry.final_composite_score.desc().nullslast())
        .limit(10)
    ).scalars().all()

    if not entries:
        return [], "", None

    as_of = (
        run.trade_date_as_of.strftime("%Y-%m-%d")
        if run.trade_date_as_of
        else run.created_at_utc.strftime("%Y-%m-%d %H:%M")
    )

    codes = [e.ts_code for e in entries]
    as_of_date = run.trade_date_as_of

    # ── 行情数据：今日涨跌 + 20日涨跌 + 60日最高 ──
    price_data: dict[str, dict] = {}
    if as_of_date and codes:
        d = as_of_date.strftime("%Y-%m-%d")
        placeholders = ",".join(f":c{i}" for i in range(len(codes)))
        params = {f"c{i}": c for i, c in enumerate(codes)}
        params["d"] = d
        rows = session.execute(
            text(
                f"SELECT dp.ts_code, dp.close, dp.pct_chg, "
                f"  dp_prev.close AS prev_close, "
                f"  dp_t20.close AS close_20d_ago, "
                f"  (SELECT MAX(high) FROM daily_prices WHERE ts_code=dp.ts_code "
                f"    AND trade_date BETWEEN DATE(:d, '-250 days') AND :d) AS high_1y "
                f"FROM daily_prices dp "
                f"LEFT JOIN daily_prices dp_prev ON dp.ts_code = dp_prev.ts_code "
                f"  AND dp_prev.trade_date = (SELECT MAX(trade_date) FROM daily_prices "
                f"    WHERE ts_code=dp.ts_code AND trade_date < :d AND market='a') "
                f"LEFT JOIN daily_prices dp_t20 ON dp.ts_code = dp_t20.ts_code "
                f"  AND dp_t20.trade_date = (SELECT MAX(trade_date) FROM daily_prices "
                f"    WHERE ts_code=dp.ts_code AND trade_date <= DATE(:d, '-20 days') AND market='a') "
                f"WHERE dp.ts_code IN ({placeholders}) AND dp.trade_date = :d"
            ),
            params,
        ).fetchall()
        for r in rows:
            close = float(r[1]) if r[1] is not None else None
            pct_chg = float(r[2]) if r[2] is not None else None
            prev_close = float(r[3]) if r[3] is not None else None
            close_20d = float(r[4]) if r[4] is not None else None
            high_1y = float(r[5]) if r[5] is not None else None

            # 今日涨跌自算（pct_chg 为 NULL 时）
            if pct_chg is None and close is not None and prev_close and prev_close > 0:
                pct_chg = round((close - prev_close) / prev_close * 100, 2)

            # 20日涨跌
            ret_20d = None
            if close is not None and close_20d is not None and close_20d > 0:
                ret_20d = round((close / close_20d - 1) * 100, 2)

            # 距高
            pct_from_high = None
            if close is not None and high_1y is not None and high_1y > 0:
                pct_from_high = round((close - high_1y) / high_1y * 100, 2)

            price_data[r[0]] = {
                "pct_chg": pct_chg,
                "ret_20d": ret_20d,
                "high_1y": high_1y,
                "pct_from_high": pct_from_high,
            }

    # ── 行业 ──
    industry_map: dict[str, str] = {}
    rows = session.execute(
        select(StockList.ts_code, StockList.industry)
        .where(StockList.ts_code.in_(codes))
    ).all()
    industry_map = {r.ts_code: (r.industry or "") for r in rows}

    # ── PE / 市值 ──
    pe_map: dict[str, float] = {}
    mv_map: dict[str, float] = {}
    latest_snap = session.execute(text("SELECT MAX(trade_date) FROM daily_snapshot")).scalar()
    if latest_snap and codes:
        placeholders = ",".join(f":c{i}" for i in range(len(codes)))
        params = {f"c{i}": c for i, c in enumerate(codes)}
        params["d"] = str(latest_snap)
        rows = session.execute(
            text(
                f"SELECT ts_code, pe_ttm, total_mv FROM daily_snapshot "
                f"WHERE ts_code IN ({placeholders}) AND trade_date = :d"
            ),
            params,
        ).fetchall()
        for r in rows:
            if r[1] is not None:
                pe_map[r[0]] = float(r[1])
            if r[2] is not None:
                mv_map[r[0]] = float(r[2])

    # ── 概念 ──
    concept_map: dict[str, list[str]] = {}
    rows = session.execute(
        select(StockConceptMember.ts_code, StockConceptMember.concept_name)
        .where(StockConceptMember.ts_code.in_(codes))
    ).all()
    for r in rows:
        if r[1] not in _SKIP_CONCEPTS:
            concept_map.setdefault(r[0], []).append(r[1])

    # ── 组装 ──
    items = []
    for e in entries:
        code = e.ts_code
        pd = price_data.get(code, {})
        # 推荐理由：优先 LLM brief，fallback 到 verdict + 概念
        reason = _extract_reason(e)
        items.append({
            "ts_code": code, "name": e.name or code,
            "score": e.final_composite_score or e.rule_composite_score,
            "close": e.close_price,
            "verdict": e.verdict or "",
            "predicted_buy": e.predicted_buy_price,
            "predicted_target": e.predicted_target_price,
            "industry": industry_map.get(code, ""),
            "pe_ttm": pe_map.get(code),
            "total_mv": mv_map.get(code),
            "concepts": concept_map.get(code, [])[:2],
            "pct_chg": pd.get("pct_chg"),
            "ret_20d": pd.get("ret_20d"),
            "high_1y": pd.get("high_1y"),
            "pct_from_high": pd.get("pct_from_high"),
            "reason": reason,
        })

    return items, as_of, run.id


def _extract_reason(entry: PickEntry) -> str:
    """从 LLM 分析中提取推荐理由（精简至 15 字以内）。"""
    # 优先 LLM brief
    try:
        if entry.analysis_process_json:
            data = json.loads(entry.analysis_process_json) if isinstance(entry.analysis_process_json, str) else entry.analysis_process_json
            brief = data.get("llm_brief", {})
            if isinstance(brief, dict):
                trend = brief.get("trend", "").strip()
                if trend:
                    return trend[:18]
                # 尝试 tech / sector 等字段
                tech = brief.get("technical", "").strip()
                if tech:
                    return tech[:18]
                sector = brief.get("sector_view", "").strip()
                if sector:
                    return sector[:18]
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Fallback: verdict
    v = (entry.verdict or "").strip()
    if v:
        return v

    return ""


def _get_forecast_direction(session) -> str:
    """获取最新大盘预测方向。"""
    mf = session.execute(
        select(MarketForecast)
        .order_by(desc(MarketForecast.created_at_utc))
        .limit(1)
    ).scalars().first()
    if mf:
        return mf.market_direction or ""
    return ""


def _format_markdown(entries: list[dict], as_of: str, direction: str) -> str:
    """格式化 TOP10 + 操作建议 markdown。"""
    beijing_now = datetime.now(BEIJING_TZ)
    date_str = beijing_now.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    lines = [
        f"## 📈 今日 TOP10 {date_str} {weekday}",
        f"> 数据截止 **{as_of}**",
        "",
    ]

    if not entries:
        lines.append("⚠️ 暂无选股数据")
        return "\n".join(lines)

    # ── TOP10 表格：含涨跌幅 + 距高 ──
    lines.append("| # | 股票 | 评分 | 今% | 20日% | PE | 收盘 | 距高% | 买入 |")
    lines.append("|---|------|------|------|-------|----|------|-------|------|")
    for i, item in enumerate(entries):
        name = (item.get("name") or item.get("ts_code", "?"))[:8]
        code = item.get("ts_code", "?")
        score = item.get("score")
        score_s = f"{score:.0f}" if score is not None else "--"
        pct = item.get("pct_chg")
        pct_s = f"{pct:+.1f}%" if pct is not None else "--"
        r20 = item.get("ret_20d")
        r20_s = f"{r20:+.1f}%" if r20 is not None else "--"
        pe = item.get("pe_ttm")
        pe_s = f"{pe:.0f}" if (pe is not None and pe > 0) else "--"
        close = item.get("close")
        close_s = f"¥{close:.2f}" if close is not None else "--"
        ph = item.get("pct_from_high")
        ph_s = f"{ph:+.0f}%" if ph is not None else "--"
        buy = item.get("predicted_buy")
        buy_s = f"¥{buy:.2f}" if (buy is not None and buy > 0) else "--"
        lines.append(f"| {i+1} | {name}({code}) | {score_s} | {pct_s} | {r20_s} | {pe_s} | {close_s} | {ph_s} | {buy_s} |")

    lines.append("")

    # ── 逐只推荐理由 ──
    for i, item in enumerate(entries):
        parts = []
        # 推荐理由（核心）
        reason = item.get("reason", "")
        if reason:
            parts.append(f"💡{reason}")
        # 概念
        concepts = item.get("concepts", [])
        if concepts:
            parts.append("🏷 " + "·".join(concepts[:2]))
        # 距高提示
        ph = item.get("pct_from_high")
        if ph is not None and ph < -15:
            parts.append(f"📉距高{ph:.0f}%")
        # 20日动量提示
        r20 = item.get("ret_20d")
        if r20 is not None and r20 > 10:
            parts.append(f"🔥20日+{r20:.0f}%")

        if parts:
            lines.append(f"**{i+1}. {item.get('name', '?')}** {' · '.join(parts)}")
        else:
            # 至少给行业
            industry = item.get("industry", "")
            lines.append(f"**{i+1}. {item.get('name', '?')}** 📌{industry}" if industry else f"**{i+1}. {item.get('name', '?')}**")

    lines.append("")

    # ── 操作建议 ──
    direction_map = {
        "bullish": ("🟢 大盘偏多", "可以积极选股，优先强势板块", "建议买入价: 昨收×0.98"),
        "bearish": ("🔴 大盘偏空", "⚠️ 建议观望，等止跌信号", "若要入场: 买入价收紧到昨收×0.96"),
        "range-bound": ("🟡 大盘震荡", "仓位≤5成，等回调不追高", "建议买入价: 昨收×0.99"),
    }
    d_info = direction_map.get(direction, ("❓ 方向不明", "等待更明确信号", ""))

    lines.append("### 💡 操作建议")
    lines.append("")
    lines.append(f"**{d_info[0]}** → {d_info[1]}")
    lines.append(f"- {d_info[2]}")

    # 低PE标的
    low_pe = [e for e in entries if e.get("pe_ttm") and e["pe_ttm"] > 0 and e["pe_ttm"] < 30]
    if low_pe:
        names = [f"{e['name']}(PE{e['pe_ttm']:.0f})" for e in low_pe[:3]]
        lines.append(f"- 低PE防御: {', '.join(names)}")

    # 强势标的（今日涨）
    up_stocks = [e for e in entries if e.get("pct_chg") is not None and e["pct_chg"] > 0]
    if up_stocks:
        names = [f"{e['name']}(+{e['pct_chg']:.1f}%)" for e in up_stocks[:3]]
        lines.append(f"- 今日逆势: {', '.join(names)}")

    lines.append("- 止损: 个股支撑位下方 1-2%")
    lines.append("")
    # ── 一键研报指令 ──
    lines.append("---")
    lines.append("### 📊 一键深度研报")
    lines.append("")
    # 分两排，每排 5 只（企微 markdown 不支持按钮，用清晰指令替代）
    cmd_parts = []
    for item in entries:
        name = item.get("name", "?")
        cmd_parts.append(f"**分析 {name}**")
    # 每 5 个一行
    for i in range(0, len(cmd_parts), 5):
        lines.append("> " + " · ".join(cmd_parts[i:i+5]))
    lines.append("")
    lines.append("👆 复制上方指令发送到群中，即可生成对应个股的 **完整研报 PDF**")

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv

    init_db()

    with SessionLocal() as session:
        entries, as_of, run_id = _load_top10(session)
        direction = _get_forecast_direction(session)

    if not entries:
        print("⚠️ 无选股数据")
        return

    markdown = _format_markdown(entries, as_of, direction)

    if dry_run:
        print("=" * 50)
        print("[DRY RUN] TOP10 推送预览:")
        print("=" * 50)
        print(markdown)
        print("=" * 50)
        print(f"字节数: {len(markdown.encode('utf-8'))} / {WECHAT_SAFE_LIMIT}")
        return

    ok = push_wechat_markdown(markdown)
    if ok:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ✅ 晚间 TOP10 推送成功 ({len(entries)} 只)")
    else:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ❌ 推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
