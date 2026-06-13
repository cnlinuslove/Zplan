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
        # ── 4. 股价分析（核心）──
        "price_trend_analysis": {
            "type": "string",
            "description": "近期股价走势深度分析：趋势方向、关键支撑/阻力位（引用具体价格）、量价配合、与板块联动分析；必须引用提供的数据日期与具体价格数值。需包含板块走势对比与资金面定性判断（游资/机构/量化）。",
        },
        "technical_analysis": {
            "type": "string",
            "description": "KDJ/MACD/RSI/均线/布林带等技术指标深度解读。必须引用具体指标数值（如 KDJ-K=xx），分析超买超卖状态、背离形态、均线排列，给出技术面综合判断。若近60日高位或RSI>80须明确警示追高风险。",
        },
        "technical_score": {"type": "number", "description": "技术面打分 0-100"},
        # ── 5. 财务分析（核心）──
        "financial_analysis": {
            "type": "string",
            "description": "财务深度分析（≥300字）。必须包含：(1)近三年营收/利润/现金流趋势表（引用具体数字和增速），(2)杜邦分析拆解（净利率×周转率×杠杆），(3)现金流质量评估（经营现金流vs净利润），(4)资产负债结构风险（负债率、应收类占比、短期偿债压力），(5)如有行业对比数据须引用行业中位数做同业比较。数据不足时须标注「库内数据不完整」并给出推断边界。",
        },
        "financial_score": {"type": "number", "description": "财务面打分 0-100，需在分析中说明加减分逻辑"},
        # ── 资讯与舆情 ──
        "news_analysis": {"type": "string", "description": "近期资讯与舆情分析：事件类型分布、利好/利空定性、市场关注度。无新闻则说明数据缺失并标注影响。须引用具体事件类型和数量。"},
        "news_score": {"type": "number"},
        # ── 公司深度 ──
        "company_summary": {
            "type": "string",
            "description": "公司深度画像（≥200字）。必须包含：(1)公司定位与行业地位，(2)核心产品或服务矩阵（有数据时详述产品线、设计理念、商业模式），(3)核心竞争优势与护城河，(4)如有竞争对标数据须做同业比较。数据不足时明确标注并基于行业常识做「推断」标注。",
        },
        "competitive_landscape": {
            "type": "string",
            "description": "同业竞争格局分析。如提供了行业对比数据（排名、中位数PE/PB/ROE），须做横向对标，指出公司在行业中的生态位、相对优势和劣势。至少对比2-3家核心竞争对手的定位/盈利模式/困境。",
        },
        # ── 风险与机遇 ──
        "risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "风险链式分析，每条按「触发条件 → 传导机制 → 潜在后果」展开，覆盖：技术面风险（超买/背离/高位）、基本面风险（利润率/负债/现金流）、行业风险（竞争/监管/技术替代）、宏观风险（政策/汇率/地缘）。每条≥40字。",
        },
        "opportunities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "核心机遇与催化因素，每条需引用具体数据支撑（政策/行业趋势/公司动作/技术突破），≥30字。",
        },
        # ── 投资建议 ──
        "investment_summary": {
            "type": "string",
            "description": "投资总结（≥150字）。必须包含：(1)核心投资逻辑一句话，(2)多方因素的利弊权衡，(3)与当前股价的关系（估值是否合理/是否有安全边际），(4)适合什么类型的投资者/策略。风格参考专业研报的「投资要点」章节。",
        },
        "composite_score": {"type": "number", "description": "综合投资推荐分 0-100（百分制）。需在 investment_summary 中给出加减分理由。评分标准：≥85强烈推荐，70-84关注，55-69观望，40-54谨慎，<40回避。追高风险时 composite 不得高于规则引擎综合分。"},
        "recommendation": {
            "type": "string",
            "enum": ["强烈关注", "关注", "观望", "谨慎", "回避"],
        },
        "buy_price": {"type": "number"},
        "target_price": {"type": "number"},
        "stop_loss": {"type": "number"},
        # ── 场景策略 ──
        "scenarios": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3 种极端走势的应对策略，每条必须包含触发条件（具体价位/指标）+ 操作纪律：(1)基准情景（大概率震荡/趋势延续），(2)乐观情景（突破后如何分批止盈），(3)悲观情景（破位/黑天鹅后如何止损/何时可重新关注）。",
        },
        "exit_plan": {
            "type": "object",
            "properties": {
                "recommended_plan": {
                    "type": "string",
                    "enum": ["static", "trailing_stop", "atr_trail", "ma_stop", "partial_tp"],
                    "description": "推荐出场策略类型",
                },
                "reasoning": {
                    "type": "string",
                    "description": "出场策略选择逻辑，≤80字",
                },
                "conditional_exits": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-3 条不同市场情境下的退出方案（强/弱/震荡）",
                },
            },
            "required": ["recommended_plan", "reasoning"],
        },
        # ── 数据缺口与引用 ──
        "data_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "当前数据不足以支撑的判断，需补充的数据源。每条说明缺什么数据、影响哪个分析模块。",
        },
        "citation_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "关键数据引用来源标注。格式：'[数据项]：来源（如 zplan.db daily_prices、company_profiles、financial_indicators、LLM推断）'",
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
        "company_summary",
        "risks",
        "opportunities",
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
        # enrich 表存纯数字代码（无后缀）
        code = str(ts_code).replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".HK", "")
        section = _build_enrich_section(code)
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

    enrich_text = _enrich_block(meta["ts_code"])

    return f"""你是一名资深 A 股研究员，报告风格对标专业机构研报（深度 + 数据驱动 + 风险厌恶）。严格基于下方 JSON 数据进行分析，不得编造未给出的数字、新闻、概念或股价。

核心原则：
- 每个数据点必须可追溯（在 citation_notes 中标注来源）
- 数据不足时标注「库内数据不完整」并给出推断边界（标注「推断」）
- 避免空洞的看多/看空口号，所有结论必须有量化依据
- 风险分析必须链式展开：触发条件 → 传导机制 → 潜在后果

【量化与资讯上下文】
{json.dumps(ctx, ensure_ascii=False, indent=2)}

{enrich_text}

══════════════════════════════════════════
【报告撰写指南 — 按模块逐一执行】
══════════════════════════════════════════

【模块 4：股价分析 — 最重要模块】
1. price_trend_analysis（≥200字）：
   - 趋势：引用近30日K线的具体日期和价位（如「X月X日收盘X元，X月X日触及X元高位」），计算区间涨跌幅
   - 支撑/阻力：结合MA20/MA60/布林带给出具体数字
   - 量价配合：引用 vol_ratio20，判断放量/缩量与价格方向是否一致
   - 板块联动：如提供了概念题材，分析所属板块近期走势
   - 资金面：根据换手率/量比判断游资/机构/量化主导特征
   - 若 ret_20d>7%，必须明确警示「追高追涨风险」，引用具体数值
2. technical_analysis（≥150字）：
   - 逐一解读 KDJ-K/D/J、MACD（DIF/DEA/柱状）、RSI、均线排列
   - 必须引用指标快照中的具体数值（如「KDJ-K=85.3，处于超买区」）
   - 判断超买超卖、金叉死叉、背离形态
   - 若 high_60d_pct>90% 或 RSI>80 或 KDJ-K>80，须降分且明确警示
3. technical_score：0-100，超买/近高位/背离须降分，均线多头+放量可加分

【模块 5：财务分析 — 对标 CFA 级深度】
4. financial_analysis（≥300字）：
   - 三年趋势表：引用「财务记录」中的具体年份、营收、净利润、PE/PB/ROE 数字，计算同比增速
   - 杜邦分析：用给出的数据拆解 净利率（净利润/营收）、资产周转率、权益乘数
   - 现金流：如有经营现金流数据，与净利润对比（现金流/净利润比值），判断盈利质量
   - 资产负债：引用负债率、应收类占比，分析财务杠杆风险
   - 同业比较：如有【行业对比】数据，必须引用行业中位数 PE/PB/ROE 做横向对比
   - 有「估值截面」数据时须引用 PE/PB/总市值/流通市值
   - 综合评估：增长性+盈利质量+杠杆水平+估值合理性
5. financial_score：0-100，在分析末尾明确列出加减分项和逻辑（加分项+扣分项）

【模块：资讯与舆情】
6. news_analysis（≥80字）：引用「关联新闻摘要」中的事件类型和数量，定性利好/利空/中性，说明市场关注度。无新闻则写「库内暂无近期关联新闻」并 news_score 取 50。

【模块：公司深度与竞争格局】
7. company_summary（≥200字）：
   - 公司定位：引用行业+概念题材，一句话概括公司的市场角色
   - 核心产品/业务矩阵：如有【公司档案】数据须详述主营业务、产品线、商业模式
   - 竞争优势：分析护城河（规模/技术/牌照/品牌/渠道）
   - 如有【深度数据】中的产品深度分析（competitive_positioning/technology_moat/key_products_json），必须整合进此段
   - 数据不足时标注缺失项
8. competitive_landscape（≥150字）：
   - 如有【行业对比】数据，引用营收/利润/市值排名、行业中位数 PE/PB/ROE
   - 至少对比 2-3 家核心竞争对手的定位、盈利模式与困境
   - 指出公司的行业生态位——是「垄断定价者」「规模流量运营者」「技术赋能平台」还是「传统服务商」
   - 无竞争对手数据时，基于行业常识做推断并标注「推断」

【模块 7：风险分析 — 链式展开】
9. risks（4-6条，每条≥40字）：
   - 每条按「触发条件 → 传导机制 → 潜在后果」三段式展开
   - 必须覆盖：(a)技术面风险（超买/背离/高位/量价背离），(b)基本面风险（利润率低/负债高/现金流差），(c)行业风险（竞争加剧/技术替代/监管变化），(d)宏观风险（政策/汇率/地缘）
   - 如有【机构持仓】数据中北向/基金减持信号，须标注
   - 引用具体数字（如「毛利率仅X%」「负债率X%」「出海业务占比X%」）

【模块 8：核心竞争力与机遇】
10. opportunities（4-6条，每条≥30字）：
    - 引用具体数据：政策利好/行业趋势/公司动作（新产品/AI赋能/出海拓展）
    - 如有【机构研报】中的评级和EPS预测，须引用（如「X家券商给予买入评级，2026E EPS=X元」）
    - 注明每条机遇的确定性（高/中/低）

【投资建议 — 最关键决策模块】
11. investment_summary（≥150字）：
    - 一句话核心投资逻辑（如「AI赋能+出海双引擎，但当前估值已透支2年预期」）
    - 多方 vs 空方的利弊权衡（至少各 2 条）
    - 与当前股价的关系：引用现价，判断估值泡沫/合理/低估，计算安全边际
    - 适合什么类型的投资者/持仓周期
    - composite_score 的加减分理由
12. composite_score：严格按百分制。≥85=强烈推荐，70-84=关注，55-69=观望，40-54=谨慎，<40=回避。追高/高位/基本面羸弱须扣分。若规则引擎综合分已较低且存在严重风险，composite 不得高于规则引擎分。
13. buy_price：必须在 **[close×0.98, close×1.0]** 区间内。直接采用规则引擎给的 suggested_buy（已综合 MA20 回踩/支撑位/ATR 定价）。禁止自定买入价。MA20 附近可给较深折扣（回踩买入），高位股折扣≤0.5%。
14. target_price ≥ buy_price×1.05；stop_loss 须与支撑位一致。

【场景策略 — 三种极端走势的机械操作纪律】
15. scenarios（3条，每条含触发条件+操作）：
    - 基准情景（大概率）：震荡/趋势延续 → 如何持有/加仓/减仓
    - 乐观情景（突破）：冲破阻力位后 → 分批止盈策略（每涨X%卖Y%）
    - 悲观情景（破位/黑天鹅）：跌破关键支撑/利空事件 → 止损触发点+何时可重新关注

16. citation_notes（≥3条）：标注关键数据来源。格式示例：「股价K线数据：zplan.db daily_prices」「财务指标：financial_indicators 表」「行业对比：industry_peers 表」「概念标签：concept_tags」「公司档案：company_profiles 表（enrich_company）」「机构研报：research_reports 表」

17. data_gaps：列出当前数据不足以支撑的判断及缺失的数据源。

══════════════════════════════════════════
输出合法 JSON，所有文本字段中文撰写，引用数据时注明具体数字。"""


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
            "2_核心产品": {
                **base["modules"]["2_核心产品"],
                "LLM产品分析": llm.get("company_summary"),
            },
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
                "竞争格局": llm.get("competitive_landscape"),
            },
        },
        "引用来源": llm.get("citation_notes") or [],
        "data_gaps_for_other_agents": list(
            dict.fromkeys(
                (base.get("data_gaps_for_other_agents") or [])
                + (llm.get("data_gaps") or [])
            )
        ),
    }
    return merged


