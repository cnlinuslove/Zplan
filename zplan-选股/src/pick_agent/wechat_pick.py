"""微信 / 企业微信：用户一句话 → 选股打分回复。"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from zplan_shared.llm.gemini import llm_available as _llm_available

gemini_available = _llm_available  # 向下兼容别名
from zplan_shared.models import init_db
from zplan_shared.pick_store import save_report_run
from zplan_shared.market import get_realtime_quote

logger = logging.getLogger(__name__)

from pick_agent.concept_tags import attach_concepts, concepts_for_code
from pick_agent.llm_research import _brief_review_one, format_llm_report_markdown, research_with_llm
from pick_agent.report import InsufficientBarsError, build_research_report, format_report_markdown
from pick_agent.resolve import SymbolAmbiguousError, SymbolNotFoundError, resolve_symbol
from pick_agent.strategy import load_strategy

def _use_llm_for_wechat() -> bool:
    raw = os.getenv("PICK_WECHAT_USE_LLM", "true").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return gemini_available()


_PICK_KEYWORDS = ("选股", "打分", "分析", "研报", "评股", "查股", "评分")
_SCREEN_KEYWORDS = ("筛选", "题材", "概念")

_FILLER_RE = re.compile(r"^[一下呀哦啊的了吗个这那帮给请麻烦\s]+")

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


def _strip_command_keywords(text: str) -> str | None:
    """从任意位置剥离已知指令词，返回纯股票名。

    "爱普股份选股" → "爱普股份"
    "选股 爱普股份" → "爱普股份"（已有 prefix 处理，此作兜底）
    "帮我分析一下爱普股份" → "爱普股份"（正则提取中文名/代码 + 分析关键词）
    "爱普股份 打分" → "爱普股份"
    """
    raw = text.strip()
    all_keywords = _PICK_KEYWORDS + _SCREEN_KEYWORDS

    # 1) 尾部指令词："爱普股份选股" / "爱普股份 分析"
    for kw in sorted(all_keywords, key=len, reverse=True):
        if raw.endswith(kw):
            name = raw[: -len(kw)].strip()
            if name:
                return name

    # 2) 头部指令词无分隔符："分析爱普股份"（前缀正则已处理有分隔符的情况）
    for kw in sorted(all_keywords, key=len, reverse=True):
        if raw.startswith(kw):
            after = raw[len(kw):].strip()
            after = re.sub(r"^[：:\s]+", "", after)
            if after and not after.startswith(tuple(all_keywords)):
                return after

    # 3) 指令词在中间："帮我分析一下爱普股份" → 提取"分析"后的中文名
    for kw in sorted(all_keywords, key=len, reverse=True):
        if kw in raw:
            # 找到 kw 的位置
            idx = raw.index(kw)
            after = raw[idx + len(kw):].strip()
            after = re.sub(r"^[：:\s一下呀哦啊的了吗]+", "", after)
            # "爱普股份" 类：2-4 个中文字或 6 位数字代码
            m = re.match(r"([一-鿿]{2,4}|\d{5,6})", after)
            if m:
                return m.group(1)
            # 或者 kw 之前的文本像是股票名
            before = raw[:idx].strip()
            before = re.sub(r"[帮给请麻烦]|[一下呀哦啊的了]", "", before)
            m2 = re.match(r"([一-鿿]{2,4}|\d{5,6})$", before)
            if m2:
                return m2.group(1)

    # 4) "XXX 选股" 尾部（已由 1 覆盖，此作冗余兜底）
    for kw in sorted(all_keywords, key=len, reverse=True):
        pattern = re.compile(r"(.{2,8})\s*" + re.escape(kw) + r"\s*$")
        m = pattern.search(raw)
        if m:
            name = m.group(1).strip()
            if name and not name.startswith(tuple(all_keywords)):
                return name

    return None


# 常见非股票名短文本，不应路由到选股
_NON_SYMBOL_WORDS = frozenset({
    "你好", "谢谢", "帮助", "最新", "7天", "列表", "退出", "结束",
    "选股", "打分", "分析", "研报", "查股", "评分", "筛选", "题材",
    "是的", "好的", "可以", "不行", "再见", "测试",
    "北向资金", "南向资金", "两融余额", "大盘", "指数", "涨停", "跌停",
    "龙虎榜", "机构", "游资", "主力", "外资", "融资融券", "美联储",
    "央行", "降息", "加息", "GDP", "CPI", "PMI",
})


def _looks_like_symbol_only(text: str) -> bool:
    s = text.strip()
    if not s or len(s) > 16:
        return False
    if _QUESTION_MARKERS.search(s):
        return False
    if s in _NON_SYMBOL_WORDS:
        return False
    return True


def parse_pick_message(message: str) -> tuple[str, str] | None:
    """
    解析用户文本 → (intent, payload)。
    intent: pick | screen

    支持多种自然语言变体：
    - "选股 爱普股份" / "爱普股份选股" / "分析 爱普股份" → pick
    - "筛选 脑机接口" / "脑机接口筛选" → screen
    - "爱普股份" / "603020" → pick
    """
    raw = (message or "").strip()
    if not raw:
        return None

    # 1) 精确前缀匹配
    m = _PICK_PREFIX.match(raw)
    if m:
        payload = _FILLER_RE.sub("", m.group(2).strip())
        return ("pick", payload) if payload else None

    m = _SCREEN_PREFIX.match(raw)
    if m:
        payload = _FILLER_RE.sub("", m.group(2).strip())
        return ("screen", payload) if payload else None

    # 2) 模糊匹配：从任意位置剥离指令词
    name = _strip_command_keywords(raw)
    if name:
        # 判断原句中含 pick 还是 screen 关键词
        if any(kw in raw for kw in _SCREEN_KEYWORDS):
            return ("screen", name)
        return ("pick", name)

    # 3) 纯股票名/代码
    if _looks_like_symbol_only(raw):
        return ("pick", raw)

    return None
    raw = os.getenv("PICK_WECHAT_USE_LLM", "true").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return gemini_available()


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


def _format_company_section(report: dict[str, Any]) -> list[str]:
    """从 report 中提取公司简介行（≤4 行，企微消息尺寸适配）。

    优先级：LLM 公司摘要 > company_products LLM调研 > 公司档案定位 > 核心产品
    """
    modules = report.get("modules") or {}
    m1 = modules.get("1_基本信息") or {}
    m2 = modules.get("2_核心产品") or {}
    m8 = modules.get("8_核心竞争力") or {}
    meta = report.get("meta") or {}

    positioning = m1.get("公司定位", "")
    if positioning and (positioning.startswith("待") or positioning == ""):
        positioning = ""

    # LLM 深度模式下的公司摘要
    llm_summary = m8.get("公司摘要") or ""

    # rule 模式下的核心产品
    core_products_raw = m2.get("核心产品") or ""
    if isinstance(core_products_raw, str) and core_products_raw.startswith("{"):
        try:
            import json as _json
            products = _json.loads(core_products_raw)
            biz = products.get("主营业务", "")
            scope = products.get("经营范围", "")
            core_products_raw = biz if biz else (scope[:80] + "..." if len(scope) > 80 else scope)
        except Exception:
            pass
    if core_products_raw and core_products_raw.startswith("待"):
        core_products_raw = ""

    result = []

    # 优先展示 LLM 公司摘要
    if llm_summary and str(llm_summary).strip():
        result.append(f"🏢 {str(llm_summary)[:150]}")
    elif positioning:
        result.append(f"🏢 {str(positioning)[:150]}")
    elif core_products_raw:
        result.append(f"🏢 {str(core_products_raw)[:150]}")

    # 尝试从 company_products 加载产品信息
    ts_code = meta.get("ts_code", "")
    if ts_code:
        try:
            from zplan_shared.enrich_company import _load_company_products, _get_db
            db = _get_db()
            try:
                cp = _load_company_products(db, ts_code)
                if cp:
                    # LLM 调研的竞争定位（简版）
                    llm_pos = cp.get("competitive_positioning")
                    if isinstance(llm_pos, str) and llm_pos.strip():
                        result.append(f"🎯 {llm_pos.strip()[:120]}")
                    # 产品名列表（截取前 5 个）
                    product_names = cp.get("product_names")
                    if isinstance(product_names, str) and product_names.strip():
                        names = [n.strip() for n in product_names.replace("、", "，").split("，") if n.strip()]
                        top5 = names[:5]
                        if top5:
                            result.append(f"📦 核心产品：{' · '.join(top5)}")
            finally:
                db.close()
        except Exception:
            pass

    # 官网
    website = m1.get("官网")
    if website and str(website).strip():
        result.append(f"🔗 {str(website).strip()[:100]}")

    return result[:5]


def format_wechat_pick_text(
    report: dict[str, Any],
    *,
    llm_brief: dict[str, Any] | None = None,
    run_id: int | None = None,
    similar_patterns: dict[str, Any] | None = None,
    realtime: dict[str, Any] | None = None,
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
    ]
    # ── 实时行情（交易时段显示）──
    if realtime and realtime.get("price") is not None:
        rt_price = realtime["price"]
        rt_pct = realtime.get("pct_chg") or 0
        rt_arrow = "🔺" if rt_pct > 0 else ("🔻" if rt_pct < 0 else "➖")
        rt_turn = realtime.get("turnover_rate")
        rt_vol = realtime.get("volume")
        rt_parts = [f"⏱ 实时 ¥{rt_price:.2f}  {rt_arrow}{rt_pct:+.2f}%"]
        if rt_turn is not None:
            rt_parts.append(f"换手 {rt_turn:.2f}%")
        if rt_vol is not None:
            if rt_vol >= 1e8:
                rt_parts.append(f"成交 {rt_vol/1e8:.1f}亿")
            elif rt_vol >= 1e4:
                rt_parts.append(f"成交 {rt_vol/1e4:.0f}万手")
        lines.append("  ".join(rt_parts))
    lines.append(f"数据截止 {report.get('as_of', '—')}")
    lines.append(f"规则综合分 {rule_s} | 技术面 {tech_score} ({tech_v})")
    if llm_score is not None:
        lines.append(f"LLM综合分 {llm_score} | LLM建议 {llm_rec_text}")

    # ── 公司简介（关键：主营业务 + 核心产品）──
    company_lines = _format_company_section(report)
    if company_lines:
        lines.extend(company_lines)

    # ── LLM 分析详情 ──
    if llm_brief:
        brief = llm_brief.get("llm_brief") or {}
        trend = brief.get("trend")
        vs_rule = brief.get("vs_rule_engine")
        if trend:
            lines.append(f"📊 {trend}")
        if vs_rule:
            lines.append(f"💡 {vs_rule}")
        # 简评模式也附加链接资讯
        lines.extend(_format_linked_news_lines(report))
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
        # 链接资讯（含可点击 URL）紧接舆情区，避免被尾部截断
        linked_news_lines = _format_linked_news_lines(report)
        has_linked_urls = any("http" in ln for ln in linked_news_lines)
        news_analysis = llm.get("news_analysis", "")
        # 有可点击链接 → 优先展示链接；仅有 LLM 摘要 → 展示摘要 + 兜底行
        if has_linked_urls:
            lines.extend(linked_news_lines)
        else:
            if news_analysis and "空" not in str(news_analysis) and "缺失" not in str(news_analysis):
                lines.append(f"📰 舆情 {news_analysis[:100]}")
            lines.extend(linked_news_lines)
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
        # 非 LLM 路径：链接资讯紧跟核心结论
        lines.extend(_format_linked_news_lines(report))

    # ── 基本面速览 ──
    ret20 = snap.get("ret_20d")
    if ret20 is not None:
        lines.append(f"20日涨跌 {ret20:+.2f}%")

    # 筹码峰速览
    chip_ctx = report.get("modules", {}).get("4_股价分析", {}).get("筹码分布") or {}
    if chip_ctx.get("available"):
        pr = chip_ctx.get("profit_ratio")
        conc = chip_ctx.get("concentration_90")
        cost = chip_ctx.get("avg_cost")
        parts = []
        if pr is not None:
            label = "获利" if pr >= 50 else "套牢"
            parts.append(f"获利盘{pr:.0f}%")
        if conc is not None:
            conc_label = "集中" if conc < 0.15 else ("较集中" if conc < 0.3 else "分散")
            parts.append(f"筹码{conc_label}")
        if cost is not None:
            parts.append(f"均本¥{cost:.2f}")
        if parts:
            lines.append(f"筹码 {' | '.join(parts)}")

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

        chart_paths = plot_stock_chart(
            code,
            price_levels=price_levels,
            risk_flags=risk_flags if risk_flags else None,
            signals=sig_list if sig_list else None,
            similar_patterns=similar_patterns,
        )
        chart_path = chart_paths["kline"] if isinstance(chart_paths, dict) else chart_paths
        chart_macd_path = chart_paths.get("macd") if isinstance(chart_paths, dict) else None

        # ── 生成 PDF 报告 ──
        from zplan_shared.report_pdf import generate_pdf_report
        pdf_path = generate_pdf_report(
            code,
            report=report,
            llm_brief=llm_row,
            chart_path=chart_path,
            chart_macd_path=chart_macd_path,
            price_levels=price_levels,
            similar_patterns=similar_patterns,
            risk_flags=risk_flags if risk_flags else None,
        )
    except Exception:
        logger.warning("走势图/PDF 生成失败", exc_info=True)

    # ── 实时行情（交易时段）──
    realtime = get_realtime_quote(code)

    text = format_wechat_pick_text(
        report,
        llm_brief=llm_row,
        run_id=run_id,
        similar_patterns=similar_patterns,
        realtime=realtime,
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
        "chart_macd_path": chart_macd_path,
        "pdf_path": pdf_path,
        "similar_patterns": similar_patterns,
    }


def run_screen_for_concept(concept_query: str, *, limit: int = 10) -> dict[str, Any]:
    from pick_agent.screen import run_screen
    from zplan_shared.stock_rule_scores import latest_score_date

    init_db()
    try:
        # 合并规则打分，按综合分排序
        result = run_screen(concept=concept_query, min_rule_score=30)
    except Exception as exc:
        logger.warning("题材筛选网络异常: %s", exc)
        from zplan_shared.concept_screen import list_cached_concepts
        cached = list_cached_concepts(keyword=concept_query, limit=5)
        if cached:
            return {
                "ok": True,
                "intent": "pick_screen_hint",
                "reply_text": (
                    f"网络异常，无法更新题材数据。\n"
                    f"已缓存的相关题材: {'、'.join(cached[:5])}\n"
                    f"发送「筛选 题材名」可查看已缓存的题材"
                ),
            }
        return {
            "ok": True,
            "intent": "pick_screen_error",
            "reply_text": (
                f"题材「{concept_query}」暂未缓存，且网络异常无法在线获取。\n"
                "请稍后重试，或在电脑上执行：main.py screen sync-concept " + concept_query
            ),
        }

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

    # 按综合分降序排列，先取 top 30 宽池
    score_col = "rule_composite_score"
    if score_col in df.columns:
        df = df.sort_values(score_col, ascending=False, na_position="last")

    total = len(df)
    # 题材筛选用全量成分股做池子（不做规则分截断），靠 LLM 相关度区分
    pool = df  # 全量：让三博脑科（规则35分）也能靠相关度冲进 TOP10

    strat = load_strategy()
    sd = latest_score_date(rule_version=strat.rule_version)
    score_date = str(sd) if sd else "最新"

    # 批量获取 ret_20d + 信号 + PE/市值（宽池）
    top_codes = tuple(pool["ts_code"].values)
    ret_map: dict[str, float | None] = {}
    sig_map: dict[str, list[str]] = {}
    pe_map: dict[str, float | None] = {}
    mv_map: dict[str, float | None] = {}
    import json as _json

    try:
        from zplan_shared.models import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as s:
            placeholders = ",".join(f":c{i}" for i in range(len(top_codes)))
            params = {f"c{i}": c for i, c in enumerate(top_codes)}
            params["d"] = sd
            params["v"] = strat.rule_version
            rows = s.execute(
                text(
                    f"SELECT ts_code, signals_json, features_json FROM stock_rule_scores "
                    f"WHERE ts_code IN ({placeholders}) AND trade_date_as_of = :d AND rule_version = :v"
                ),
                params,
            ).fetchall()
        for r in rows:
            code = r[0]
            if r[1]:
                try:
                    sig_map[code] = _json.loads(r[1])
                except Exception:
                    pass
            if r[2]:
                try:
                    feats = _json.loads(r[2])
                    ret20 = feats.get("ret_20d")
                    if ret20 is not None:
                        ret_map[code] = float(ret20)
                except Exception:
                    pass

        # 批量获取 PE + 市值
        sd_str = str(sd)
        with SessionLocal() as s:
            rows2 = s.execute(
                text(
                    f"SELECT ts_code, pe_ttm, total_mv FROM daily_snapshot "
                    f"WHERE ts_code IN ({placeholders}) AND trade_date = :d"
                ),
                {**{f"c{i}": c for i, c in enumerate(top_codes)}, "d": sd_str},
            ).fetchall()
        for r in rows2:
            code = r[0]
            if r[1] is not None:
                pe_map[code] = float(r[1])
            if r[2] is not None:
                mv_map[code] = float(r[2])
    except Exception:
        logger.warning("获取扩展数据失败", exc_info=True)

    # ── 产品摘要（LLM 批量 + 缓存，含相关度评分）──
    product_map: dict[str, str] = {}
    relevance_map: dict[str, int] = {}
    try:
        from zplan_shared.concept_product import (
            generate_product_summaries,
            get_concept_relevance_scores,
        )
        name_map = {}
        for _, row in pool.iterrows():
            name_map[str(row["ts_code"])] = str(row.get("name", ""))
        # 先看缓存里有相关度分吗，没有就 force_refresh
        existing_rel = get_concept_relevance_scores(list(top_codes), concept_query)
        need_refresh = any(c not in existing_rel for c in top_codes)
        product_map = generate_product_summaries(
            list(top_codes), concept_query,
            names=name_map,
            force_refresh=need_refresh,
        )
        relevance_map = get_concept_relevance_scores(list(top_codes), concept_query)
    except Exception:
        logger.warning("产品摘要获取失败", exc_info=True)

    # ── 综合排序：规则分(50%) + LLM 概念相关度(50%) ──
    pool = pool.copy()
    pool["llm_relevance"] = pool["ts_code"].apply(
        lambda c: relevance_map.get(str(c), 50)  # 默认 50（中性）
    )
    pool["concept_composite"] = pool.apply(
        lambda r: (
            float(r.get(score_col, 0) or 0) * 0.5
            + float(r.get("llm_relevance", 50)) * 0.5
        ),
        axis=1,
    )
    pool = pool.sort_values("concept_composite", ascending=False, na_position="last")
    show = pool.head(limit)

    lines = [
        f"【题材筛选 · {concept_query}】",
        f"共 {total} 只  |  综合排名（规则分×0.5 + LLM概念相关度×0.5）  |  数据 {score_date}",
        "",
    ]

    for i, (_, row) in enumerate(show.iterrows()):
        name = row.get("name", "") or str(row["ts_code"])
        code = str(row["ts_code"])
        score = row.get(score_col)
        score_val = int(float(score)) if score is not None and str(score) != "nan" else None
        close = row.get("close_price")
        verdict = row.get("verdict", "")
        verdict_str = str(verdict) if verdict and str(verdict) != "nan" else ""

        # 价格 + 建议买入（收盘价 × 0.995，0.5% T+1 滑点缓冲）
        price_str = f"¥{float(close):.2f}" if close is not None and str(close) != "nan" else ""
        buy_price = round(float(close) * 0.995, 2) if close is not None and str(close) != "nan" and float(close) > 0 else None
        # 20日涨跌
        ret20 = ret_map.get(code)
        ret20_str = f"20日{ret20:+.1f}%" if ret20 is not None else ""
        # PE + 市值
        pe = pe_map.get(code)
        pe_str = f"PE {pe:.1f}" if pe is not None and pe > 0 else ""
        mv = mv_map.get(code)
        if mv is not None and mv > 0:
            if mv >= 1e8:
                mv_str = f"市值{mv/1e8:.0f}亿"
            else:
                mv_str = f"市值{mv/1e4:.0f}万"
        else:
            mv_str = ""
        # 信号
        sigs = sig_map.get(code, [])
        sig_str = " · ".join(str(s) for s in sigs[:2]) if sigs else ""

        # 第一行：排名. 名(码) ¥价 · 综合分(规则+LLM相关度) · 建议买入
        meta = []
        rule_val = score_val
        rel_val = row.get("llm_relevance")
        comp_val = row.get("concept_composite")
        rel_str_display = f"概念相关度{int(rel_val)}" if rel_val is not None and str(rel_val) != "nan" else ""
        if comp_val is not None and str(comp_val) != "nan":
            meta.append(f"综合{float(comp_val):.0f}分")
        if rule_val is not None:
            meta.append(f"规则{rule_val}")
        if verdict_str:
            meta.append(verdict_str)
        if price_str:
            meta.append(price_str)
        if buy_price:
            meta.append(f"建议买入 ¥{buy_price}")
        line1 = f"{i+1:2d}. {name}({code})  {' · '.join(meta)}"

        # 第二行：PE / 市值 / 20日走势 / 概念相关度
        detail = []
        if pe_str:
            detail.append(pe_str)
        if mv_str:
            detail.append(mv_str)
        if ret20_str:
            detail.append(ret20_str)
        if rel_str_display:
            detail.append(rel_str_display)
        line2 = f"    { '  |  '.join(detail) }" if detail else ""

        # 第三行：信号
        line3 = f"    {sig_str}" if sig_str else ""

        # 第四行：产品摘要
        product = product_map.get(code, "")
        line4 = f"    🏷 {product}" if product else ""

        lines.append(line1)
        if line2:
            lines.append(line2)
        if line3:
            lines.append(line3)
        if line4:
            lines.append(line4)

    if total > limit:
        lines.append(f"    ... 另有 {total - limit} 只")
    lines.append("")
    lines.append("发送「选股 名称或代码」可查看个股完整分析报告")
    return {"ok": True, "intent": "pick_screen", "reply_text": "\n".join(lines), "count": total}


def _try_concept_route(query: str) -> dict[str, Any] | None:
    """尝试将 query 匹配为概念/题材筛选。匹配到则返回筛选结果，否则 None。"""
    from zplan_shared.concept_screen import list_cached_concepts, resolve_alias

    # 0) 别名优先："脑机接口" → "人脑工程"，直接筛选无需重输
    aliased = resolve_alias(query)
    if aliased:
        concepts = list_cached_concepts(keyword=aliased, limit=5)
        exact = [c for c in concepts if c == aliased]
        if exact:
            return run_screen_for_concept(exact[0])
        if concepts:
            return run_screen_for_concept(concepts[0])

    # 1) 精确匹配
    concepts = list_cached_concepts(keyword=query, limit=20)
    exact = [c for c in concepts if c == query]
    if exact:
        return run_screen_for_concept(exact[0])

    # 2) 包含匹配
    if concepts:
        return run_screen_for_concept(concepts[0])

    # 3) 模糊匹配：尝试拆分关键词，搜索相似概念
    if len(query) >= 2:
        tried = set()
        for sub_len in range(len(query) - 1, 0, -1):
            for start in range(len(query) - sub_len + 1):
                sub = query[start:start + sub_len]
                if sub in tried or len(sub) < 1:
                    continue
                tried.add(sub)
                partial_hits = list_cached_concepts(keyword=sub, limit=10)
                if partial_hits:
                    return {
                        "ok": True,
                        "intent": "pick_screen_hint",
                        "reply_text": (
                            f"未找到题材「{query}」，但找到相关题材：\n"
                            + "\n".join(f"  · {c}" for c in partial_hits[:8])
                            + f"\n\n发送「筛选 题材名」可查看详情"
                        ),
                    }

    return None


def _llm_route_intent(query: str) -> dict[str, Any]:
    """用 LLM 理解用户意图，路由到对应功能。"""
    if not gemini_available():
        return {
            "ok": True,
            "intent": "pick_unknown",
            "reply_text": (
                f"不太确定你的意思。「{query}」似乎不是股票名或题材名。\n"
                "试试：选股 爱普股份 | 筛选 脑机接口 | 查 北向资金"
            ),
        }

    from zplan_shared.llm.gemini import generate_json

    prompt = (
        "你是一个股票分析助手的意图路由器。用户输入了一句话，你需要判断用户的意图并提取关键信息。\n\n"
        "支持的意图类型：\n"
        "- pick: 用户想分析某只股票 → 提取股票名称或代码\n"
        "- screen: 用户想筛选某个题材/概念/行业的股票 → 提取题材名\n"
        "- news: 用户想查询新闻/资讯/快讯 → 提取查询关键词\n"
        "- help: 用户需要帮助或指引\n\n"
        f"用户输入: {query}\n\n"
        "请返回 JSON: {\"intent\": \"pick|screen|news|help\", \"target\": \"目标名\", \"reply\": \"如果不确定，给用户的引导语\"}"
    )

    try:
        result = generate_json(
            prompt=prompt,
            response_schema={
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["pick", "screen", "news", "help"]},
                    "target": {"type": "string"},
                    "reply": {"type": "string"},
                },
                "required": ["intent", "target"],
            },
            model=None,  # 用默认模型
        )
        intent = result.get("intent", "help")
        target = result.get("target", query)
        guide = result.get("reply", "")

        if intent == "screen":
            screen_result = _try_concept_route(target)
            if screen_result:
                return screen_result
            return {
                "ok": True,
                "intent": "pick_screen",
                "reply_text": f"题材「{target}」暂未缓存。\n可先在本机执行：main.py screen sync-concept {target}",
            }
        elif intent == "pick":
            try:
                return run_pick_for_symbol(target)
            except SymbolNotFoundError:
                return {
                    "ok": True,
                    "intent": "pick_not_found",
                    "reply_text": f"未找到「{target}」。{guide or '请确认股票名或代码'}\n示例：选股 爱普股份 或 603020",
                }
            except SymbolAmbiguousError as exc:
                return {
                    "ok": True,
                    "intent": "pick_ambiguous",
                    "reply_text": str(exc),
                    "matches": exc.matches,
                }
        elif intent == "news":
            return {
                "ok": True,
                "intent": "info_query",
                "reply_text": f"请发送「{target} 新闻」查询相关资讯。\n或者发送「最新」查看今日快讯。",
            }
        else:
            return {
                "ok": True,
                "intent": "pick_help",
                "reply_text": guide or f"不太确定你的意思。试试：\n选股 爱普股份 | 筛选 脑机接口 | 查 北向资金",
            }
    except Exception:
        logger.warning("LLM 意图路由失败", exc_info=True)
        return {
            "ok": True,
            "intent": "pick_unknown",
            "reply_text": (
                f"不太确定你的意思。「{query}」似乎不是股票名或题材名。\n"
                "试试：选股 爱普股份 | 筛选 脑机接口 | 查 北向资金"
            ),
        }


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
    except SymbolNotFoundError:
        # ── 第一层 fallback：尝试概念/题材匹配 ──
        concept_result = _try_concept_route(payload)
        if concept_result:
            return concept_result

        # ── 第二层 fallback：LLM 理解意图并路由 ──
        return _llm_route_intent(payload)
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
