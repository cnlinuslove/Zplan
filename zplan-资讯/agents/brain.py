"""Z-Plan Brain — 统一智能引擎。

单入口处理所有 LLM 交互。内部流程：
1. 预加载股票数据（复用 chat_engine.gather_chat_context）
2. 发 system prompt + tools 给 DeepSeek
3. 如果 LLM 请求 tool_call → 并行执行 → 追加结果 → 再调
4. 返回最终回复

作为 chat_engine 的 function-calling 增强版，chat_engine 保留为回退路径。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any

from agents.chat_engine import SYSTEM_PROMPT as _BASE_SYSTEM_PROMPT
from agents.chat_engine import gather_chat_context
from agents.shared import find_stocks_in_text
from config import INFO_QUERY_LIVE_FETCH
from llm.gemini_client import (
    _chat_completion,
    _effective_max_tokens,
    _extract_text_and_check,
    deepseek_available,
)
from models import SessionLocal, init_db
from sqlalchemy import desc, select
from zplan_shared.models import PickEntry, PickRun
from zplan_shared.news_linker import get_linked_news_for_stock

logger = logging.getLogger(__name__)

# ── Tool 定义 ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_info",
            "description": "获取股票基本信息：行业、上市日期、概念板块、最新规则评分和技术判定",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码，如 603020"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_bars",
            "description": "获取近期K线数据摘要：最新价、近5日走势、60日区间高低点和均量",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_financials",
            "description": "获取财报指标：PE/PB/ROE/营收/净利润，近4个报告期",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "实时搜索最新资讯（Google News + 东财快讯），获取与股票或话题相关的新闻",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "code": {"type": "string", "description": "可选，股票代码以增强搜索精度"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_linked_news",
            "description": "获取库内已关联到该股票的近期新闻（近7天已入库的）",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pick_result",
            "description": "获取该股票最新的选股分析结果：含LLM综合评分、技术/财务/资讯各项打分、操作建议、买入价/目标价/止损价预测",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_picks",
            "description": "获取今日全市场选股推荐清单（Top 10）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_stock",
            "description": "触发完整的选股分析流程（规则扫描+LLM深度评分），获取最新操作建议。适合用户明确要求'分析'某股票时使用",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_backtest_result",
            "description": "获取选股系统的回测验证结果：预测准确率、命中率、失败模式分析、迭代优化建议。适合用户问'上次选股准吗'或'系统表现如何'时使用",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "可选，指定股票代码查该股的回测表现"}
                },
                "required": [],
            },
        },
    },
]

# ── Tool 执行器 ─────────────────────────────────────────────────────────


def _execute_tool(name: str, args: dict[str, Any]) -> str:
    """执行单个 tool 调用，返回 JSON 字符串结果。"""
    if name == "get_stock_info":
        return _tool_stock_info(args["code"])
    elif name == "get_stock_bars":
        return _tool_stock_bars(args["code"])
    elif name == "get_financials":
        return _tool_financials(args["code"])
    elif name == "search_news":
        return _tool_search_news(args.get("query", ""), args.get("code", ""))
    elif name == "get_linked_news":
        return _tool_linked_news(args["code"])
    elif name == "get_pick_result":
        return _tool_pick_result(args["code"])
    elif name == "get_latest_picks":
        return _tool_latest_picks()
    elif name == "analyze_stock":
        return _tool_analyze_stock(args["code"])
    elif name == "get_backtest_result":
        return _tool_backtest(args.get("code", ""))
    else:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


def _tool_stock_info(code: str) -> str:
    from agents.chat_engine import _stock_meta, _concept_list, _rule_score

    meta = _stock_meta(code)
    concepts = _concept_list(code)
    score = _rule_score(code)
    return json.dumps({
        "code": code,
        "industry": meta.get("industry", "未知"),
        "listing_date": str(meta.get("listing_date", "未知")),
        "concepts": concepts[:5],
        "composite_score": score.get("composite_score"),
        "tech_score": score.get("tech_score"),
        "verdict": score.get("verdict"),
    }, ensure_ascii=False, default=str)


def _tool_stock_bars(code: str) -> str:
    from agents.chat_engine import _compact_bars

    bars = _compact_bars(code)
    return json.dumps({"code": code, "bars_summary": bars}, ensure_ascii=False)


def _tool_financials(code: str) -> str:
    from agents.chat_engine import _compact_financials

    fin = _compact_financials(code)
    return json.dumps({"code": code, "financials": fin}, ensure_ascii=False)


def _tool_search_news(query: str, code: str = "") -> str:
    if not INFO_QUERY_LIVE_FETCH:
        return json.dumps({"news": [], "status": "实时搜索未启用"}, ensure_ascii=False)
    try:
        from agents.info_query import fetch_live_hits

        kws = [query[:40]]
        if code:
            kws.append(code)
        hits, status = fetch_live_hits(query, kws[:2], limit=6)
        items = []
        for h in hits[:6]:
            items.append({
                "title": h.title,
                "source": h.source_label,
                "time": str(h.published_at_utc)[:16] if h.published_at_utc else "",
                "url": h.url or "",
            })
        return json.dumps({"news": items, "status": " | ".join(status)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"news": [], "error": str(e)}, ensure_ascii=False)


def _tool_linked_news(code: str) -> str:
    items = get_linked_news_for_stock(code, hours=168, limit=8)
    out = []
    for item in items:
        out.append({
            "title": str(item.get("title") or ""),
            "source": str(item.get("source_label") or ""),
            "time": str(item.get("published_at_utc") or "")[:16],
        })
    return json.dumps({"linked_news": out, "count": len(out)}, ensure_ascii=False)


def _tool_pick_result(code: str) -> str:
    init_db()
    with SessionLocal() as session:
        run = session.execute(
            select(PickRun)
            .where(PickRun.run_kind.in_(["scan", "llm_top300"]))
            .order_by(desc(PickRun.created_at_utc))
            .limit(1)
        ).scalars().first()
        if not run:
            return json.dumps({"error": "暂无选股运行记录"}, ensure_ascii=False)

        entry = session.execute(
            select(PickEntry).where(
                PickEntry.run_id == run.id,
                PickEntry.ts_code == code,
            )
        ).scalars().first()
        if not entry:
            return json.dumps({"error": f"{code} 不在最新选股结果中"}, ensure_ascii=False)

        return json.dumps({
            "code": code,
            "name": entry.name,
            "run_date": str(run.trade_date_as_of or run.created_at_utc)[:10],
            "final_score": entry.final_composite_score,
            "rule_score": entry.rule_composite_score,
            "llm_score": entry.llm_composite_score,
            "llm_technical": entry.llm_technical_score,
            "llm_financial": entry.llm_financial_score,
            "llm_news": entry.llm_news_score,
            "recommendation": entry.recommendation,
            "verdict": entry.verdict,
            "buy_price": entry.predicted_buy_price,
            "target_price": entry.predicted_target_price,
            "stop_loss": entry.predicted_stop_loss,
            "close_price": entry.close_price,
        }, ensure_ascii=False, default=str)


def _tool_latest_picks() -> str:
    from wechat_interact import get_latest_picks

    result = get_latest_picks()
    return json.dumps({
        "picks_text": result.get("reply_text", "")[:2000],
    }, ensure_ascii=False)


def _tool_analyze_stock(code: str) -> str:
    try:
        from pick_wechat import try_handle_pick

        # 获取股票名称
        from agents.shared import load_stock_names
        names = load_stock_names()
        name = None
        for nm, c in names.items():
            if c == code:
                name = nm
                break
        label = name or code
        result = try_handle_pick(f"选股 {label}")
        if result and result.get("reply_text"):
            return json.dumps({
                "code": code,
                "name": name,
                "analysis": str(result["reply_text"])[:3500],
            }, ensure_ascii=False)
        return json.dumps({"error": "选股分析暂不可用"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"分析失败: {e}"}, ensure_ascii=False)


def _tool_backtest(code: str = "") -> str:
    """获取回测验证结果。"""
    try:
        from zplan_shared.pick_predictions import calibration_summary, list_outcomes
        from zplan_shared.pick_iterate_store import list_iterations

        # 校准摘要
        cal = calibration_summary(horizon_days=10)
        summary = {
            "total_outcomes": cal.get("total_outcomes", 0),
            "buy_hit_rate": cal.get("buy_hit_rate"),
            "target_hit_rate": cal.get("target_hit_rate"),
            "stop_loss_hit_rate": cal.get("stop_loss_hit_rate"),
            "avg_return_pct": cal.get("avg_return_pct"),
        }

        # 最近迭代
        iters = list_iterations(limit=3)
        recent = []
        for it in iters:
            recent.append({
                "id": it.get("iteration_id", "")[:16],
                "fail_rate": it.get("fail_rate"),
                "top_failure_tags": it.get("top_failure_tags", [])[:3],
                "suggestions": (it.get("suggestions") or [])[:3],
            })

        # 如果指定了股票，查该股的回测
        stock_outcomes = None
        if code:
            outcomes = list_outcomes(limit=5)
            stock_outcomes = [
                {
                    "ts_code": o.get("ts_code"),
                    "name": o.get("name"),
                    "predicted_buy": o.get("predicted_buy_price"),
                    "actual_low": o.get("actual_low"),
                    "buy_hit": o.get("buy_hit"),
                    "target_hit": o.get("target_hit"),
                    "return_pct": o.get("return_pct"),
                }
                for o in outcomes
                if o.get("ts_code") == code
            ][:3]

        return json.dumps({
            "calibration": summary,
            "recent_iterations": recent,
            "stock_outcomes": stock_outcomes,
        }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"回测数据获取失败: {e}"}, ensure_ascii=False)


# ── Brain 类 ────────────────────────────────────────────────────────────

_BRAIN_SYSTEM = """你是 Z-Plan Brain，A 股量化智能投研系统的中枢。