def format_llm_report_markdown(report: dict[str, Any]) -> str:
    """LLM 增强版 Markdown 研报（对标机构研报 8 模块结构）。"""
    meta = report["meta"]
    title = meta.get("name") or meta["ts_code"]
    llm = report.get("llm") or {}
    advice = report["投资建议"]
    modules = report["modules"]

    lines = [
        f"# {title}（{meta['ts_code']}）深度研究报告",
        "",
        f"> 数据截止：{report.get('as_of', '—')} | "
        f"**LLM 综合推荐分：{advice.get('LLM综合分', advice.get('综合推荐分'))}** | "
        f"规则引擎：{advice.get('规则引擎综合分', '—')} | "
        f"操作建议：**{advice.get('操作建议', '—')}**",
        "",
        "---",
        "",
    ]

    # ═══ 1. 公司基本信息 ═══
    m1 = modules.get("1_基本信息", {})
    lines.extend([
        "## 1. 公司基本信息",
        "",
        f"- **行业**：{m1.get('行业', '—')}",
        f"- **上市日期**：{m1.get('上市日期', '—')}",
        f"- **官网**：{m1.get('官网', '—')}",
        f"- **数据来源**：{m1.get('数据来源', '—')}",
        "",
    ])

    # ═══ 2. 核心产品 ═══
    m2 = modules.get("2_核心产品", {})
    llm_product = m2.get("LLM产品分析", "")
    core_products = m2.get("核心产品", "")
    lines.append("## 2. 核心产品")
    lines.append("")
    if llm_product:
        lines.append(llm_product)
        lines.append("")
    elif core_products and core_products != "待扩展":
        lines.append(f"- 核心产品：{core_products}")
        lines.append("")
    else:
        lines.append("> ⚠️ 产品数据待充实（需 enrich_company P2 深度调研）")
        lines.append("")
    if m2.get("news_mentions_48h"):
        lines.append(f"- 48h 新闻提及：{m2['news_mentions_48h']} 条")
        lines.append("")

    # ═══ 3. 创始团队 ═══
    m3 = modules.get("3_创始团队", {})
    team_data = m3.get("团队", "")
    lines.append("## 3. 创始团队与核心管理层")
    lines.append("")
    if team_data and team_data != "待扩展":
        try:
            import json as _json
            team_dict = _json.loads(team_data) if isinstance(team_data, str) else team_data
            if isinstance(team_dict, dict) and team_dict:
                for k, v in team_dict.items():
                    lines.append(f"- **{k}**：{v}")
            else:
                lines.append(f"- {team_data}")
        except Exception:
            lines.append(f"- {team_data}")
    else:
        lines.append("> ⚠️ 管理层数据待充实（需 enrich_company P0 公司档案扩展）")
    lines.append("")

    # ═══ 4. 股价分析 ═══
    m4 = modules.get("4_股价分析", {})
    lines.extend([
        "## 4. 股价分析（核心）",
        "",
    ])
    # LLM 走势深度分析
    price_analysis = llm.get("price_trend_analysis") or advice.get("LLM股价分析") or m4.get("LLM走势深度分析", "")
    if price_analysis:
        lines.append("### 4.1 走势深度分析")
        lines.append("")
        lines.append(price_analysis)
        lines.append("")
    # 规则引擎趋势
    trend = m4.get("趋势叙述", "")
    if trend and "近 60" in str(trend):
        lines.append(f"> 规则引擎趋势：{trend}")
        lines.append("")
    # 技术面分析
    tech_analysis = llm.get("technical_analysis") or advice.get("LLM技术面分析") or m4.get("LLM技术面分析", "")
    lines.append("### 4.2 技术指标分析")
    lines.append("")
    lines.append(f"**技术面结论**：{m4.get('技术面结论', '—')}")
    lines.append("")
    if tech_analysis:
        lines.append(tech_analysis)
        lines.append("")
    # 得分
    lines.append(f"| 评分维度 | 得分 |")
    lines.append(f"|----------|------|")
    lines.append(f"| LLM 技术得分 | {llm.get('technical_score', '—')} |")
    lines.append(f"| 规则引擎技术得分 | {m4.get('技术得分', '—')} |")
    lines.append("")

    # 关键信号
    signals = m4.get("关键信号") or []
    if signals:
        lines.append("**关键信号**：")
        for sig in signals:
            lines.append(f"- {sig}")
        lines.append("")

    # 筹码分布
    chip = m4.get("筹码分布") or {}
    if chip.get("available"):
        lines.extend([
            "**筹码分布**：",
            f"- 获利比例：{chip['profit_ratio']:.1f}%　|　平均成本：{chip['avg_cost']:.2f}",
            f"- 90%筹码区间：[{chip['cost_90_low']:.2f}, {chip['cost_90_high']:.2f}]",
            f"- 90%集中度：{chip['concentration_90']:.4f}　|　70%集中度：{chip['concentration_70']:.4f}",
            f"- 数据截止：{chip.get('as_of', '—')}",
            "",
        ])

    # ═══ 5. 财务情况 ═══
    m5 = modules.get("5_财务情况", {})
    lines.extend([
        "## 5. 财务情况（核心）",
        "",
    ])
    fin_analysis = llm.get("financial_analysis") or advice.get("LLM财务分析") or m5.get("LLM分析", "")
    if fin_analysis:
        lines.append(fin_analysis)
        lines.append("")
    # 规则引擎财务
    lines.append(f"**规则引擎财务评语**：{m5.get('评语', '—')}")
    lines.append(f"**规则引擎财务得分**：{m5.get('财务得分', '—')}")
    if llm.get("financial_score") is not None:
        lines.append(f"**LLM 财务得分**：{llm.get('financial_score', '—')}")
    lines.append("")

    # 估值截面
    snap = m5.get("估值截面")
    if snap and isinstance(snap, dict):
        lines.append("**估值截面**：")
        pe = snap.get("pe_ttm")
        pb = snap.get("pb")
        mv = snap.get("total_mv")
        if pe:
            lines.append(f"- PE(TTM)：{pe:.2f}")
        if pb:
            lines.append(f"- PB：{pb:.2f}")
        if mv:
            lines.append(f"- 总市值：{mv/1e8:.1f} 亿")
        lines.append("")

    # ═══ 6. 投资持仓 ═══
    m6 = modules.get("6_投资持仓", {})
    lines.append("## 6. 获得投资情况")
    lines.append("")
    if m6 and m6.get("状态") and "待" not in str(m6.get("状态")):
        lines.append(f"- {m6.get('状态', '—')}")
    else:
        lines.append("> ⚠️ 机构持仓数据待充实（需 enrich_company P1 股东+北向+基金 ETL）")
    lines.append("")

    # ═══ 7. 公司风险 ═══
    m7 = modules.get("7_公司风险", {})
    lines.extend([
        "## 7. 公司风险（核心）",
        "",
    ])
    # LLM 风险要点
    llm_risks = llm.get("risks") or m7.get("风险要点") or []
    if llm_risks:
        for i, r in enumerate(llm_risks, 1):
            lines.append(f"{i}. {r}")
            lines.append("")
    else:
        lines.append("> 暂无风险标注")
        lines.append("")
    # 技术风险
    lines.append(f"**技术面风险信号**：{m7.get('技术风险', '—')}")
    lines.append(f"**48h 新闻条数**：{m7.get('新闻条数_48h', 0)}")
    lines.append("")

    # ═══ 8. 核心竞争力 ═══
    m8 = modules.get("8_核心竞争力", {})
    lines.extend([
        "## 8. 核心竞争力与行业对标",
        "",
    ])
    # 公司摘要
    company_summary = llm.get("company_summary") or m8.get("公司摘要", "")
    if company_summary:
        lines.append("### 8.1 公司定位与护城河")
        lines.append("")
        lines.append(company_summary)
        lines.append("")

    # 竞争格局
    competitive = llm.get("competitive_landscape") or m8.get("竞争格局", "")
    if competitive:
        lines.append("### 8.2 同业竞争格局")
        lines.append("")
        lines.append(competitive)
        lines.append("")

    # 机遇
    opportunities = llm.get("opportunities") or m8.get("机遇要点") or []
    if opportunities:
        lines.append("### 8.3 核心机遇与催化")
        lines.append("")
        for o in opportunities:
            lines.append(f"- {o}")
        lines.append("")

    # ═══ 9. 投资建议 ═══
    lines.extend([
        "---",
        "",
        "## 9. 投资建议",
        "",
    ])
    # 总结
    inv_summary = advice.get("总结") or advice.get("investment_summary", "")
    if inv_summary:
        lines.append(inv_summary)
        lines.append("")

    lines.extend([
        "### 价格与操作建议",
        "",
        f"| 项目 | 价格 |",
        f"|------|------|",
        f"| 操作建议 | **{advice.get('操作建议', '—')}** |",
        f"| 建议买入价 | {advice.get('建议买入价', '—')} |",
        f"| 目标价 | {advice.get('目标价', '—')} |",
        f"| 止损参考 | {advice.get('止损参考', '—')} |",
        f"| 综合推荐分 | {advice.get('综合推荐分', '—')} / 100 |",
        "",
    ])

    # 走势应对
    scenarios = advice.get("走势应对") or []
    if scenarios:
        lines.append("### 不同走势应对策略")
        lines.append("")
        for i, s in enumerate(scenarios, 1):
            lines.append(f"{i}. {s}")
            lines.append("")
    else:
        lines.append("> 暂无场景策略")
        lines.append("")

    # ═══ 附录 ═══
    # 指标快照
    snap = m4.get("指标快照")
    if snap:
        lines.extend([
            "---",
            "",
            "## 附录 A：量化指标快照",
            "",
            "```",
        ])
        for k, v in snap.items():
            if v is not None:
                lines.append(f"{k}: {v}")
        lines.extend(["```", ""])

    # 引用来源
    citations = report.get("引用来源") or llm.get("citation_notes") or []
    if citations:
        lines.extend([
            "## 附录 B：数据引用来源",
            "",
        ])
        for c in citations:
            lines.append(f"- {c}")
        lines.append("")

    # 数据缺口
    gaps = report.get("data_gaps_for_other_agents") or llm.get("data_gaps") or []
    if gaps:
        lines.extend([
            "## 附录 C：数据缺口",
            "",
        ])
        for g in gaps:
            lines.append(f"- {g}")
        lines.append("")

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
                    "positive_flags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "多题材催化",
                                "资讯催化",
                                "温和放量上涨",
                                "买价可成交",
                                "无明显催化",
                            ],
                        },
                        "description": "1-3 项正面催化剂；无催化填['无明显催化']",
                    },
                    "confidence_adjustment": {
                        "type": "number",
                        "description": "相对规则分的置信调整：-5 到 +5，默认 0；正面催化可加，风险可减",
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
                "required": ["ts_code", "trend_one_liner", "risk_flags", "positive_flags", "confidence_adjustment", "recommendation"],
            },
        },
        "recommended_exit_plan": {
            "type": "string",
            "enum": ["static", "trailing_stop", "atr_trail", "ma_stop", "partial_tp"],
            "description": "最适合此标的的退出策略类型（全局推荐，覆盖多数票）",
        },
        "exit_reasoning": {
            "type": "string",
            "description": "出场策略选择理由，≤60字。如「波动偏高(ATR%>5)需宽松止损」或「趋势强劲宜用移动止盈」",
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

    # ── 行业上下文（板块轮动关键信息）──
    features = p.get("features") or {}
    industry_heat = features.get("_industry_heat")
    industry_rank_pct = features.get("_industry_rank_pct")
    industry_rel_rank = features.get("_industry_relative_rank")
    industry_ctx = None
    if p.get("industry") and industry_heat is not None:
        rank_desc = ""
        if industry_rank_pct is not None:
            if industry_rank_pct >= 80:
                rank_desc = "（领涨板块，前{:.0f}%）".format(100 - industry_rank_pct)
            elif industry_rank_pct >= 60:
                rank_desc = "（偏强板块）"
            elif industry_rank_pct >= 40:
                rank_desc = "（中性）"
            elif industry_rank_pct >= 20:
                rank_desc = "（偏弱板块）"
            else:
                rank_desc = "（领跌板块，后{:.0f}%）".format(industry_rank_pct)
        leader_note = ""
        if industry_rel_rank is not None and industry_rel_rank >= 80:
            leader_note = "；该股在行业内领涨（前{:.0f}%）".format(100 - industry_rel_rank)
        industry_ctx = (
            f"行业「{p['industry']}」20日涨幅 {industry_heat:+.1f}%{rank_desc}{leader_note}"
        )

    return {
        "ts_code": p.get("ts_code"),
        "name": p.get("name"),
        "industry": p.get("industry"),
        "industry_context": industry_ctx,
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
【定位】你是投资分析师。规则引擎已给出综合基准分（rule_composite），你需要综合评估风险与机会：1) 识别风险 → risk_flags；2) 识别正面催化剂 → positive_flags；3) 置信调整 → confidence_adjustment（可正可负）；4) 操作建议 → recommendation。

