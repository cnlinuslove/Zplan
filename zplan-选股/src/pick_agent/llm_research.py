"""LLM 驱动的个股深度研究与打分（默认 DeepSeek，可通过 .env 切换模型）。"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

from zplan_shared.config import DEEPSEEK_MODEL, GEMINI_MODEL, LLM_MODEL
from zplan_shared.llm.gemini import (
    LLMError,
    generate_json,
    llm_available,
    pop_usage,
)
# 模块内向下兼容别名
GeminiError = LLMError
gemini_available = llm_available
generate_json_with_gemini = generate_json

# 实际使用的 LLM 模型
_LLM_MODEL = LLM_MODEL or DEEPSEEK_MODEL or GEMINI_MODEL
from zplan_shared.market import get_bars

from pick_agent.concept_tags import concepts_for_code
from pick_agent.report import build_research_report, format_report_markdown
from pick_agent.strategy import PickStrategy, load_strategy

try:
    from zplan_shared.enrich_company import build_enrich_prompt_section as _build_enrich_section
except ImportError:
    _build_enrich_section = None


_RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "price_trend_analysis": {
            "type": "string",
            "description": "近期股价走势深度分析：趋势、支撑阻力、量价、与板块对比；必须引用提供的数据日期与价格。",
        },
        "technical_analysis": {
            "type": "string",
            "description": "KDJ/MACD/均线/RSI 等技术面对买入价值的判断。",
        },
        "technical_score": {"type": "number", "description": "技术面打分 0-100"},
        "financial_analysis": {"type": "string"},
        "financial_score": {"type": "number"},
        "news_analysis": {"type": "string", "description": "资讯与舆情；无新闻则说明数据缺失"},
        "news_score": {"type": "number"},
        "company_summary": {"type": "string", "description": "公司定位、核心业务（数据不足时明确说明）"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "opportunities": {"type": "array", "items": {"type": "string"}},
        "investment_summary": {"type": "string"},
        "composite_score": {"type": "number", "description": "综合投资推荐分 0-100"},
        "recommendation": {
            "type": "string",
            "enum": ["强烈关注", "关注", "观望", "谨慎", "回避"],
        },
        "buy_price": {"type": "number"},
        "target_price": {"type": "number"},
        "stop_loss": {"type": "number"},
        "scenarios": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-4 条不同走势应对策略",
        },
        "data_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "当前数据不足以支撑的判断，需补充的数据源",
        },
    },
    "required": [
        "price_trend_analysis",
        "technical_analysis",
        "technical_score",
        "financial_analysis",
        "financial_score",
        "news_analysis",
        "news_score",
        "investment_summary",
        "composite_score",
        "recommendation",
        "buy_price",
        "target_price",
        "stop_loss",
        "scenarios",
    ],
}


def _enrich_block(ts_code: str) -> str:
    """从 enrich_company 表拉取 P0+P1 数据，拼成 prompt 注入块。"""
    if _build_enrich_section is None:
        return ""
    try:
        section = _build_enrich_section(str(ts_code))
        if section.strip():
            return f"\n【深度数据：公司档案/行业对比/机构研报/持仓】\n{section}\n"
    except Exception:
        pass
    return ""


def _bars_table(bars, n: int = 30) -> list[dict[str, Any]]:
    tail = bars.tail(n)
    rows = []
    for dt, row in tail.iterrows():
        rows.append(
            {
                "date": str(dt)[:10],
                "open": round(float(row["open"]), 4) if row.get("open") == row.get("open") else None,
                "high": round(float(row["high"]), 4) if row.get("high") == row.get("high") else None,
                "low": round(float(row["low"]), 4) if row.get("low") == row.get("low") else None,
                "close": round(float(row["close"]), 4),
                "pct_chg": round(float(row["pct_chg"]), 4)
                if "pct_chg" in row and row["pct_chg"] == row["pct_chg"]
                else None,
                "volume": float(row["volume"]) if "volume" in row and row["volume"] == row["volume"] else None,
                "turnover_rate": round(float(row["turnover_rate"]), 4)
                if "turnover_rate" in row and row["turnover_rate"] == row["turnover_rate"]
                else None,
            }
        )
    return rows


def _build_prompt(base_report: dict[str, Any], bars_table: list[dict[str, Any]]) -> str:
    meta = base_report["meta"]
    m4 = base_report["modules"]["4_股价分析"]
    m5 = base_report["modules"]["5_财务情况"]
    m7 = base_report["modules"]["7_公司风险"]
    advice = base_report["投资建议"]
    linked = base_report["modules"].get("8_核心竞争力", {}).get("舆情") or {}

    concepts = concepts_for_code(str(meta["ts_code"]))
    ctx = {
        "股票": f"{meta.get('name')} ({meta['ts_code']})",
        "行业": meta.get("industry"),
        "概念题材": concepts[:8] if concepts else [],
        "上市日期": meta.get("listing_date"),
        "数据截止": base_report.get("as_of"),
        "规则引擎技术分": m4.get("技术得分"),
        "规则引擎综合分": advice.get("综合推荐分"),
        "技术指标快照": m4.get("指标快照"),
        "规则信号": m4.get("关键信号"),
        "分时特征": m4.get("分时特征"),
        "近30日K线": bars_table,
        "财务记录": m5.get("近三年记录"),
        "财务评语": m5.get("评语"),
        "估值截面": m5.get("估值截面"),
        "关联新闻摘要": linked,
        "规则建议买卖价": {
            "buy": advice.get("建议买入价"),
            "target": advice.get("目标价"),
            "stop": advice.get("止损参考"),
        },
    }

    return f"""你是一名 A 股上市公司研究报告员，风格**偏题材催化、厌恶追高**。请**严格基于下方 JSON 数据**分析，不得编造未给出的财务数字、新闻标题、概念名或股价。