## 核心能力
你可以调用工具获取实时数据。**在回答用户问题前，先判断是否需要调用工具获取最新数据。**

## 多轮对话
- 用户可能在多轮对话中追问，结合历史上下文理解用户意图
- 如果用户说"它""这个股票""继续"，结合上一轮对话确定指代
- 如果用户已在上一轮提到某股票，本轮不需要重复调 get_stock_info

## 工具使用规则
1. 用户提到具体股票 → 调用 analyze_stock 获取完整 LLM 深度分析（含综合评分、技术/财务/舆情打分、操作建议）
2. 用户问股价/走势 → 调用 get_stock_bars
3. 用户问财报/基本面 → 调用 get_financials
4. 用户问新闻/利好/利空 → 同时调用 get_linked_news + search_news
5. 用户要求「刷新」「重新分析」或对旧数据不满 → 调用 analyze_stock 强制重跑
6. 用户要求「生成报告」→ 先调用 get_stock_info + get_stock_bars + get_financials + get_pick_result，再基于数据生成报告
7. 用户问「推荐股票」「今天买什么」→ 调用 get_latest_picks
8. 单次最多调用 3 个工具，优先并行调用
9. 数据不足时诚实说明，不要编造

## 防幻觉规则（严格遵守）
- **涉及任何具体数字（价格、涨跌幅、评分、PE、市值、营收、利润等），必须先调用工具获取，严禁凭训练数据编造**
- **不要根据发音相似或部分匹配猜测股票名**。只使用工具返回的名称或用户明确写出的名称
- **如果工具返回空或报错，直接告知用户「暂无该数据」，不要编造替代内容**
- **不要在回答中引入用户从未提及、工具也未返回的股票**
- 如果系统提示了「当前会话上下文」中的股票，回答时以该股票为准