【板块轮动上下文（重要！2026-06-12 新增）】
- 每只标的的 JSON 中含有 `industry_context` 字段，描述该股所属行业的 20 日涨幅和板块强弱排名。
- 领涨板块（前20%）中的个股 → 板块趋势加持，可上调 confidence_adjustment +1~+2（前提：个股无严重风险）。
- 领跌板块（后20%）中的个股 → 逆势选股风险大，即使个股技术面尚可，也应下调 confidence_adjustment -1~-3，推荐最高为「关注」。
- 行业龙头（在行业内领涨的个股）→ 有资金聚焦优势，可上调 confidence_adjustment +1~+2。
- 板块轮动是 A 股最重要的 alpha 来源之一——选对板块有时比选对个股更重要。

【风险识别（必须标注的风险）】
- ret_20d>5% 且 vol_ratio20<1.0 → 必须标注「量价背离(缩量上涨)」。
- ret_20d>7% → 必须标注「追高风险(涨幅过高)」。
- high_60d_pct>90% → 必须标注「接近阶段高点」。
- KDJ 的 K>80 或 RSI>70 → 必须标注「超买区域(KDJ/RSI)」。
- concept_count=0 → 写「题材库未覆盖」，不得编造概念名。

【正面催化剂识别（有明确证据时标注）】
- concept_count≥3 且题材与近期热点相关 → positive_flags 标注「多题材催化」。
- news_48h 有利好事件（中标/业绩预增/新产品/政策利好）→ 标注「资讯催化」。
- ret_20d 在 0~5% 且 vol_ratio20>1.0 且无明显风险 → 标注「温和放量上涨」。
- close_vs_buy_gap_pct<5% → 标注「买价可成交」。
- 无明显利好 → positive_flags 填 ['无明显催化']。