若某模块数据为空或标注「待扩展」，须在对应分析与 data_gaps 中说明，可结合行业常识做**定性**推断但须标注「推断，非库内事实」。

【量化与资讯上下文】
{json.dumps(ctx, ensure_ascii=False, indent=2)}

{_enrich_block(meta["ts_code"])}
【任务】
1. **题材**：有「概念题材」则 price_trend_analysis / investment_summary 须点明核心题材与催化逻辑；列表为空则写明「库内无概念标签」，不得编造。
2. **股价走势**：结合近 30 日 K 线；若 ret_20d 偏高（指标快照）须写清追高风险，不得只写看多。
3. **技术面**：解读 KDJ、MACD、RSI、均线，给出 technical_score（0-100）；超买或近 60 日高位须降分。
4. **财务**：有数据则评营收/利润/估值；无数据则 financial_score 取 50 并说明。如有行业对比数据，须在 financial_analysis 中引用行业排名和中位数做同业比较。
5. **资讯**：解读关联新闻与事件类型；无新闻则 news_score 取 50。
6. **竞争力与产品**：如有【公司档案/行业对比/研报/机构持仓】数据，须在 company_summary 中分析公司核心产品与竞争壁垒，在 opportunities 中引用券商评级和盈利预测，在 risks 中标注机构持仓变化风险。
7. **综合打分 composite_score**（0-100）与 recommendation（五选一）；追高风险时 composite 不得高于规则引擎综合分。
8. buy_price 必须在 **[close×0.98, close×1.0]** 区间内。A 股 T+1 场景下，次日开盘正常滑点约 0-2%，买入价最多只应低于收盘价 2%。若规则引擎给的「规则建议买卖价」中 suggested_buy 在此区间，**必须直接采用**。禁止设定低于 close×0.98 的买入价（回测验证此类买价 100% unreachable）。target ≥ buy_price×1.05；stop 须与支撑位一致。
9. scenarios：基准/回调/破位/突发利空等 3-4 条可执行策略。
10. 输出合法 JSON，字段符合 schema；中文撰写。"""


def research_with_llm(
    ts_code: str,
    *,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
) -> dict[str, Any]:
    """规则引擎打底 + LLM 深度研究与打分。"""
    if not gemini_available():
        raise GeminiError(
            "未配置 DEEPSEEK_API_KEY。请在 zplan-资讯/.env 设置 DEEPSEEK_API_KEY"
        )

    strat = strategy or load_strategy()
    base = build_research_report(
        ts_code,
        strategy=strat,
        skip_health_check=skip_health_check,
    )
    code = base["meta"]["ts_code"]
    bars = get_bars(code)
    bars_table = _bars_table(bars, 30)

    llm = generate_json_with_gemini(
        prompt=_build_prompt(base, bars_table),
        response_schema=_RESEARCH_SCHEMA,
        temperature=0.35,
        max_output_tokens=16384,
        model=strat.llm_model or _LLM_MODEL,
    )
    usage = pop_usage(llm)

    rule_composite = base["投资建议"]["综合推荐分"]
    llm_composite = float(llm.get("composite_score", rule_composite))

    merged = {
        **base,
        "pipeline": ["rule_engine", "llm_research"],
        "llm": {
            "model": strat.llm_model or _LLM_MODEL,
            "enabled": True,
            "usage": usage,
            **llm,
        },
        "投资建议": {
            **base["投资建议"],
            "综合推荐分": llm_composite,
            "规则引擎综合分": rule_composite,
            "LLM综合分": llm_composite,
            "操作建议": llm.get("recommendation", base["投资建议"]["操作建议"]),
            "建议买入价": llm.get("buy_price", base["投资建议"]["建议买入价"]),
            "目标价": llm.get("target_price", base["投资建议"]["目标价"]),
            "止损参考": llm.get("stop_loss", base["投资建议"]["止损参考"]),
            "走势应对": llm.get("scenarios", base["投资建议"]["走势应对"]),
            "总结": llm.get("investment_summary", base["投资建议"]["总结"]),
            "LLM股价分析": llm.get("price_trend_analysis"),
            "LLM技术面分析": llm.get("technical_analysis"),
            "LLM财务分析": llm.get("financial_analysis"),
            "LLM资讯分析": llm.get("news_analysis"),
        },
        "modules": {
            **base["modules"],
            "4_股价分析": {
                **base["modules"]["4_股价分析"],
                "LLM走势深度分析": llm.get("price_trend_analysis"),
                "LLM技术面分析": llm.get("technical_analysis"),
                "LLM技术得分": llm.get("technical_score"),
            },
            "5_财务情况": {
                **base["modules"]["5_财务情况"],
                "LLM分析": llm.get("financial_analysis"),
                "LLM财务得分": llm.get("financial_score"),
            },
            "7_公司风险": {
                **base["modules"]["7_公司风险"],
                "风险要点": llm.get("risks", []),
            },
            "8_核心竞争力": {
                **base["modules"]["8_核心竞争力"],
                "机遇要点": llm.get("opportunities", []),
                "公司摘要": llm.get("company_summary"),
            },
        },
        "data_gaps_for_other_agents": list(
            dict.fromkeys(
                (base.get("data_gaps_for_other_agents") or [])
                + (llm.get("data_gaps") or [])
            )
        ),
    }
    return merged


def format_llm_report_markdown(report: dict[str, Any]) -> str:
    """LLM 增强版 Markdown 研报。"""
    meta = report["meta"]
    title = meta.get("name") or meta["ts_code"]
    llm = report.get("llm") or {}
    advice = report["投资建议"]

    lines = [
        f"# {title}（{meta['ts_code']}）投资研究报告（LLM）",
        "",
        f"> 数据截止：{report.get('as_of', '—')} | "
        f"**LLM 综合推荐分：{advice.get('LLM综合分', advice.get('综合推荐分'))}** | "
        f"规则引擎：{advice.get('规则引擎综合分', '—')}",
        "",
        "## 投资建议",
        advice.get("总结") or advice.get("investment_summary", ""),
        "",
        f"- **操作建议**：{advice.get('操作建议')}",
        f"- **建议买入价**：{advice.get('建议买入价')}",
        f"- **目标价**：{advice.get('目标价')}",
        f"- **止损参考**：{advice.get('止损参考')}",
        "",
        "## 4. 股价走势分析（LLM）",
        llm.get("price_trend_analysis") or advice.get("LLM股价分析") or "—",
        "",
        "## 技术面分析（LLM）",
        llm.get("technical_analysis") or advice.get("LLM技术面分析") or "—",
        "",
        f"**LLM 技术得分**：{llm.get('technical_score', '—')} | "
        f"**规则引擎技术得分**：{report['modules']['4_股价分析'].get('技术得分', '—')}",
        "",
        "## 5. 财务分析（LLM）",
        llm.get("financial_analysis") or "—",
        "",
        "## 资讯与舆情（LLM）",
        llm.get("news_analysis") or "—",
        "",
        "## 7. 风险",
    ]
    for r in llm.get("risks") or []:
        lines.append(f"- {r}")

    lines.extend(["", "## 8. 机遇与竞争力"])
    if llm.get("company_summary"):
        lines.append(llm["company_summary"])
        lines.append("")
    for o in llm.get("opportunities") or []:
        lines.append(f"- {o}")

    lines.extend(["", "### 不同走势应对"])
    for s in advice.get("走势应对") or []:
        lines.append(f"- {s}")

    # 附录：规则引擎指标
    snap = report["modules"]["4_股价分析"].get("指标快照")
    if snap:
        lines.extend(["", "## 附录：量化指标快照", "```"])
        for k, v in snap.items():
            if v is not None:
                lines.append(f"{k}: {v}")
        lines.append("```")

    gaps = report.get("data_gaps_for_other_agents") or llm.get("data_gaps") or []
    if gaps:
        lines.extend(["", "## 数据缺口"])
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines)


_SCAN_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "as_of": {"type": "string"},
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ts_code": {"type": "string"},
                    "trend_one_liner": {
                        "type": "string",
                        "description": "一句话走势判断，须引用具体数值（ret_20d/vol_ratio20/KDJ/signals），≤60字，含题材、技术信号、风险提示",
                    },
                    "risk_flags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "追高风险(涨幅过高)",
                                "量价背离(缩量上涨)",
                                "接近阶段高点",
                                "超买区域(KDJ/RSI)",
                                "基本面恶化",
                                "题材退潮",
                                "监管/减持风险",
                                "无明显风险",
                            ],
                        },
                        "description": "1-3 项具体风险；无风险填['无明显风险']",
                    },
                    "confidence_adjustment": {
                        "type": "number",
                        "description": "相对规则分的置信调整：-5 到 +3，默认 0；仅在确有证据时微调",
                    },
                    "recommendation": {
                        "type": "string",
                        "enum": ["强烈关注", "关注", "观望", "谨慎", "回避"],
                    },
                    "vs_rule_engine": {
                        "type": "string",
                        "description": "与规则引擎差异说明，≤30字",
                    },
                },
                "required": ["ts_code", "trend_one_liner", "risk_flags", "confidence_adjustment", "recommendation"],
            },
        },
    },
    "required": ["reviews"],
}


def _compact_pick_row(p: dict[str, Any]) -> dict[str, Any]:
    buy = p.get("predicted_buy_price")
    close = p.get("close")
    buy_gap_pct = None
    if buy and close:
        buy_gap_pct = round((float(close) - float(buy)) / float(buy) * 100, 2)
    rule_c = p.get("rule_composite_score") or p.get("composite_score")
    concepts = p.get("concepts")
    if concepts is None and p.get("ts_code"):
        concepts = concepts_for_code(str(p["ts_code"]), limit=6)
    return {
        "ts_code": p.get("ts_code"),
        "name": p.get("name"),
        "industry": p.get("industry"),
        "concepts": concepts or [],
        "concept_count": len(concepts) if concepts else 0,
        "close": close,
        "ret_20d": p.get("ret_20d"),
        "high_60d_pct": p.get("high_60d_pct"),
        "vol_ratio20": p.get("vol_ratio20"),
        "tech_score": p.get("tech_score"),
        "rule_composite": rule_c,
        "suggested_buy": buy,
        "close_vs_buy_gap_pct": buy_gap_pct,
        "kdj_k": p.get("kdj_k"),
        "kdj_d": p.get("kdj_d"),
        "signals": (p.get("signals") or [])[:3],
        "news_48h": p.get("news_mentions_48h"),
    }


_LLM_BRIEF_RULES = """
【定位】你是风险审核员，不是评分员。规则引擎已给出综合基准分（rule_composite），你只需：1) 识别具体风险 → risk_flags；2) 微小置信调整 → confidence_adjustment；3) 操作建议 → recommendation。