## 对话风格
- 专业、直接，不寒暄（"好的""收到"禁止）
- 引用工具返回的具体数据
- 可以用 markdown 表格和列表
- 结尾可提一个简短后续建议

## 选股榜单展示规范
当 get_latest_picks 返回榜单时，每只股票必须展示：
1. 当前股价 + 当日涨跌幅
2. 所属板块（概念标签）
3. 核心理由（LLM 简评结论）
格式示例：用表格或结构化列表，数据引用工具返回的 picks_text 原文。"""


class ZplanBrain:
    """统一智能引擎。"""

    def __init__(self):
        self._tools = TOOLS

    def ask(self, message: str, history: list[dict[str, str]] | None = None,
            current_stock: dict[str, str] | None = None) -> dict[str, Any]:
        """对话入口。

        Args:
            message: 当前用户消息
            history: 可选，之前的对话历史 [{"role":"user","content":...}, ...]
            current_stock: 可选，会话中当前讨论的股票 {ts_code, name}
        """
        t0 = time.time()

        if not deepseek_available():
            return self._fallback_chat(message)

        # 1. 预加载上下文（仅在无会话上下文或用户明确提到新股时执行）
        ctx: dict[str, Any] = {"stocks": [], "live_news": []}
        stock_hint = ""
        has_explicit_new_stock = False

        if current_stock:
            # 有会话上下文时，先检测用户是否明确提到了新股名
            from agents.shared import find_stocks_in_text
            detected = find_stocks_in_text(message)
            current_code = current_stock["ts_code"]
            new_stocks = [(c, n) for c, n in detected if c != current_code]
            if new_stocks:
                # 用户提到了不同股票 → 正常预加载
                has_explicit_new_stock = True
                ctx = gather_chat_context(message)
                stocks = ctx.get("stocks", [])
                if stocks:
                    sd = stocks[0]
                    stock_hint = f"\n系统已检测到用户可能在问 {sd['name']}({sd['code']})。可调用工具获取详细数据。"
            # else: 用户没提新股 → 跳过 gather_chat_context，避免误匹配
        else:
            # 无会话上下文 → 正常检测
            ctx = gather_chat_context(message)
            stocks = ctx.get("stocks", [])
            if stocks:
                sd = stocks[0]
                stock_hint = f"\n系统已检测到用户可能在问 {sd['name']}({sd['code']})。可调用工具获取详细数据。"

        stocks = ctx.get("stocks", [])

        # 2. 会话上下文注入（当前讨论的股票）
        session_hint = ""
        if current_stock:
            cs_code = current_stock["ts_code"]
            cs_name = current_stock.get("name", cs_code)
            session_hint = (
                "\n\n## 当前会话上下文（最高优先级）\n"
                f"用户刚才查看了 {cs_name}({cs_code}) 的选股分析报告。\n"
                "**重要规则：**\n"
                "1. 如果用户追问中没有明确写出另一只股票的名称或代码，"
                f"必须默认用户仍在讨论 {cs_name}({cs_code})，不得自行更换股票。\n"
                "2. 用户说「它」「这个」「这股」等代词时，均指 {cs_name}({cs_code})。\n"
                f"3. 调用工具时应优先使用 {cs_code}。\n"
                "4. 只有用户明确写出其他股票名（如「对比XX」「那看看XX呢」）时才可以切换。\n"
                "5. 严禁根据发音相似或名称部分匹配去猜测其他股票。"
            )

        try:
            messages = [{"role": "system", "content": _BRAIN_SYSTEM + stock_hint + session_hint}]

            # 2. 注入历史上下文（最近 N 轮，含 stock 信息压缩）
            if history:
                # 只保留最近 6 轮（12 条消息）
                recent = history[-12:]
                messages.extend(recent)

            messages.append({"role": "user", "content": message})

            # 2. Function calling 循环（最多 2 轮）
            for _round in range(2):
                resp = _chat_completion(
                    messages,
                    temperature=0.3,
                    max_tokens=min(4096, _effective_max_tokens(2048)),
                    tools=self._tools,
                    tool_choice="auto",
                )

                # 检查是否有 tool_calls
                body = resp.json()
                choice = body["choices"][0]
                msg = choice["message"]
                tool_calls = msg.get("tool_calls") or []

                if not tool_calls:
                    # 无工具调用 → 最终回复
                    reply = str(msg.get("content", "")).strip()
                    if not reply:
                        reply = _extract_text_and_check(resp)
                    return self._format_response(reply, stocks, time.time() - t0,
                                                current_stock=current_stock)

                # 执行工具调用
                messages.append(msg)
                for tc in tool_calls:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    tool_args = json.loads(fn.get("arguments", "{}"))
                    logger.info("Brain tool: %s(%s)", tool_name, tool_args)
                    try:
                        result = _execute_tool(tool_name, tool_args)
                    except Exception as e:
                        result = json.dumps({"error": str(e)}, ensure_ascii=False)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                # 如果第一轮已经用了工具，继续循环让 LLM 生成最终回复

            # 超过最大轮数 → 强制生成最终回复
            messages.append({"role": "user", "content": "请基于以上工具返回的数据，生成最终回复。"})
            final_resp = _chat_completion(
                messages,
                temperature=0.3,
                max_tokens=min(4096, _effective_max_tokens(2048)),
            )
            reply = _extract_text_and_check(final_resp)
            return self._format_response(reply, stocks, time.time() - t0,
                                        current_stock=current_stock)

        except Exception as exc:
            logger.warning("Brain function calling 失败，回退 chat_engine: %s", exc)
            return self._fallback_chat(message)

    def _fallback_chat(self, message: str) -> dict[str, Any]:
        """回退到 chat_engine 的直接 LLM 调用（预加载数据 + 一条 prompt）。"""
        from agents.chat_engine import llm_driven_chat

        return llm_driven_chat(message)

    def _format_response(
        self, reply: str, stocks: list[dict], elapsed: float,
        current_stock: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """格式化回复为 wechat_interact 兼容的 payload。"""
        # 护栏：检测回复是否提到了错误的股票代码
        if current_stock:
            import re as _re
            cs_code = current_stock["ts_code"]
            cs_name = current_stock.get("name", "")
            # 提取回复中所有 6 位数字代码
            codes_in_reply = _re.findall(r'(?<!\d)(\d{6})(?!\d)', reply)
            wrong_codes = [c for c in codes_in_reply if c != cs_code]
            if wrong_codes:
                logger.warning(
                    "Brain 护栏触发: 回复提到了错误股票 %s（当前上下文 %s %s）",
                    wrong_codes, cs_name, cs_code,
                )
                # 追加纠正提示（不修改 LLM 原文，但明确标记）
                reply += (
                    f"\n\n⚠️ 以上回复中提及了 {', '.join(wrong_codes)}，"
                    f"但你当前正在讨论的是 {cs_name}({cs_code})。"
                    f"如需切换股票请明确说明，如「看看{', '.join(wrong_codes)}」。"
                )

        # 截断
        encoded = reply.encode("utf-8")
        if len(encoded) > 3900:
            result = ""
            current = 0
            for ch in reply:
                ch_bytes = len(ch.encode("utf-8"))
                if current + ch_bytes > 3800:
                    result += "\n\n…（已截断）"
                    break
                result += ch
                current += ch_bytes
            reply = result

        primary = stocks[0] if stocks else None
        card = None
        if primary:
            card = {
                "card_type": "button_interaction",
                "main_title": {
                    "title": f"{primary['name']}({primary['code']})",
                    "desc": "LLM 深度分析",
                },
                "task_id": f"brain_{int(time.time() * 1000)}",
                "button_list": [
                    {"text": "刷新分析", "style": 1, "key": f"analyze|{primary['code']}|{primary['name']}"},
                    {"text": "最新快讯", "style": 0, "key": f"news|{primary['code']}|{primary['name']}"},
                    {"text": "持仓备忘", "style": 0, "key": f"watch|{primary['code']}|{primary['name']}"},
                ],
            }

        return {
            "ok": True,
            "intent": "brain_chat",
            "reply_markdown": reply,
            "reply_text": reply,
            "reply_template_card": card,
            "ts_code": primary["code"] if primary else None,
            "name": primary["name"] if primary else None,
            "elapsed_s": elapsed,
        }


# 模块级单例
_brain: ZplanBrain | None = None


def get_brain() -> ZplanBrain:
    global _brain
    if _brain is None:
        _brain = ZplanBrain()
    return _brain
