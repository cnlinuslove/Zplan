"""微信 / 企业微信：用户一句话 → 选股打分回复。"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from zplan_shared.llm.gemini import gemini_available
from zplan_shared.models import init_db
from zplan_shared.pick_store import save_report_run

logger = logging.getLogger(__name__)

from pick_agent.concept_tags import attach_concepts, concepts_for_code
from pick_agent.llm_research import _brief_review_one, format_llm_report_markdown, research_with_llm
from pick_agent.report import InsufficientBarsError, build_research_report, format_report_markdown
from pick_agent.resolve import SymbolAmbiguousError, SymbolNotFoundError, resolve_symbol
from pick_agent.strategy import load_strategy

_PICK_PREFIX = re.compile(
    r"^(选股|打分|分析|研报|评股|查股|评分)\s*[：:\s]*(.+)$",
    re.IGNORECASE,
)
_SCREEN_PREFIX = re.compile(
    r"^(筛选|题材|概念)\s*[：:\s]*(.+)$",
    re.IGNORECASE,
)
_QUESTION_MARKERS = re.compile(
    r"(怎么|如何|为什么|为何|什么|哪|吗|？|\?|最近|会不会|能不能|多少|是否|展望|影响)"
)


def _use_llm_for_wechat() -> bool:
    raw = os.getenv("PICK_WECHAT_USE_LLM", "true").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return gemini_available()


def _looks_like_symbol_only(text: str) -> bool:
    s = text.strip()
    if not s or len(s) > 16:
        return False
    if _QUESTION_MARKERS.search(s):
        return False
    if s in ("帮助", "最新", "7天", "列表"):
        return False
    return True


def parse_pick_message(message: str) -> tuple[str, str] | None:
    """
    解析用户文本 → (intent, payload)。
    intent: pick | screen
  """
    raw = (message or "").strip()
    if not raw:
        return None

    m = _PICK_PREFIX.match(raw)
    if m:
        return ("pick", m.group(2).strip())

    m = _SCREEN_PREFIX.match(raw)
    if m:
        return ("screen", m.group(2).strip())

    if _looks_like_symbol_only(raw):
        return ("pick", raw)

    return None


def _report_to_pick_row(report: dict[str, Any]) -> dict[str, Any]:
    meta = report["meta"]
    m4 = report["modules"]["4_股价分析"]
    advice = report["投资建议"]
    snap = m4.get("指标快照") or {}
    row = {
        "ts_code": meta["ts_code"],
        "name": meta.get("name"),
        "industry": meta.get("industry"),
        "close": snap.get("close"),
        "ret_20d": snap.get("ret_20d"),
        "high_60d_pct": snap.get("high_60d_pct"),
        "tech_score": m4.get("技术得分"),
        "composite_score": advice.get("综合推荐分"),
        "rule_composite_score": advice.get("综合推荐分"),
        "signals": (m4.get("关键信号") or [])[:5],
        "predicted_buy_price": advice.get("建议买入价"),
        "predicted_target_price": advice.get("目标价"),
        "predicted_stop_loss": advice.get("止损参考"),
        "news_mentions_48h": (report.get("modules") or {})
        .get("7_公司风险", {})
        .get("新闻条数_48h"),
    }
    return attach_concepts(row)


def _format_linked_news_lines(report: dict[str, Any], *, max_items: int = 3) -> list[str]:
    linked = (report.get("modules") or {}).get("8_核心竞争力", {}).get("舆情") or {}
    items = list(linked.get("items") or [])
    total = int(linked.get("total") or 0)
    fallback = int((report.get("modules") or {}).get("7_公司风险", {}).get("新闻条数_48h") or 0)
    n = total or fallback
    name = (report.get("meta") or {}).get("name") or "该股"
    if n <= 0:
        return [f"📰 近48h 库内暂无关联快讯（可问「{name} 最近新闻」）"]
    lines = [f"📰 相关资讯（48h {n} 条）"]
    shown = 0
    for item in items[:max_items]:
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        url = str(item.get("article_url") or "").strip()
        if url.startswith(("http://", "https://")):
            # 企微文本消息：明文 URL 可自动识别为可点击链接
            lines.append(f"· {title[:80]}")
            lines.append(f"  {url}")
        else:
            lines.append(f"· {title[:80]}")
        shown += 1
    if shown == 0 and n > 0:
        lines.append(f"· 库内已关联 {n} 条，标题未载入（可问「{name} 最近新闻」）")
    return lines


def _synthesize_verdict(
    report: dict[str, Any],
    llm_brief: dict[str, Any] | None,
) -> tuple[str, str]:
    """综合判断 → (verdict_label, verdict_emoji)。

    返回: ("看多", "📈") / ("看空", "📉") / ("观望", "📊")
    """
    advice = report["投资建议"]
    m4 = report["modules"]["4_股价分析"]
    tech_verdict = m4.get("技术面结论", "中性")  # 偏多/中性/偏空
    recommendation = advice.get("操作建议", "观望")  # 强烈关注/关注/观望/谨慎/回避
    rule_score = advice.get("综合推荐分", 50)

    # LLM 建议
    llm_rec = None
    if llm_brief:
        brief = llm_brief.get("llm_brief") or {}
        llm_rec = brief.get("recommendation")
    elif report.get("llm"):
        llm_rec = report["llm"].get("recommendation")

    # 多方信号权重
    bull_score = 0
    bear_score = 0

    # 技术面结论
    if tech_verdict == "偏多":
        bull_score += 3
    elif tech_verdict == "偏空":
        bear_score += 3
    # 中性不加分

    # 操作建议（权重最高）
    if recommendation == "强烈关注":
        bull_score += 3
    elif recommendation == "关注":
        bull_score += 1  # 关注 = 轻仓试探，不是强烈看多
    elif recommendation == "谨慎":
        bear_score += 2
    elif recommendation == "回避":
        bear_score += 3

    # 规则分
    if rule_score is not None:
        if rule_score >= 70:
            bull_score += 2
        elif rule_score >= 60:
            bull_score += 1
        elif rule_score <= 35:
            bear_score += 2
        elif rule_score <= 45:
            bear_score += 1

    # LLM 建议
    if llm_rec:
        if llm_rec == "强烈关注":
            bull_score += 1
        elif llm_rec == "关注":
            bull_score += 0  # LLM 关注不额外加分
        elif llm_rec in ("谨慎",):
            bear_score += 1
        elif llm_rec == "回避":
            bear_score += 2

    # ── 从信号文本中检测关键矛盾信号 ──
    sig_texts = m4.get("关键信号") or []
    for s in sig_texts:
        s = str(s)
        if any(kw in s for kw in ("空头排列", "死叉", "跌破", "破位")):
            bear_score += 1
        if any(kw in s for kw in ("多头排列", "金叉", "突破", "站上")):
            bull_score += 0.5  # 单信号权重低于结论

    # 综合判定
    if bull_score >= bear_score + 2:
        return "看多", "📈"
    elif bear_score >= bull_score + 2:
        return "看空", "📉"
    elif bull_score > bear_score:
        return "偏多", "📊"  # 略偏多但不确定
    elif bear_score > bull_score:
        return "偏空", "📊"
    else:
        return "观望", "📊"


def format_wechat_pick_text(
    report: dict[str, Any],
    *,
    llm_brief: dict[str, Any] | None = None,
    run_id: int | None = None,
    similar_patterns: dict[str, Any] | None = None,
) -> str:
    meta = report["meta"]
    advice = report["投资建议"]
    m4 = report["modules"]["4_股价分析"]
    snap = m4.get("指标快照") or {}
    name = meta.get("name") or meta["ts_code"]
    code = meta["ts_code"]

    verdict_label, verdict_emoji = _synthesize_verdict(report, llm_brief)
    rule_s = advice.get("综合推荐分")
    tech_score = m4.get("技术得分")
    tech_v = m4.get("技术面结论", "")

    # LLM 分 + 建议
    llm_score = None
    llm_rec_text = ""
    if llm_brief:
        llm_score = llm_brief.get("llm_composite_score")
        brief = llm_brief.get("llm_brief") or {}
        llm_rec_text = brief.get("recommendation", "")
    elif report.get("llm"):
        llm = report["llm"]
        llm_score = advice.get("LLM综合分") or llm.get("composite_score")
        llm_rec_text = llm.get("recommendation", "")

    rec_text = advice.get("操作建议", "")

    # 结论行 + 详细分数
    lines = [
        f"{verdict_emoji} 【{name} {code}】 综合研判：{verdict_label}",
        f"数据截止 {report.get('as_of', '—')}",
        f"规则综合分 {rule_s} | 技术面 {tech_score} ({tech_v})",
    ]
    if llm_score is not None:
        lines.append(f"LLM综合分 {llm_score} | LLM建议 {llm_rec_text}")

    # ── LLM 分析详情 ──
    if llm_brief:
        brief = llm_brief.get("llm_brief") or {}
        trend = brief.get("trend")
        vs_rule = brief.get("vs_rule_engine")
        if trend:
            lines.append(f"📊 {trend}")
        if vs_rule:
            lines.append(f"💡 {vs_rule}")
    elif report.get("llm"):
        llm = report["llm"]
        price_analysis = advice.get("LLM股价分析") or llm.get("price_trend_analysis", "")
        if price_analysis:
            lines.append(f"📊 走势 {price_analysis[:200]}")
        tech_analysis = advice.get("LLM技术面分析") or llm.get("technical_analysis", "")
        if tech_analysis:
            lines.append(f"🔧 技术 {tech_analysis[:120]}")
        fin_analysis = advice.get("LLM财务分析") or llm.get("financial_analysis", "")
        if fin_analysis:
            lines.append(f"💰 财务 {fin_analysis[:100]}")
        news_analysis = llm.get("news_analysis", "")
        if news_analysis:
            lines.append(f"📰 舆情 {news_analysis[:100]}")
        summary = advice.get("总结") or advice.get("investment_summary")
        if not price_analysis and summary:
            lines.append(f"📋 {summary[:150]}")
        risks = (report.get("modules", {}).get("7_公司风险", {}).get("风险要点")
                 or llm.get("risks") or [])
        if risks:
            lines.append(f"⚠️ 风险 {'；'.join(str(r)[:50] for r in risks[:3])}")
        opportunities = llm.get("opportunities") or []
        if opportunities:
            lines.append(f"🌟 机遇 {'；'.join(str(o)[:50] for o in opportunities[:2])}")
    else:
        summary = advice.get("总结")
        if summary:
            lines.append(f"📊 {summary[:120]}")

    # ── 基本面速览 ──
    ret20 = snap.get("ret_20d")
    if ret20 is not None:
        lines.append(f"20日涨跌 {ret20:+.2f}%")
    concepts = concepts_for_code(code, limit=4)
    if concepts:
        lines.append(f"题材 {'、'.join(concepts[:4])}")

    # ── 操作建议：看空时不展示买入价 ──
    lines.append(f"操作建议 {rec_text}")
    buy, tgt, stop = advice.get("建议买入价"), advice.get("目标价"), advice.get("止损参考")
    if verdict_label == "看空":
        # 看空不做买入建议，仅展示止损参考
        if stop is not None and rec_text in ("谨慎", "回避"):
            lines.append(f"⚠ 当前偏空，不建议买入；若已持有，止损参考 ¥{stop:.2f}")
        elif stop is not None:
            lines.append(f"止损参考 ¥{stop:.2f}（跌破离场）")
    elif verdict_label == "看多":
        if any(x is not None for x in (buy, tgt, stop)):
            parts = []
            if buy is not None:
                parts.append(f"买 ¥{buy:.2f}")
            if tgt is not None:
                parts.append(f"目标 ¥{tgt:.2f}")
            if stop is not None:
                parts.append(f"止损 ¥{stop:.2f}")
            lines.append(" / ".join(parts))
    else:
        # 观望状态展示区间
        if any(x is not None for x in (buy, tgt, stop)):
            parts = []
            if buy is not None:
                parts.append(f"回调至 ¥{buy:.2f} 可关注")
            if stop is not None:
                parts.append(f"止损 ¥{stop:.2f}")
            lines.append(" / ".join(parts))

    sig = m4.get("关键信号") or []
    if sig:
        lines.append("信号 " + "；".join(str(s) for s in sig[:3]))

    # ── 相似历史形态详情 ──
    if similar_patterns and similar_patterns.get("matches"):
        matches = similar_patterns["matches"]
        summary = similar_patterns.get("summary") or {}
        total = summary.get("total", 0)
        win = summary.get("win_count", 0)
        avg_ret = summary.get("avg_return_20d", 0)
        sim_verdict = summary.get("verdict", "")
        if total > 0:
            sim_icon = {"偏多": "📈", "偏空": "📉"}.get(sim_verdict, "📊")
            lines.append(
                f"🔍 历史相似形态 {win}/{total} 上涨 · 20日平均 {avg_ret:+.1f}% {sim_icon}"
            )
            # 列出每只匹配股票，可进一步分析
            for m in matches:
                m_name = m.get("name", m["ts_code"])
                m_code = m["ts_code"]
                m_date = m["match_date"][5:] if len(m.get("match_date", "")) >= 10 else m.get("match_date", "")
                m_fwd = m.get("forward_return_20d", 0) or 0
                m_sim = m.get("similarity", 0)
                m_icon = "✅" if m_fwd > 0 else "❌"
                lines.append(
                    f"  {m_icon} {m_name}({m_code}) {m_date} "
                    f"相似{m_sim:.0%} → 20日后 {m_fwd:+.1f}%"
                )
            lines.append("  💡 发送「选股 代码」可查看匹配股详情")

    # ── 走势应对 ──
    scenarios = advice.get("走势应对") or []
    if scenarios:
        lines.append("📋 走势应对")
        for s in scenarios[:3]:
            lines.append(f"  {str(s)[:100]}")

    lines.extend(_format_linked_news_lines(report))

    if run_id is not None:
        lines.append(f"（已入库 run_id={run_id}）")

    lines.append("—")
    lines.append("指令：选股 名称 | 筛选 题材 | 帮助")
    lines.append("📄 完整 PDF 报告已生成，含走势图+指标+分析")
    return "\n".join(lines)


def run_pick_for_symbol(
    query: str,
    *,
    use_llm: bool | None = None,
    persist: bool = True,
    skip_health_check: bool = True,
) -> dict[str, Any]:
    """单票打分（规则 + 可选 LLM 简评/深度）。"""
    init_db()
    strat = load_strategy()
    want_llm = _use_llm_for_wechat() if use_llm is None else bool(use_llm)
    # 深度研报默认开启；设 PICK_WECHAT_FULL_RESEARCH=false/0/no/off 可回退简评
    _fr_raw = os.getenv("PICK_WECHAT_FULL_RESEARCH", "").strip().lower()
    full_research = _fr_raw not in ("0", "false", "no", "off")

    code = resolve_symbol(query)
    llm_row: dict[str, Any] | None = None

    if want_llm and gemini_available() and full_research:
        report = research_with_llm(code, strategy=strat, skip_health_check=skip_health_check)
        md = format_llm_report_markdown(report)
    else:
        report = build_research_report(
            code, strategy=strat, skip_health_check=skip_health_check
        )
        md = format_report_markdown(report)
        if want_llm and gemini_available():
            llm_row = _brief_review_one(
                _report_to_pick_row(report),
                as_of=report.get("as_of"),
                model=strat.llm_model,
            )

    run_id = None
    if persist:
        run_id = save_report_run(
            report,
            symbol_query=query,
            markdown=md,
            params={"channel": "wechat", "llm": want_llm, "full_research": full_research},
            llm_enabled=bool(report.get("llm") or (want_llm and llm_row)),
            llm_model=strat.llm_model,
        )

    # ── 走势可视化 + PDF 报告（在格式化文本前完成）──
    chart_path: str | None = None
    pdf_path: str | None = None
    similar_patterns: dict[str, Any] | None = None
    price_levels: dict[str, float | None] = {}
    risk_flags: list[str] = []
    sig_list: list[str] = []
    try:
        from zplan_shared.chart_viz import plot_stock_chart
        from zplan_shared.features import suggested_price_levels
        from zplan_shared.market import get_bars as _get_bars

        bars = _get_bars(code)
        price_levels = suggested_price_levels(bars)

        # 提取风险标签和信号
        m4 = report["modules"]["4_股价分析"]
        advice = report["投资建议"]
        sig_list = list(m4.get("关键信号") or [])
        if llm_row:
            brief = llm_row.get("llm_brief") or {}
            risk_flags = brief.get("risk_flags") or []
        elif report.get("llm"):
            risk_flags = (report.get("modules", {}).get("7_公司风险", {}).get("风险要点") or [])[:3]

        # 走势明确时搜索相似历史形态（使用综合研判结论）
        synth_verdict, _ = _synthesize_verdict(report, llm_row)
        if synth_verdict in ("看多", "看空", "偏多", "偏空"):
            from zplan_shared.pattern_similarity import find_similar_patterns
            similar_patterns = find_similar_patterns(code, as_of=report.get("as_of"))

        chart_path = plot_stock_chart(
            code,
            price_levels=price_levels,
            risk_flags=risk_flags if risk_flags else None,
            signals=sig_list if sig_list else None,
            similar_patterns=similar_patterns,
        )

        # ── 生成 PDF 报告 ──
        from zplan_shared.report_pdf import generate_pdf_report
        pdf_path = generate_pdf_report(
            code,
            report=report,
            llm_brief=llm_row,
            chart_path=chart_path,
            price_levels=price_levels,
            similar_patterns=similar_patterns,
            risk_flags=risk_flags if risk_flags else None,
        )
    except Exception:
        logger.warning("走势图/PDF 生成失败", exc_info=True)

    text = format_wechat_pick_text(
        report,
        llm_brief=llm_row,
        run_id=run_id,
        similar_patterns=similar_patterns,
    )

    return {
        "ok": True,
        "intent": "pick_symbol",
        "ts_code": code,
        "name": report["meta"].get("name"),
        "reply_text": text,
        "report": report,
        "run_id": run_id,
        "chart_path": chart_path,
        "pdf_path": pdf_path,
        "similar_patterns": similar_patterns,
    }


def run_screen_for_concept(concept_query: str, *, limit: int = 12) -> dict[str, Any]:
    from pick_agent.screen import run_screen

    init_db()
    result = run_screen(concept=concept_query)
    df = result.get("dataframe")
    if df is None or df.empty:
        return {
            "ok": True,
            "intent": "pick_screen",
            "reply_text": (
                f"题材「{concept_query}」无匹配标的。\n"
                "可先在本机执行：main.py screen sync-concept <概念名>"
            ),
        }

    lines = [f"【题材筛选 · {concept_query}】共 {len(df)} 只，示例如下："]
    show = df.head(limit)
    for _, row in show.iterrows():
        parts = [f"{row.get('name', '')}({row['ts_code']})"]
        rs = row.get("rule_composite_score")
        if rs is not None and str(rs) != "nan":
            try:
                parts.append(f"规则{float(rs):.0f}")
            except (TypeError, ValueError):
                pass
        pct = row.get("pct_chg_today")
        if pct is not None and str(pct) != "nan":
            try:
                parts.append(f"今涨{float(pct):.2f}%")
            except (TypeError, ValueError):
                pass
        lines.append(" · ".join(parts))
    if len(df) > limit:
        lines.append(f"... 另有 {len(df) - limit} 只")
    lines.append("\n单票打分：选股 爱普股份")
    return {"ok": True, "intent": "pick_screen", "reply_text": "\n".join(lines), "count": len(df)}


def handle_pick_message(message: str) -> dict[str, Any]:
    """供 zplan-资讯 wechat_interact 调用。"""
    parsed = parse_pick_message(message)
    if not parsed:
        return {"ok": False, "intent": "pick_skip"}

    intent, payload = parsed
    try:
        if intent == "screen":
            return run_screen_for_concept(payload)
        return run_pick_for_symbol(payload)
    except SymbolAmbiguousError as exc:
        return {
            "ok": True,
            "intent": "pick_ambiguous",
            "reply_text": str(exc),
            "matches": exc.matches,
        }
    except SymbolNotFoundError as exc:
        return {
            "ok": True,
            "intent": "pick_not_found",
            "reply_text": str(exc) + "\n示例：选股 爱普股份 或 603020",
        }
    except InsufficientBarsError as exc:
        return {
            "ok": True,
            "intent": "pick_no_bars",
            "reply_text": str(exc),
        }


def handle_wechat_pick_message(message: str) -> dict[str, Any] | None:
    """若可识别为选股意图则返回回复 dict，否则 None。"""
    if parse_pick_message(message) is None:
        return None
    out = handle_pick_message(message)
    if not out.get("ok") or out.get("intent") == "pick_skip":
        return None
    return out