【核心约束：你不是在打分，而是在标注风险】
- confidence_adjustment 默认 0。仅在有明确证据时微调（-5 到 +3）。
- 发现 risk_flags 中 ≥2 项严重风险（追高/量价背离/接近高点/超买）→ recommendation 最高「观望」。
- ret_20d>5% 且 vol_ratio20<1.0 → 必须标注「量价背离(缩量上涨)」。
- ret_20d>7% → 必须标注「追高风险(涨幅过高)」。
- high_60d_pct>92% → 必须标注「接近阶段高点」。
- KDJ 的 K>80 或 RSI>70 → 必须标注「超买区域(KDJ/RSI)」。
- concept_count=0 → 写「题材库未覆盖」，不得编造概念名。
- 无以上风险 → risk_flags 填 ['无明显风险']，confidence_adjustment 填 0。

【trend_one_liner 要求】
- 引用 JSON 中具体数值（ret_20d、vol_ratio20、KDJ K/D、signals 等），≤60 字。
- 若 ret_20d>3% 须写具体涨幅；若有风险须指明风险关键词。
- 禁止套话：「均线多头排列」「技术形态强势」「走势向好」等空洞描述一律禁止。

【vs_rule_engine 要求】
- 说明风险因素或与规则引擎的差异，≤30 字。
- 若无明显风险则写「风险可控」。"""



def _apply_risk_penalty(p: dict[str, Any], br: dict[str, Any]) -> dict[str, Any]:
    """将 LLM 风险标记转化为分数调整，取代旧版分数封顶。

    LLM 不再输出 composite_score，改为 risk_flags + confidence_adjustment。
    最终 adjusted_score = rule_composite + confidence_adjustment - risk_penalty。
    """
    out = dict(br)
    rule = float(p.get("rule_composite_score") or p.get("composite_score") or 0)
    ret20 = p.get("ret_20d")
    high_pct = p.get("high_60d_pct")
    vol_ratio = p.get("vol_ratio20")

    # LLM 置信调整（小范围：-5 到 +3）
    confidence_adj = float(br.get("confidence_adjustment", 0))
    confidence_adj = max(-5.0, min(3.0, confidence_adj))

    # 从 risk_flags 计算额外扣分
    risk_flags = list(br.get("risk_flags") or [])
    risk_penalty = 0.0
    if "追高风险(涨幅过高)" in risk_flags:
        risk_penalty += 5.0
    if "量价背离(缩量上涨)" in risk_flags:
        risk_penalty += 4.0
    if "接近阶段高点" in risk_flags:
        risk_penalty += 3.0
    if "超买区域(KDJ/RSI)" in risk_flags:
        risk_penalty += 3.0
    if "监管/减持风险" in risk_flags:
        risk_penalty += 6.0
    if "基本面恶化" in risk_flags:
        risk_penalty += 5.0
    if "题材退潮" in risk_flags:
        risk_penalty += 4.0

    # 程序侧硬约束（独立于 LLM，双重保险）
    if ret20 is not None:
        r = float(ret20)
        if r > 7:
            risk_penalty = max(risk_penalty, 6.0)
            if "追高风险(涨幅过高)" not in risk_flags:
                risk_flags.append("追高风险(涨幅过高)")
        elif r > 5:
            risk_penalty = max(risk_penalty, 3.0)
    if high_pct is not None and float(high_pct) > 92:
        risk_penalty = max(risk_penalty, 4.0)
        if "接近阶段高点" not in risk_flags:
            risk_flags.append("接近阶段高点")
    if ret20 is not None and float(ret20) > 3 and vol_ratio is not None and float(vol_ratio) < 0.8:
        risk_penalty = max(risk_penalty, 4.0)
        if "量价背离(缩量上涨)" not in risk_flags:
            risk_flags.append("量价背离(缩量上涨)")

    # 推荐语降级（基于风险）
    rec = str(out.get("recommendation") or "")
    if risk_penalty >= 8 and rec in ("强烈关注", "关注", "观望"):
        out["recommendation"] = "谨慎"
    elif risk_penalty >= 4 and rec in ("强烈关注", "关注"):
        out["recommendation"] = "观望"
    if ret20 is not None and float(ret20) > 7 and rec in ("强烈关注", "关注", "观望"):
        out["recommendation"] = "谨慎"

    # 计算最终分
    final_score = rule + confidence_adj - risk_penalty
    out["adjusted_score"] = round(max(0.0, min(100.0, final_score)), 1)
    out["risk_penalty"] = risk_penalty
    out["confidence_adjustment"] = confidence_adj
    out["risk_flags"] = risk_flags
    return out


def _brief_review_one(
    p: dict[str, Any],
    *,
    as_of: str | None,
    model: str | None,
) -> dict[str, Any]:
    """单票简评 — LLM 作为风险审核员（不输出 composite_score）。"""
    row = _compact_pick_row(p)
    prompt = f"""A股风险审核。数据截止 {as_of or '最新'}。
{_LLM_BRIEF_RULES}
标的：{json.dumps(row, ensure_ascii=False)}
输出 JSON 单对象：ts_code, trend_one_liner(≤60字), risk_flags, confidence_adjustment, recommendation, vs_rule_engine(≤30字)。"""
    raw = generate_json_with_gemini(
        prompt=prompt,
        response_schema=None,
        temperature=0.2,
        max_output_tokens=2048,
        model=model or _LLM_MODEL,
    )
    usage = pop_usage(raw)
    br = _apply_risk_penalty(p, raw if raw.get("ts_code") else raw)
    out = {**p, "rule_composite_score": p.get("composite_score")}
    # LLM 不再输出 composite_score，保存风险信息
    out["llm_composite_score"] = None  # 向后兼容：nullable
    out["composite_score"] = p.get("composite_score")  # 保留规则分
    out["adjusted_score"] = br.get("adjusted_score")
    out["llm_brief"] = {
        "trend": br.get("trend_one_liner"),
        "recommendation": br.get("recommendation"),
        "vs_rule_engine": br.get("vs_rule_engine"),
        "risk_flags": br.get("risk_flags"),
        "risk_penalty": br.get("risk_penalty"),
        "confidence_adjustment": br.get("confidence_adjustment"),
    }
    if usage:
        out["_usage"] = usage
    return out


def _scan_brief_max_output(n: int) -> int:
    """批量简评输出 token 上限（避免 finish_reason=length）。"""
    cap = int(__import__("os").getenv("LLM_SCAN_BRIEF_MAX_OUTPUT", "16384"))
    per_stock = int(__import__("os").getenv("LLM_SCAN_BRIEF_PER_STOCK_OUT", "280"))
    return min(cap, max(2048, 400 + max(1, n) * per_stock))


def _brief_review_batch(
    picks: list[dict[str, Any]],
    *,
    as_of: str | None,
    model: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    rows = [_compact_pick_row(p) for p in picks]
    prompt = f"""你是 A 股风险审核员。以下为规则引擎筛出的 {len(rows)} 只（数据截止 {as_of or '最新'}）。
{_LLM_BRIEF_RULES}

