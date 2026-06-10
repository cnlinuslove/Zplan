"""盘前检查 — T 日 8:28 触发，检查隔夜外盘/新闻并调整买入价。

用法:
    from execution.pre_market import run_pre_market_check
    result = run_pre_market_check(top_n=10, dry_run=True)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from execution.plan import ExecutionPlan, load_latest_picks

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))


# ── 隔夜大盘数据拉取 ──

def _fetch_overnight_markets() -> dict[str, Any]:
    """拉取隔夜外盘快照（美股期货、A50、汇率等）。

    失败时返回空 dict，不阻断流程。
    """
    result: dict[str, Any] = {
        "us_futures": None,    # 美股期货
        "a50_futures": None,   # 富时A50期货
        "usd_cny": None,       # 离岸人民币
        "global_sentiment": "", # 整体情绪
        "fetched_at": datetime.now(BEIJING_TZ).isoformat(),
    }

    try:
        import akshare as ak

        # 1. 美股三大指数（用东财全球指数接口）
        try:
            df = ak.index_global_spot_em()
            if df is not None and not df.empty:
                us_keywords = ["纳斯达克", "道琼斯", "标普500"]
                us_rows = df[df["名称"].str.contains("|".join(us_keywords), na=False)]
                futures = []
                for _, row in us_rows.iterrows():
                    name = str(row.get("名称", ""))
                    price = row.get("最新价")
                    pct = row.get("涨跌幅")
                    if price is not None:
                        futures.append({
                            "name": name,
                            "price": float(price),
                            "pct_chg": float(pct) if pct is not None else None,
                        })
                if futures:
                    result["us_futures"] = futures
        except Exception:
            logger.debug("美股指数拉取失败", exc_info=True)

        # 2. 富时 A50 期货（东财）
        try:
            df_a50 = ak.futures_foreign_hist(symbol="CN")
            if df_a50 is not None and not df_a50.empty:
                last = df_a50.iloc[-1]
                result["a50_futures"] = {
                    "price": float(last.get("close", last.get("收盘", 0))),
                    "pct_chg": float(last.get("pct_chg", last.get("涨跌幅", 0)) or 0),
                }
        except Exception:
            logger.debug("A50期货拉取失败", exc_info=True)

        # 3. 离岸人民币（USD/CNH）
        try:
            df_fx = ak.currency_boc_sina(symbol="美元")
            if df_fx is not None and not df_fx.empty:
                last = df_fx.iloc[-1]
                result["usd_cny"] = {
                    "rate": float(last.get("close", last.get("收盘价", 0))),
                    "pct_chg": float(last.get("pct_chg", 0) or 0),
                }
        except Exception:
            logger.debug("汇率拉取失败", exc_info=True)

    except ImportError:
        logger.warning("akshare 未安装，跳过隔夜大盘数据")
    except Exception:
        logger.warning("隔夜大盘数据拉取失败", exc_info=True)

    # ── 整体情绪判断 ──
    sentiment_score = 0  # 正=偏多，负=偏空
    notes = []

    us = result.get("us_futures") or []
    if us:
        avg_pct = sum(f.get("pct_chg", 0) or 0 for f in us) / len(us)
        if avg_pct > 1.0:
            sentiment_score += 2
            notes.append(f"美股期货普涨 +{avg_pct:.1f}%")
        elif avg_pct > 0.3:
            sentiment_score += 1
            notes.append(f"美股期货微涨 +{avg_pct:.1f}%")
        elif avg_pct < -1.0:
            sentiment_score -= 2
            notes.append(f"美股期货普跌 {avg_pct:.1f}%")
        elif avg_pct < -0.3:
            sentiment_score -= 1
            notes.append(f"美股期货微跌 {avg_pct:.1f}%")

    a50 = result.get("a50_futures")
    if a50:
        a50_pct = a50.get("pct_chg", 0) or 0
        if a50_pct > 0.5:
            sentiment_score += 1
            notes.append(f"A50期货涨 +{a50_pct:.1f}%")
        elif a50_pct < -0.5:
            sentiment_score -= 1
            notes.append(f"A50期货跌 {a50_pct:.1f}%")

    fx = result.get("usd_cny")
    if fx:
        fx_pct = fx.get("pct_chg", 0) or 0
        if fx_pct > 0.2:  # 人民币贬值
            sentiment_score -= 1
            notes.append(f"人民币贬值 {fx_pct:+.2f}%")
        elif fx_pct < -0.2:  # 人民币升值
            sentiment_score += 1
            notes.append(f"人民币升值 {fx_pct:+.2f}%")

    if sentiment_score >= 2:
        result["global_sentiment"] = "偏多"
    elif sentiment_score <= -2:
        result["global_sentiment"] = "偏空"
    elif sentiment_score > 0:
        result["global_sentiment"] = "略偏多"
    elif sentiment_score < 0:
        result["global_sentiment"] = "略偏空"
    else:
        result["global_sentiment"] = "中性"

    result["sentiment_score"] = sentiment_score
    result["sentiment_notes"] = notes

    return result


# ── 个股隔夜新闻检查 ──

def _check_overnight_news(ts_code: str) -> list[str]:
    """查询某只股票最近 12h 的关联新闻，返回要点列表。"""
    from sqlalchemy import text
    from zplan_shared.models import SessionLocal, init_db

    init_db()
    cutoff = datetime.now(BEIJING_TZ) - timedelta(hours=12)
    notes: list[str] = []

    try:
        with SessionLocal() as session:
            rows = session.execute(
                text(
                    """SELECT gn.title, gn.source_name, gn.published_at_utc, nsl.event_type
                       FROM news_stock_link nsl
                       JOIN global_news gn ON nsl.news_id = gn.id AND nsl.news_source = 'global_news'
                       WHERE nsl.ts_code = :code
                         AND gn.published_at_utc >= :cutoff
                       ORDER BY gn.published_at_utc DESC
                       LIMIT 5"""
                ),
                {"code": ts_code, "cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S")},
            ).fetchall()

        for r in rows:
            title = str(r[0] or "").strip()
            source = str(r[1] or "")
            event = str(r[3] or "")
            if title:
                tag = f"[{event}] " if event else ""
                notes.append(f"{tag}{title[:80]}")

    except Exception:
        logger.debug("新闻查询失败: %s", ts_code, exc_info=True)

    return notes


# ── 买入价调整 ──

def _adjust_buy_price(
    plan: ExecutionPlan,
    market_sentiment: dict[str, Any],
    news_notes: list[str],
) -> tuple[float | None, float, list[str]]:
    """根据隔夜情绪和个股新闻调整建议买入价。

    Returns:
        (adjusted_buy, adjustment_pct, notes)
    """
    if plan.predicted_buy is None:
        return None, 0.0, ["无建议买入价，无法调整"]

    sentiment_score = market_sentiment.get("sentiment_score", 0)
    adjustment = 0.0
    reasons = []

    # 大盘情绪调整
    if sentiment_score >= 2:
        adjustment += 0.005  # +0.5%
        reasons.append("外盘偏多，买入价上调0.5%")
    elif sentiment_score <= -2:
        adjustment -= 0.015  # -1.5%
        reasons.append("外盘偏空，买入价下调1.5%")
    elif sentiment_score >= 1:
        adjustment += 0.003  # +0.3%
    elif sentiment_score <= -1:
        adjustment -= 0.01  # -1.0%

    # 个股新闻调整
    has_negative = any(
        kw in str(n).lower()
        for n in news_notes
        for kw in ["减持", "亏损", "处罚", "监管", "退市", "诉讼", "暴雷", "跌停"]
    )
    has_positive = any(
        kw in str(n).lower()
        for n in news_notes
        for kw in ["增持", "回购", "中标", "业绩预增", "分红", "涨停", "突破"]
    )

    if has_negative:
        adjustment -= 0.02  # -2%
        reasons.append("⚠️ 隔夜有利空新闻，买入价下调2%")
    if has_positive:
        adjustment += 0.01  # +1%
        reasons.append("隔夜有利好新闻，买入价上调1%")

    # 限制调整范围
    adjustment = max(-0.03, min(0.02, adjustment))
    adjusted_buy = round(plan.predicted_buy * (1 + adjustment), 2)

    if not reasons:
        reasons.append("买入价维持不变")

    return adjusted_buy, adjustment, reasons


# ── 主入口 ──

def run_pre_market_check(
    top_n: int = 10,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行盘前检查，返回结构化结果。"""
    beijing_now = datetime.now(BEIJING_TZ)
    today_str = beijing_now.strftime("%Y-%m-%d")
    weekday = beijing_now.weekday()

    # 周末跳过
    if weekday >= 5:
        return {
            "ok": True,
            "skipped": True,
            "reason": f"周末跳过（{['周一','周二','周三','周四','周五','周六','周日'][weekday]}）",
            "date": today_str,
        }

    # 1. 加载最新 picks
    plans = load_latest_picks(top_n=top_n)
    if not plans:
        return {
            "ok": False,
            "error": "无选股数据，请先运行选股流水线",
            "date": today_str,
        }

    # 2. 隔夜大盘
    market = _fetch_overnight_markets()

    # 3. 逐只检查
    for plan in plans:
        # 新闻
        news_notes = _check_overnight_news(plan.ts_code)
        plan.pre_market_notes = news_notes

        # 调整买入价
        adj_buy, adj_pct, adj_reasons = _adjust_buy_price(plan, market, news_notes)
        plan.overnight_adjustment = adj_pct
        plan.adjusted_buy = adj_buy
        if adj_reasons:
            plan.pre_market_notes = adj_reasons + plan.pre_market_notes

    # 4. 格式化输出
    markdown = _format_pre_market_markdown(plans, market, today_str)

    return {
        "ok": True,
        "date": today_str,
        "plans": plans,
        "market": market,
        "markdown": markdown,
    }