【confidence_adjustment 规则】
- 默认 0。有正面催化剂可 +1~+5；有风险可 -1~-5。
- 正面催化剂≥2 项且风险≤1 项 → 至少 +2。
- 严重风险≥2 项且无正面催化剂 → 至少 -3。

【recommendation 规则（五选一：强烈关注/关注/观望/谨慎/回避）】
- 正面催化剂≥2 且风险≤1 → 可「强烈关注」或「关注」。
- 风险≥2 且无正面催化剂 → 最高「观望」。
- ret_20d>7% → 最高「观望」（除非有极强正面催化剂可升至「关注」）。
- 基本面恶化或监管/减持 → 「回避」。
- 其余情况综合判断，鼓励给出明确方向，减少「观望」。
- ⚠️ 回测数据表明：「强烈关注」「积极关注」的票历史上平均亏损 -8~-15%。请克制使用最高两级推荐，除非有极明确的催化剂+低风险组合。宁可多给「关注」而非「强烈关注」。

【trend_one_liner 要求】
- 引用 JSON 中具体数值（ret_20d、vol_ratio20、KDJ K/D、signals 等），≤60 字。
- 若有正面催化须指明；若有风险须指明风险关键词。
- 须提及板块强弱（如「XX板块领涨」「行业偏弱」），不可忽略 industry_context。
- 禁止套话：「均线多头排列」「技术形态强势」「走势向好」等空洞描述一律禁止。