请对**每一只**基于 JSON 中的 concepts、news_48h、close、ret_20d、high_60d_pct、vol_ratio20、KDJ、signals 写 trend_one_liner（**一句话**，≤60字），并给出 risk_flags、confidence_adjustment、recommendation。
vs_rule_engine 每项 **≤30字**。勿漏 ts_code。

【候选列表 JSON】
{json.dumps(rows, ensure_ascii=False, indent=2)}

输出 JSON：reviews 数组，ts_code 与输入一致，共 {len(rows)} 条。"""

    max_out = _scan_brief_max_output(len(rows))
    raw = generate_json_with_gemini(
        prompt=prompt,
        response_schema=_SCAN_BRIEF_SCHEMA,
        temperature=0.25,
        max_output_tokens=max_out,
        model=model or _LLM_MODEL,
    )
    usage = pop_usage(raw)
    by_code = {str(r.get("ts_code")): r for r in raw.get("reviews") or []}

    merged: list[dict[str, Any]] = []
    for p in picks:
        code = str(p.get("ts_code"))
        br = _apply_risk_penalty(p, by_code.get(code) or {})
        out = {**p}
        out["rule_composite_score"] = p.get("composite_score")
        out["llm_composite_score"] = None  # LLM 不再输出 composite_score
        out["composite_score"] = p.get("composite_score")  # 保留规则分
        out["adjusted_score"] = br.get("adjusted_score")
        out["llm_brief"] = {
            "trend": br.get("trend_one_liner"),
            "recommendation": br.get("recommendation"),
            "vs_rule_engine": br.get("vs_rule_engine"),
            "risk_flags": br.get("risk_flags"),
            "risk_penalty": br.get("risk_penalty"),
            "confidence_adjustment": br.get("confidence_adjustment"),
        }
        merged.append(out)
    return merged, usage


def _merge_usage(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    if not a:
        return b
    if not b:
        return a
    out = dict(a)
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if k in b:
            out[k] = int(out.get(k) or 0) + int(b.get(k) or 0)
    out["batch_calls"] = int(out.get("batch_calls") or 1) + int(b.get("batch_calls") or 1)
    return out


def brief_review_scan_picks(
    picks: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    model: str | None = None,
    batch_size: int | None = None,
    per_stock: bool | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """扫描候选：LLM 简评。默认批量模式（省 ~80% tokens）；``per_stock=True`` 时逐只调用。"""
    if not picks:
        return [], None
    if not gemini_available():
        raise GeminiError("未配置 DEEPSEEK_API_KEY")

    use_per_stock = per_stock
    if use_per_stock is None:
        # 默认批量模式（节省 ~80% prompt tokens + 10x 墙钟加速）。
        # 逐只调试：export PICK_LLM_BRIEF_PER_STOCK=true
        use_per_stock = __import__("os").getenv("PICK_LLM_BRIEF_PER_STOCK", "false").lower() in (
            "1",
            "true",
            "yes",
        )

    if use_per_stock:
        merged_all: list[dict[str, Any]] = []
        usage_total: dict[str, Any] | None = None
        for i, p in enumerate(picks, 1):
            one = _brief_review_one(p, as_of=as_of, model=model)
            u = one.pop("_usage", None)
            merged_all.append(one)
            if u:
                u["batch_calls"] = 1
                usage_total = _merge_usage(usage_total, u)
            if i % 25 == 0 or i == len(picks):
                logger.info("LLM 简评逐只 %s/%s", i, len(picks))
        if usage_total:
            usage_total["batch_calls"] = len(picks)
            usage_total["mode"] = "per_stock"
        return merged_all, usage_total

    effective_batch = batch_size if batch_size and batch_size > 0 else 10
    effective_batch = min(effective_batch, 15)

    def _run_chunk(chunk: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        try:
            return _brief_review_batch(chunk, as_of=as_of, model=model)
        except GeminiError as exc:
            if "MAX_TOKENS" not in str(exc) or len(chunk) <= 1:
                if len(chunk) == 1:
                    one = _brief_review_one(chunk[0], as_of=as_of, model=model)
                    u = one.pop("_usage", None)
                    return [one], u
                raise
            mid = len(chunk) // 2
            logger.warning(
                "简评批次 %s 只触发 MAX_TOKENS，拆为 %s + %s 重试",
                len(chunk),
                mid,
                len(chunk) - mid,
            )
            a, u1 = _run_chunk(chunk[:mid])
            b, u2 = _run_chunk(chunk[mid:])
            return a + b, _merge_usage(u1, u2)

    if len(picks) <= effective_batch:
        merged, usage = _run_chunk(picks)
        if usage:
            usage["batch_calls"] = 1
        return merged, usage

    merged_all: list[dict[str, Any]] = []
    usage_total: dict[str, Any] | None = None
    calls = 0
    for i in range(0, len(picks), effective_batch):
        chunk = picks[i : i + effective_batch]
        merged, usage = _run_chunk(chunk)
        merged_all.extend(merged)
        calls += 1
        if usage:
            usage["batch_calls"] = 1
        usage_total = _merge_usage(usage_total, usage)
    if usage_total:
        usage_total["batch_calls"] = calls
    return merged_all, usage_total