# ── Markdown 格式化 ──

def _format_pre_market_markdown(
    plans: list[ExecutionPlan],
    market: dict[str, Any],
    today_str: str,
) -> str:
    """生成盘前简报 markdown。"""
    beijing_now = datetime.now(BEIJING_TZ)
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]
    time_str = beijing_now.strftime("%H:%M")

    sentiment = market.get("global_sentiment", "未知")
    sentiment_icon = {"偏多": "🟢", "偏空": "🔴", "略偏多": "🟡", "略偏空": "🟠"}.get(sentiment, "⚪")

    lines = [
        f"## 🌅 Z-Plan 盘前简报",
        f"> {today_str} {weekday} {time_str} · 隔夜情绪 {sentiment_icon} **{sentiment}**",
        "",
    ]

    # ── 隔夜大盘 ──
    us = market.get("us_futures") or []
    a50 = market.get("a50_futures")
    fx = market.get("usd_cny")

    if us or a50 or fx:
        lines.append("### 🌍 隔夜外盘")
        lines.append("")
        if us:
            for f in us:
                name = f["name"]
                price = f.get("price", "--")
                pct = f.get("pct_chg")
                arrow = "🔺" if (pct or 0) > 0 else ("🔻" if (pct or 0) < 0 else "➖")
                pct_s = f"{arrow} {pct:+.2f}%" if pct is not None else "--"
                lines.append(f"- {name}: {price} {pct_s}")
        if a50:
            a50_pct = a50.get("pct_chg", 0) or 0
            a50_arrow = "🔺" if a50_pct > 0 else ("🔻" if a50_pct < 0 else "➖")
            lines.append(f"- 富时A50期货: {a50.get('price', '--')} {a50_arrow} {a50_pct:+.2f}%")
        if fx:
            fx_pct = fx.get("pct_chg", 0) or 0
            fx_arrow = "🔺" if fx_pct > 0 else ("🔻" if fx_pct < 0 else "➖")
            lines.append(f"- 美元/人民币: {fx.get('rate', '--')} {fx_arrow} {fx_pct:+.2f}%")
        lines.append("")

    # 情绪说明
    notes = market.get("sentiment_notes") or []
    if notes:
        lines.append("> " + " · ".join(notes))
        lines.append("")

    # ── 今日操作清单（整合选股排行）──
    lines.append("### 📋 今日操作清单")
    lines.append("")
    lines.append("| # | 标的 | 昨收 | 评分 | 买入参考 | 建议 | 推荐理由 |")
    lines.append("|---|------|------|------|----------|------|----------|")

    for p in plans:
        name = p.name or p.ts_code
        code = p.ts_code
        close_s = f"¥{p.close_yesterday:.2f}" if p.close_yesterday else "--"

        # 买入参考：优先调整后价格；与原始建议价差异 >0.1% 时标 *
        predicted = p.predicted_buy
        adjusted = p.adjusted_buy
        if adjusted is not None and predicted is not None and abs(adjusted - predicted) / predicted > 0.001:
            buy_s = f"¥{adjusted:.2f}*"
        elif adjusted is not None:
            buy_s = f"¥{adjusted:.2f}"
        elif predicted is not None:
            buy_s = f"¥{predicted:.2f}"
        else:
            buy_s = "--"

        # 评分
        score_val = p.final_score
        score_s = f"{score_val:.0f}" if score_val is not None else "--"

        rec = p.recommendation or "--"
        rec_icon = {"强烈关注": "🔥", "关注": "👀", "观望": "⏸️", "谨慎": "⚠️", "回避": "🚫"}.get(rec, "")

        # ── 推荐理由：信号(看涨理由) + 风险(注意事项) ──
        reason_parts: list[str] = []

        # 正面信号（选股理由）
        if p.signals:
            for sig in p.signals[:2]:
                short = str(sig)
                # 统一缩写
                for full, abbr in [
                    ("多头排列", "多头排列"), ("金叉", "金叉"), ("站上均线", "站上均线"),
                    ("放量突破", "放量突破"), ("MACD底背离", "底背离"), ("KDJ超卖", "KDJ超卖"),
                    ("缩量回调", "缩量回调"), ("均线粘合", "均线粘合"),
                ]:
                    if full in short:
                        short = abbr
                        break
                if len(short) > 6:
                    short = short[:6]
                reason_parts.append(short)

        # LLM 走势简评（无信号时作为兜底理由）
        if not reason_parts and p.llm_trend:
            trend_short = p.llm_trend[:20]
            reason_parts.append(trend_short)

        # 风险标签
        if p.risk_flags:
            for rf in p.risk_flags[:2]:
                short = rf.replace("追高风险(涨幅过高)", "追高").replace("量价背离(缩量上涨)", "量价背离") \
                          .replace("接近阶段高点", "近高点").replace("超买区域(KDJ/RSI)", "超买")
                if len(short) > 6:
                    short = short[:6]
                reason_parts.append(f"⚠{short}")

        reason_str = " · ".join(reason_parts) if reason_parts else "--"

        lines.append(
            f"| {p.rank} | {rec_icon} **{name}**({code}) | {close_s} | {score_s} | {buy_s} | {rec} | {reason_str} |"
        )

    # 买入参考列注释
    lines.append("")
    lines.append("> 💡 买入参考 = 盘前调整后的建议挂单价（`*` 表示隔夜情绪/新闻调整过）")
    lines.append("")

    # ── 个股盘前备注 ──
    lines.append("### 🔔 盘前关注")
    lines.append("")
    has_alert = False
    for p in plans:
        if p.pre_market_notes and any(
            "⚠️" in n or "减持" in n or "亏损" in n or "监管" in n
            for n in p.pre_market_notes
        ):
            has_alert = True
            lines.append(f"**{p.name}({p.ts_code})** ⚠️")
            for note in p.pre_market_notes[:3]:
                lines.append(f"  - {note}")
            lines.append("")

    if not has_alert:
        lines.append("> ✅ 推荐标的中无重大隔夜利空")

    # ── 买卖提前量提醒 ──
    lines.append("")
    lines.append("### ⏰ 时间线")
    lines.append("")
    lines.append("| 时间 | 动作 |")
    lines.append("|------|------|")
    lines.append("| **9:25** | 集合竞价快照，对比建议买入价 |")
    lines.append("| **9:30** | 开盘决策：BUY / WAIT / SKIP |")
    lines.append("| **10:00** | 早盘量价确认 |")
    lines.append("| **14:00** | 尾盘检查 |")
    lines.append("")
    lines.append("---")
    lines.append("💡 竞价前做好**限价单准备**：调整买入价 = 你的挂单价参考")
    lines.append("💡 开盘涨超 2% → 不建议追，等回调或放弃今日操作")

    return "\n".join(lines)