【vs_rule_engine 要求】
- 说明与规则引擎的差异（风险或催化因素），≤30 字。
- 若无明显差异则写「与规则引擎一致」。

【出场策略推荐（recommended_exit_plan + exit_reasoning）】
为整体选股池推荐最合适的出场策略类型，综合以下判断：
- 多数票波动偏高（ATR% > 5）→ 推荐 atr_trail（ATR 自适应止损）。
- 多数票趋势强劲（均线多头 + ret_20d 3~7%）→ 推荐 trailing_stop（移动止盈保利润）。
- 多数票偏题材催化（多概念+高资讯量）→ 推荐 partial_tp（分批止盈降风险）。
- 多数票超买/接近高点 → 推荐 static（固定目标不恋战）。
- 波动极低（ATR%<2）且区间震荡 → 推荐 ma_stop（均线破位出场）。
- 无明确倾向 → 推荐 static（默认静态止盈止损）。
exit_reasoning 不超过 60 字，引用关键指标支撑选择。"""



# ── 处罚权重兜底（当 strategy.yaml 不可用或缺少 penalty_weights 段时）──
_DEFAULT_PENALTY_WEIGHTS: dict[str, Any] = {
    "risk_flags": {
        "追高风险(涨幅过高)": 5.0,
        "量价背离(缩量上涨)": 4.0,
        "接近阶段高点": 3.0,
        "超买区域(KDJ/RSI)": 3.0,
        "监管/减持风险": 6.0,
        "基本面恶化": 5.0,
        "题材退潮": 4.0,
    },
    "positive_flags": {
        "多题材催化": 2.0,
        "资讯催化": 2.0,
        "温和放量上涨": 2.0,
        "买价可成交": 1.0,
    },
    "program_constraints": {
        "ret20_over_7_min_penalty": 6.0,
        "ret20_over_5_min_penalty": 3.0,
        "high60_over_95_min_penalty": 3.0,
        "divergence_min_penalty": 4.0,
        "divergence_ret20_threshold": 3.0,
        "divergence_vol_threshold": 0.8,
    },
    "recommendation_rules": {
        "severe_penalty_downgrade": 10,
        "net_risk_high_downgrade": 5,
        "positive_upgrade_threshold": 3,
        "net_risk_low_upgrade": 2,
    },
}


def _apply_risk_penalty(
    p: dict[str, Any],
    br: dict[str, Any],
    penalty_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将 LLM 风险/催化标记转化为分数调整，取代旧版分数封顶。

    LLM 输出 risk_flags + positive_flags + confidence_adjustment。
    最终 adjusted_score = rule_composite + confidence_adjustment - risk_penalty + positive_boost。

    处罚权重优先从 penalty_weights 参数读取，其次从 strategy.yaml 加载，最后用硬编码兜底。
    """
    # ── 加载处罚权重 ──
    if penalty_weights is None:
        try:
            from pick_agent.strategy import load_strategy
            penalty_weights = load_strategy().penalty_weights
        except Exception:
            penalty_weights = {}
    if not penalty_weights:
        penalty_weights = _DEFAULT_PENALTY_WEIGHTS

    risk_weights = penalty_weights.get("risk_flags", _DEFAULT_PENALTY_WEIGHTS["risk_flags"])
    positive_weights = penalty_weights.get("positive_flags", _DEFAULT_PENALTY_WEIGHTS["positive_flags"])
    constraints = penalty_weights.get("program_constraints", _DEFAULT_PENALTY_WEIGHTS["program_constraints"])
    rec_rules = penalty_weights.get("recommendation_rules", _DEFAULT_PENALTY_WEIGHTS["recommendation_rules"])

    out = dict(br)
    rule = float(p.get("rule_composite_score") or p.get("composite_score") or 0)
    ret20 = p.get("ret_20d")
    high_pct = p.get("high_60d_pct")
    vol_ratio = p.get("vol_ratio20")

    # LLM 置信调整（-5 到 +5，允许正面催化剂抬分）
    confidence_adj = float(br.get("confidence_adjustment", 0))
    confidence_adj = max(-5.0, min(5.0, confidence_adj))

    # 从 risk_flags 计算扣分（权重可配置）
    risk_flags = list(br.get("risk_flags") or [])
    risk_penalty = 0.0
    for flag in risk_flags:
        risk_penalty += float(risk_weights.get(flag, 0))

    # 正面催化剂加分（权重可配置）
    positive_flags = list(br.get("positive_flags") or [])
    positive_boost = 0.0
    for flag in positive_flags:
        positive_boost += float(positive_weights.get(flag, 0))

    # 程序侧硬约束（独立于 LLM，双重保险 — 阈值可配置）
    ret20_over_7 = float(constraints.get("ret20_over_7_min_penalty", 6.0))
    ret20_over_5 = float(constraints.get("ret20_over_5_min_penalty", 3.0))
    high60_over_95 = float(constraints.get("high60_over_95_min_penalty", 3.0))
    div_min = float(constraints.get("divergence_min_penalty", 4.0))
    div_ret20 = float(constraints.get("divergence_ret20_threshold", 3.0))
    div_vol = float(constraints.get("divergence_vol_threshold", 0.8))

    if ret20 is not None:
        r = float(ret20)
        if r > 7:
            risk_penalty = max(risk_penalty, ret20_over_7)
            if "追高风险(涨幅过高)" not in risk_flags:
                risk_flags.append("追高风险(涨幅过高)")
        elif r > 5:
            risk_penalty = max(risk_penalty, ret20_over_5)
    if high_pct is not None and float(high_pct) > 95:
        risk_penalty = max(risk_penalty, high60_over_95)
        if "接近阶段高点" not in risk_flags:
            risk_flags.append("接近阶段高点")
    if ret20 is not None and float(ret20) > div_ret20 and vol_ratio is not None and float(vol_ratio) < div_vol:
        risk_penalty = max(risk_penalty, div_min)
        if "量价背离(缩量上涨)" not in risk_flags:
            risk_flags.append("量价背离(缩量上涨)")

    # 推荐语调整：风险与催化剂综合判断（阈值可配置）
    rec = str(out.get("recommendation") or "")
    net_risk = risk_penalty - positive_boost

    severe_downgrade = float(rec_rules.get("severe_penalty_downgrade", 10))
    net_risk_downgrade = float(rec_rules.get("net_risk_high_downgrade", 5))
    positive_upgrade = float(rec_rules.get("positive_upgrade_threshold", 3))
    net_risk_upgrade = float(rec_rules.get("net_risk_low_upgrade", 2))

    if risk_penalty >= severe_downgrade and rec in ("强烈关注", "关注", "观望"):
        out["recommendation"] = "谨慎"
    elif net_risk >= net_risk_downgrade and rec in ("强烈关注", "关注"):
        out["recommendation"] = "观望"
    # 正面催化剂可升一级：观望→关注
    if net_risk <= net_risk_upgrade and positive_boost >= positive_upgrade and rec == "观望":
        out["recommendation"] = "关注"
    if ret20 is not None and float(ret20) > 7 and rec in ("强烈关注", "关注", "观望"):
        out["recommendation"] = "谨慎"

    # ── 反热情惩罚（数据驱动）─────────────────────────────
    # 回测发现：LLM 标记「强烈关注」「积极关注」的票前向收益最差（-8~-15%）。
    # confidence_adjustment > 3 时，LLM 过度乐观也是反向指标（-5~-10%）。
    # 策略：过度热情的推荐降级 + 额外扣分。
    over_enthusiasm = False
    if any(w in rec for w in ["强烈关注", "积极关注"]):
        over_enthusiasm = True
    if confidence_adj > 3:
        over_enthusiasm = True

    if over_enthusiasm:
        extra_penalty = 5.0
        risk_penalty += extra_penalty
        out["_anti_enthusiasm_penalty"] = extra_penalty
        # 热情降级：强烈关注→关注，积极关注→观望
        if "强烈关注" in rec:
            out["recommendation"] = "关注"
        elif "积极关注" in rec:
            out["recommendation"] = "观望"

    # 计算最终分
    final_score = rule + confidence_adj - risk_penalty + positive_boost
    out["adjusted_score"] = round(max(0.0, min(200.0, final_score)), 1)
    out["risk_penalty"] = risk_penalty
    out["positive_boost"] = positive_boost
    out["confidence_adjustment"] = confidence_adj
    out["risk_flags"] = risk_flags
    out["positive_flags"] = positive_flags
    return out


