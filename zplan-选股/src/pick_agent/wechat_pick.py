"""微信 / 企业微信：用户一句话 → 选股打分回复。"""
from __future__ import annotations

import os
import re
from typing import Any

from zplan_shared.llm.gemini import gemini_available
from zplan_shared.models import init_db
from zplan_shared.pick_store import save_report_run

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
        return [f"相关资讯：近48h 库内暂无关联快讯（可问「{name} 最近新闻」）"]
    lines = [f"相关资讯（48h {n} 条）"]
    shown = 0
    for item in items[:max_items]:
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        url = str(item.get("article_url") or "").strip()
        if url.startswith(("http://", "https://")):
            lines.append(f"· [{title[:56]}]({url})")
        else:
            lines.append(f"· {title[:72]}")
        shown += 1
    if shown == 0 and n > 0:
        lines.append(f"· 库内已关联 {n} 条，标题未载入（可问「{name} 最近新闻」）")
    return lines


def format_wechat_pick_text(
    report: dict[str, Any],
    *,
    llm_brief: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> str:
    meta = report["meta"]
    advice = report["投资建议"]
    m4 = report["modules"]["4_股价分析"]
    snap = m4.get("指标快照") or {}
    name = meta.get("name") or meta["ts_code"]
    code = meta["ts_code"]

    lines = [
        f"【{name} {code}】",
        f"数据截止 {report.get('as_of', '—')}",
    ]
    rule_s = advice.get("综合推荐分")
    lines.append(f"规则综合分 {rule_s} | 技术面 {m4.get('技术得分')} ({m4.get('技术面结论')})")

    if llm_brief:
        llm_s = llm_brief.get("llm_composite_score")
        if llm_s is not None:
            lines.append(f"LLM综合分 {llm_s}")
        trend = (llm_brief.get("llm_brief") or {}).get("trend")
        rec = (llm_brief.get("llm_brief") or {}).get("recommendation")
        if trend:
            lines.append(f"简评 {trend}")
        if rec:
            lines.append(f"LLM建议 {rec}")
    elif report.get("llm"):
        llm = report["llm"]
        lines.append(f"LLM综合分 {advice.get('LLM综合分', llm.get('composite_score'))}")
        if llm.get("recommendation"):
            lines.append(f"LLM建议 {llm['recommendation']}")

    ret20 = snap.get("ret_20d")
    if ret20 is not None:
        lines.append(f"20日涨跌 {ret20:+.2f}%")
    concepts = concepts_for_code(code, limit=4)
    if concepts:
        lines.append(f"题材 {'、'.join(concepts[:4])}")

    lines.append(f"操作建议 {advice.get('操作建议')}")
    buy, tgt, stop = advice.get("建议买入价"), advice.get("目标价"), advice.get("止损参考")
    if any(x is not None for x in (buy, tgt, stop)):
        lines.append(f"买/目标/止 {buy} / {tgt} / {stop}")

    sig = m4.get("关键信号") or []
    if sig:
        lines.append("信号 " + "；".join(str(s) for s in sig[:3]))

    lines.extend(_format_linked_news_lines(report))

    if run_id is not None:
        lines.append(f"（已入库 run_id={run_id}）")

    lines.append("—")
    lines.append("指令：选股 名称 | 筛选 题材 | 帮助")
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
    full_research = os.getenv("PICK_WECHAT_FULL_RESEARCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

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

    text = format_wechat_pick_text(
        report,
        llm_brief=llm_row,
        run_id=run_id,
    )

    return {
        "ok": True,
        "intent": "pick_symbol",
        "ts_code": code,
        "name": report["meta"].get("name"),
        "reply_text": text,
        "report": report,
        "run_id": run_id,
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