def _brief_review_one(
    p: dict[str, Any],
    *,
    as_of: str | None,
    model: str | None,
) -> dict[str, Any]:
    """单票简评 — LLM 作为投资分析师（评估风险与催化剂）。"""
    row = _compact_pick_row(p)
    prompt = f"""A股投资分析。数据截止 {as_of or '最新'}。
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
    adj = br.get("adjusted_score")
    out = {**p, "rule_composite_score": p.get("composite_score")}
    # 风险调整后的分数就是新的 LLM 分数（替代旧版 composite_score）
    out["llm_composite_score"] = adj
    out["composite_score"] = adj  # 排序/最终分用调整后的值
    out["adjusted_score"] = adj
    out["llm_brief"] = {
        "trend": br.get("trend_one_liner"),
        "recommendation": br.get("recommendation"),
        "vs_rule_engine": br.get("vs_rule_engine"),
        "risk_flags": br.get("risk_flags"),
        "positive_flags": br.get("positive_flags"),
        "risk_penalty": br.get("risk_penalty"),
        "positive_boost": br.get("positive_boost"),
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
    prompt = f"""你是 A 股投资分析师。以下为规则引擎筛出的 {len(rows)} 只（数据截止 {as_of or '最新'}）。
{_LLM_BRIEF_RULES}

请对**每一只**基于 JSON 中的 industry_context、concepts、news_48h、close、ret_20d、high_60d_pct、vol_ratio20、KDJ、signals 写 trend_one_liner（**一句话**，≤60字），并给出 risk_flags、positive_flags、confidence_adjustment、recommendation。注意 industry_context 描述了板块强弱——领涨板块个股可适度上调信心，领跌板块个股需额外谨慎。
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
        adj = br.get("adjusted_score")
        out = {**p}
        out["rule_composite_score"] = p.get("composite_score")
        out["llm_composite_score"] = adj  # 风险调整后分数
        out["composite_score"] = adj  # 排序/最终分用调整后的值
        out["adjusted_score"] = adj
        out["llm_brief"] = {
            "trend": br.get("trend_one_liner"),
            "recommendation": br.get("recommendation"),
            "vs_rule_engine": br.get("vs_rule_engine"),
            "risk_flags": br.get("risk_flags"),
            "positive_flags": br.get("positive_flags"),
            "risk_penalty": br.get("risk_penalty"),
            "positive_boost": br.get("positive_boost"),
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
