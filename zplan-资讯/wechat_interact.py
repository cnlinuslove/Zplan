"""
微信侧「发一句话 → 返回回复」的轻量意图解析。

设计给 OpenClaw / 中间件调用：微信收消息入口仍在编排层，本模块负责
根据用户文本生成回复（`reply_text` / `reply_markdown`）+ 可选的模板卡片按钮。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))

from agents.info_query import answer_info_question
from agents.news_agent import get_history_payload
from chat_session import session_active, touch_session, expire_session
from claude_tasks import queue as claude_task_queue
from config import CHAT_HISTORY_ENABLED
from models import init_db
from pick_wechat import try_handle_pick
from topic_admin import list_topics
from wechat_limits import WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8
from zplan_shared.models import (
    ChatHistory,
    DailyPrice,
    MarketForecast,
    PickEntry,
    PickRun,
    SessionLocal,
    StockConceptMember,
    StockList,
)

# ── 股票名快速识别（用于直接路由，绕过 Brain 减少 LLM 往返）───
_STOCK_CODE_RE = re.compile(r"^[0368]\d{5}$")
_QUESTION_MARKERS = re.compile(
    r"(怎么|如何|为什么|为何|什么|哪|吗|？|\?|最近|会不会|能不能|多少|是否|展望|影响)"
)
_IGNORED_SIMPLE = frozenset({
    "帮助", "最新", "7天", "列表", "退出", "结束", "help", "latest",
})

# 批量分析检测：识别 "分析 XXX · 分析 YYY" 或 "分析 XXX 分析 YYY" 等多票请求
_BATCH_PICK_RE = re.compile(
    r"(?:分析|选股|研报|打分)\s*[：:\s]*([一-鿿]{2,6}|[0368]\d{5})",
    re.IGNORECASE,
)
_BATCH_SEPARATOR = re.compile(r"\s*[·•,，、\n]+\s*")


def _split_batch_queries(text: str) -> list[str]:
    """识别批量分析请求，返回各个股票名/代码列表。至少 2 个才算批量。"""
    raw = text.strip()

    # 方法1: 先尝试匹配所有 "指令词 + 股票名" 对
    matches = list(_BATCH_PICK_RE.finditer(raw))
    if len(matches) >= 2:
        return [m.group(1) for m in matches]

    # 方法2: 如果包含分隔符，按分隔符拆分后看每段是否像股票名
    if _BATCH_SEPARATOR.search(raw):
        parts = _BATCH_SEPARATOR.split(raw)
        queries = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 剥离可能的指令词前缀
            sub_m = _BATCH_PICK_RE.search(part)
            if sub_m:
                queries.append(sub_m.group(1))
            else:
                code_m = _STOCK_CODE_RE.match(part)
                name_m = re.match(r"^[一-鿿]{2,6}$", part)
                if code_m or name_m:
                    queries.append(part)
        if len(queries) >= 2:
            return queries

    return []


def _looks_like_stock_query(text: str) -> bool:
    """简单股票名/代码判定，误判会由 try_handle_pick 返回 None 兜底。"""
    s = text.strip()
    if not s or len(s) > 16 or s in _IGNORED_SIMPLE or _QUESTION_MARKERS.search(s):
        return False
    return bool(_STOCK_CODE_RE.match(s) or re.match(r"^[一-鿿]{2,8}$", s))


HELP_TEXT = """【Z-Plan】

查股票：
· 发名称或代码 → LLM 深度分析（含综合评分、技术面、财务、风险）
· 发「分析 爱普股份」→ 强制重跑完整选股分析
· 发「603987 新闻」→ 个股关联快讯
· 发「生成爱普股份分析报告」→ 深度研究报告

查市场：
· 最新 / 7天 → 资讯摘要
· 直接提问 → 北向资金、美联储等
· 筛选 脑机接口 → 题材成份股
· 大盘预测 / 市场预测 → 多空研判 + 板块方向

选股与对比：
· 选股清单 → 今日推荐列表
· 对比 爱普股份 和 平安银行 → 两只股票并排比较

自选与持仓（NEW）：
· 加入自选 爱普股份 → 添加到关注清单
· 我的自选 → 查看自选列表
· 买入 爱普股份 1000股 12.50 → 记录持仓
· 卖出 爱普股份 → 移除持仓
· 我的持仓 → 查看持仓 + 盈亏估算

其它：
· 列表 → 全部 topic
· 帮助 → 本说明
· 退出 → 结束当前会话窗口"""

HELP_MARKDOWN = f"### Z-Plan\n> {HELP_TEXT.replace(chr(10), chr(10) + '> ')}"

# 非 @ 消息且无活跃会话时的提示
SESSION_REQUIRED_TEXT = (
    "请 @我 发起对话，之后 2 小时内可直接发消息，无需再次 @。\n"
    "发送「帮助」查看功能列表。"
)

# 会话窗口激活后的提示（仅首次，追加在回复末尾）
SESSION_ACTIVE_HINT = "\n\n💡 接下来 {} 分钟内，直接发消息即可，无需 @我。"


# ── 模板卡片 ──────────────────────────────────────────────

def _make_card(
    title: str,
    desc: str,
    buttons: list[dict[str, Any]],
) -> dict[str, Any]:
    """构建 button_interaction 模板卡片。"""
    return {
        "card_type": "button_interaction",
        "main_title": {"title": title, "desc": desc},
        "task_id": f"card_{int(time.time() * 1000)}_{id(buttons)}",
        "button_list": buttons,
    }


def _parse_llm_brief(analysis_json: str | None) -> str:
    """从 analysis_process_json 中提取 LLM 简评趋势段，做安全兜底。"""
    if not analysis_json:
        return ""
    try:
        data = json.loads(analysis_json)
        brief = data.get("llm_brief", {})
        if isinstance(brief, dict):
            trend = brief.get("trend", "")
            return str(trend).strip()
        return ""
    except (json.JSONDecodeError, TypeError, KeyError):
        return ""


def _pick_top_concepts(session, ts_code: str, limit: int = 3) -> str:
    """取某股票的前几个概念标签，用 · 分隔。需传入已有 session。"""
    rows = session.execute(
        select(StockConceptMember.concept_name)
        .where(StockConceptMember.ts_code == ts_code)
        .limit(limit * 2)  # 多取一点用于过滤
    ).scalars().all()

    if not rows:
        return ""

    # 过滤掉过于泛化的标签
    skip = {
        "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
        "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
        "央国企改革", "黑龙江", "深圳特区", "机械设备", "通信", "电子", "计算机",
        "公用事业", "电力", "基础化工", "化学制品", "元件", "通信技术", "通信设备",
    }
    concepts = [r for r in rows if r not in skip]
    return " · ".join(concepts[:limit])


# ── 大盘预测 ──────────────────────────────────────────────

def get_latest_forecast() -> dict[str, Any]:
    """最新大盘预测：综合方向 + 多空对照 + 选股参考。"""
    init_db()
    with SessionLocal() as session:
        mf = session.execute(
            select(MarketForecast)
            .order_by(desc(MarketForecast.created_at_utc))
            .limit(1)
        ).scalars().first()

        if not mf:
            return _reply_payload(
                "forecast",
                "暂无大盘预测数据。\n请先运行 market_forecast.py。",
            )

        try:
            f = json.loads(mf.forecast_json) if isinstance(mf.forecast_json, str) else mf.forecast_json
        except (json.JSONDecodeError, TypeError):
            return _reply_payload("forecast", "预测数据格式错误，请稍后重试。")

        md = f.get("market_direction", {})
        direction_map = {"bullish": "🟢 看涨", "bearish": "🔴 看跌", "range-bound": "🟡 震荡"}
        direction_label = direction_map.get(md.get("direction", ""), md.get("direction", "?"))

        lines = [
            f"🔮 大盘预测 · {mf.as_of_date}",
            "",
            f"**综合判断: {direction_label}**（置信度 {md.get('confidence', '?')}%）",
            f"> {md.get('reasoning', '')}",
            "",
        ]

        # 多空对照
        evidence = md.get("evidence") or []
        counter = md.get("counter_evidence") or []
        if evidence or counter:
            lines.append("**⚖️ 多空对照**")
            for e in evidence:
                lines.append(f"🔺 [{e.get('type', '')}] {e.get('signal', '')}: {e.get('value', '')}")
            for c in counter:
                lines.append(f"🔻 {c}")
            lines.append("")

        # 指数全景
        ix_forecasts = f.get("index_forecasts") or []
        if ix_forecasts:
            bullish_ix = [ix for ix in ix_forecasts if ix.get("direction") == "偏多"]
            bearish_ix = [ix for ix in ix_forecasts if ix.get("direction") == "偏空"]
            neutral_ix = [ix for ix in ix_forecasts if ix.get("direction") == "震荡"]
            lines.append(
                f"🏛️ 指数: 🔺偏多{len(bullish_ix)}只 ➖震荡{len(neutral_ix)}只 🔻偏空{len(bearish_ix)}只"
            )
            for ix in ix_forecasts:
                emoji = {"偏多": "🔺", "偏空": "🔻", "震荡": "➖"}.get(ix.get("direction", ""), "❓")
                sp = ix.get("similar_patterns_verdict", "")
                sp_str = f" · 历史相似: {sp}" if sp else ""
                lines.append(f"  {emoji} {ix.get('name', '')}: {ix.get('direction', '')}（置信{ix.get('confidence', '?')}%）{sp_str}")
            lines.append("")

        # 板块
        sectors = f.get("sector_calls") or []
        if sectors:
            lines.append("🏭 板块判断:")
            for s in sectors:
                emoji = {"看多": "🟢", "看淡": "🔴", "中性": "⚪"}.get(s.get("direction", ""), "")
                lines.append(f"  {emoji} {s.get('sector', '')}: {s.get('reasoning', '')[:60]}")
            lines.append("")

        # 选股参考
        dir_signal = md.get("direction", "")
        pick_guide = {
            "bullish": "🟢 偏多 → 可积极选股，重点看偏多指数对应标的",
            "bearish": "🔴 偏空 → 降低仓位防守，关注逆势板块",
            "range-bound": "🟡 震荡 → 控制仓位精选个股，不追高",
        }.get(dir_signal, "等待更明确信号")
        lines.append(f"📋 选股参考: {pick_guide}")
        lines.append("")
        lines.append("💡 发送「选股清单」查看今日推荐")

        return _reply_payload("forecast", "\n".join(lines))


# ── 选股清单 ──────────────────────────────────────────────

def get_latest_picks() -> dict[str, Any]:
    """最新选股清单：规则+LLM 排行榜 Top 10。

    每行包含：股价、当日涨幅、所属板块、核心理由。
    """
    init_db()
    with SessionLocal() as session:
        run = session.execute(
            select(PickRun)
            .where(PickRun.run_kind.in_(["scan", "llm_top300"]))
            .order_by(desc(PickRun.created_at_utc))
            .limit(1)
        ).scalars().first()

        if not run:
            return _reply_payload(
                "picks_list",
                "暂无选股运行记录。\n请先运行选股扫描。",
            )

        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run.id)
            .order_by(
                PickEntry.rank_in_run,
                PickEntry.final_composite_score.desc().nullslast(),
            )
            .limit(10)
        ).scalars().all()

        # 批量查行业 & 当日行情
        codes = [e.ts_code for e in entries]
        as_of_date = run.trade_date_as_of

        # 行业
        industry_map: dict[str, str] = {}
        if codes:
            rows = session.execute(
                select(StockList.ts_code, StockList.industry)
                .where(StockList.ts_code.in_(codes))
            ).all()
            industry_map = {r.ts_code: (r.industry or "") for r in rows}

        # 当日涨跌幅
        pct_map: dict[str, float | None] = {}
        close_map: dict[str, float | None] = {}
        if codes and as_of_date:
            rows = session.execute(
                select(DailyPrice.ts_code, DailyPrice.close, DailyPrice.pct_chg)
                .where(
                    DailyPrice.ts_code.in_(codes),
                    DailyPrice.trade_date == as_of_date,
                )
            ).all()
            pct_map = {r.ts_code: r.pct_chg for r in rows}
            close_map = {r.ts_code: r.close for r in rows}

    data_date = (
        run.trade_date_as_of.strftime("%m-%d")
        if run.trade_date_as_of
        else run.created_at_utc.strftime("%m-%d %H:%M")
    )
    today_str = datetime.now(BEIJING_TZ).strftime("%m-%d")
    lines = [f"【今日选股 TOP10】{today_str}"]
    lines.append(f"数据截止 {data_date}  |  规则 {run.rule_version}" + (" · LLM" if run.llm_enabled else ""))
    lines.append("")

    for e in entries:
        score = e.final_composite_score or e.rule_composite_score
        score_str = f"{score:.0f}" if score is not None else "--"
        nm = e.name or e.ts_code

        # 股价 & 涨幅
        price = close_map.get(e.ts_code) or e.close_price
        price_str = f"¥{price:.2f}" if price is not None else "--"
        pct = pct_map.get(e.ts_code)
        if pct is not None:
            arrow = "↑" if pct >= 0 else "↓"
            pct_str = f"{arrow}{abs(pct):.2f}%"
        else:
            pct_str = "--"

        # 板块
        industry = industry_map.get(e.ts_code, "")
        concepts = _pick_top_concepts(session, e.ts_code, limit=3)
        sector_line = industry if industry else ""
        if concepts:
            sector_line = f"{sector_line} | {concepts}" if sector_line else concepts

        # 核心理由（从 LLM 分析中提取）
        reason = _parse_llm_brief(e.analysis_process_json)

        lines.append(
            f"{e.rank_in_run or '-'}. {nm}({e.ts_code}) {price_str} {pct_str} 评分{score_str}"
        )
        if sector_line:
            lines.append(f"   📌 {sector_line}")
        if reason:
            lines.append(f"   💡 {reason}")

    lines.append("")
    lines.append("点击下方按钮，或发送「分析 股票名」查看详情")

    card = _make_card(
        title=f"今日选股 TOP10 · {today_str}",
        desc=f"数据截止 {data_date} · Top {len(entries)} · 规则 {run.rule_version}",
        buttons=[
            {"text": "刷新清单", "style": 1, "key": "picklist"},
            {"text": "分析某股", "style": 0, "key": "picklist_analyze"},
        ],
    )
    return _reply_payload("picks_list", "\n".join(lines), card=card)


# ── 路由核心 ──────────────────────────────────────────────

def _normalize_user_text(message: str) -> str:
    """去掉企微 @机器人 前缀。"""
    raw = (message or "").strip()
    if not raw:
        return raw
    raw = re.sub(r"^@\S+\s*", "", raw, count=1).strip()
    return raw or (message or "").strip()


_POLL_INTERVAL_SECONDS = 60


def _next_poll_eta() -> str:
    """返回轮询器下次检查的预估时间（人性化）。"""
    return f"最多 {_POLL_INTERVAL_SECONDS} 秒"


# ── 上下文功能提示 ──────────────────────────────────────────

_HINT_MAP: dict[str, str] = {
    "pick": "💡 新功能：发送「加入自选」收藏 | 「对比 两只股票」横向比较 | 「买入 1000股 价格」记录持仓",
    "pick_symbol": "💡 新功能：发送「加入自选」收藏 | 「对比 两只股票」横向比较 | 「买入 1000股 价格」记录持仓",
    "pick_screen": "💡 发送「选股 名称」查看个股深度分析 | 「对比 A 和 B」并排比较",
    "picks_list": "💡 发送「选股 名称」查看个股分析 | 「对比 A 和 B」横向比较两股",
    "forecast": "💡 发送「选股清单」看今日推荐 | 直接发股票名深度分析 | 「筛选 题材名」选标的",
    "watchlist": "💡 发送「选股 名称」分析自选股 | 「我的持仓」查看仓位 | 「对比 A B」比较",
    "watch_add": "💡 发送「选股」分析该股 | 「我的持仓」记录买入",
    "positions": "💡 发送「选股 名称」分析持仓股 | 「对比 A B」横向比较 | 「加入自选 XX」扩展关注",
    "buy": "💡 发送「选股」跟踪分析 | 「对比」比较 | 「我的持仓」查看全部",
    "sell": "💡 发送「选股 名称」发掘新标的 | 「我的自选」管理关注清单",
    "compare": "💡 发送「加入自选 XX」收藏 | 「买入 XX 1000股 价格」记录持仓",
    "brain_chat": "💡 新功能：持仓追踪、股票对比、自选管理。发送「帮助」查看全部功能",
    "help": "💡 新上线：持仓追踪、股票对比、多轮追问。选股报告后可追问「目标价合理吗？」",
    "history_latest": "💡 发送「选股清单」看推荐 | 直接提问如「北向资金最近走势如何」",
    "topic_list": "💡 发送「查 北向资金」按 topic 搜索 | 「最新」看今日快讯",
    "claude_task": "💡 发送「claude 任务描述」让 Claude 远程改代码，完成后企微推送结果",
}

# 不追加提示的意图（报错、会话管理等）
_HINT_SKIP = frozenset({
    "empty", "session_required", "session_end", "pick_error",
    "pick_timeout", "pick_skip", "watch_error", "watchlist_error",
    "positions_error", "buy_error", "sell_error", "compare_error",
    "button_unknown", "picklist_analyze", "info_query",
})


def _hint_for(intent: str, name: str = "") -> str:
    """根据意图返回单行功能提示，空串表示无提示。"""
    if intent in _HINT_SKIP:
        return ""
    return _HINT_MAP.get(intent, "")


def _reply_payload(
    intent: str, text: str, *, card: dict[str, Any] | None = None,
    hint_name: str = "", **extra: Any
) -> dict[str, Any]:
    hint = _hint_for(intent, name=hint_name)
    if hint and hint not in text:
        text = text + "\n\n" + hint
    result: dict[str, Any] = {
        "ok": True,
        "intent": intent,
        "reply_text": text,
        "reply_markdown": text if intent == "help" else f"### 资讯回复\n{text}",
        **extra,
    }
    if card:
        result["reply_template_card"] = card
    return result


def _capture_pick_context(chat_id: str | None, result: dict[str, Any]) -> None:
    """记录选股结果到会话上下文，供后续多轮追问使用。"""
    if not chat_id:
        return
    ts = result.get("ts_code")
    name = result.get("name")
    if ts:
        from chat_session import set_current_stock, set_last_intent

        set_current_stock(chat_id, ts, name or ts)
        set_last_intent(chat_id, result.get("intent", "pick"))


def _resolve_watch_symbol(query: str) -> tuple[str, str]:
    """解析自选股票名 → (ts_code, name)。"""
    from agents.user_position import _resolve_symbol
    return _resolve_symbol(query)


def _handle_button_click(key: str) -> dict[str, Any]:
    """处理模板卡片按钮点击。key 格式: action|code|name 或 action。"""
    parts = key.split("|")
    action = parts[0]
    code = parts[1] if len(parts) >= 2 else ""
    name = parts[2] if len(parts) >= 3 else code

    if action == "analyze" and code:
        pick = try_handle_pick(f"选股 {name or code}")
        if pick and pick.get("reply_text"):
            return _pick_reply(pick, str(pick["reply_text"]))
        return _reply_payload("pick_error", f"分析 {name or code} 暂不可用，请稍后重试。")

    if action == "news" and code:
        from agents.info_query import answer_info_question
        try:
            result = answer_info_question(f"{name} {code} 最近新闻")
            return _reply_payload("info_query", result["text"],
                                  keywords=result.get("keywords"),
                                  hit_count=result.get("count"))
        except Exception:
            return _reply_payload("info_query", f"查询 {name or code} 资讯失败，请稍后重试。")

    if action == "watch" and code:
        try:
            from zplan_shared.pick_watchlist import add_watch
            result = add_watch(name or code)
            return _reply_payload(
                "watch_add", f"✅ 已添加 {result['name']}({result['ts_code']}) 到自选清单。\n发送「我的自选」查看全部。",
                hint_name=result.get("name", ""),
            )
        except Exception as e:
            return _reply_payload("watch_error", f"添加自选失败: {e}")

    if action == "picklist":
        return get_latest_picks()

    if action == "picklist_analyze":
        return _reply_payload("picklist_analyze", "请回复股票名或代码进行分析，如「选股 爱普股份」。")

    return _reply_payload("button_unknown", f"按钮操作「{action}」暂不支持。")


def _pick_reply(pick: dict[str, Any], text: str) -> dict[str, Any]:
    """构建选股回复，PDF 报告通过群机器人 webhook 推送。

    文本回复使用 reply_markdown（企微 4096 字节限制），
    便于嵌入可点击的资讯链接；reply_text 保留短版兜底。
    """
    pdf_path = pick.get("pdf_path")

    # PDF 报告通过群机器人 webhook 推送（不再单独推送 K 线图，图表已嵌入 PDF）
    if pdf_path:
        try:
            from wechat_push import push_wechat_file
            push_wechat_file(pdf_path)
        except Exception:
            logger.warning("PDF 推送失败", exc_info=True)

    # 构建可点击链接的 markdown 版（资讯 URL 使用 [标题](url) 格式）
    md_text = _to_pick_markdown(text)
    short_text = truncate_wechat_utf8(text, WECHAT_TEXT_MAX_BYTES)

    # 追加上下文功能提示
    pick_intent = str(pick.get("intent") or "pick")
    stock_name = pick.get("name", "")
    hint = _hint_for(pick_intent, name=stock_name)
    if hint:
        short_text = (short_text + "\n\n" + hint) if short_text else short_text
        md_text = md_text + "\n\n" + hint

    result: dict[str, Any] = {
        "ok": True,
        "intent": pick_intent,
        "reply_text": short_text if len(short_text.encode("utf-8")) <= WECHAT_TEXT_MAX_BYTES else "",
        "reply_markdown": md_text,
        "ts_code": pick.get("ts_code"),
        "name": pick.get("name"),
        "run_id": pick.get("run_id"),
        "chart_path": pick.get("chart_path"),
        "pdf_path": pdf_path,
    }
    return result


def _handle_batch_pick(
    queries: list[str],
    *,
    chat_id: str | None = None,
) -> dict[str, Any] | None:
    """批量分析多只股票：每只单独生成研报（PDF + 回复），汇总提示。

    每个 query 会走完整的 try_handle_pick → _pick_reply 路径，
    PDF 通过 webhook 推送，文本回复仅返回汇总提示避免刷屏。
    """
    if not queries or len(queries) < 2:
        return None

    import concurrent.futures

    names: list[str] = []
    errors: list[str] = []

    def _run_one(q: str) -> tuple[str, str | None]:
        """返回 (query, error_or_None)。"""
        pick = try_handle_pick(f"分析 {q}")
        if pick and pick.get("reply_text"):
            # _pick_reply 会推送 PDF，我们只需要知道成功
            _pick_reply(pick, str(pick["reply_text"]))
            name = pick.get("name") or q
            return (name, None)
        elif pick and pick.get("intent") == "pick_error":
            return (q, pick.get("reply_text", "分析失败"))
        else:
            return (q, "无法识别该股票")

    # 顺序执行（避免并发导致 LLM API 限流和 DB 锁竞争）
    for q in queries:
        name, err = _run_one(q.strip())
        if err:
            errors.append(f"{q}: {err[:60]}")
        else:
            names.append(name)

    if not names and errors:
        return _reply_payload(
            "batch_pick",
            f"批量分析全部失败：\n" + "\n".join(errors[:5]),
        )

    lines = [
        f"📊 批量研报生成中（{len(names)}/{len(queries)} 只）",
        "",
        f"✅ 已生成: {'、'.join(names[:8])}",
    ]
    if errors:
        lines.append(f"⚠️ 失败: {'、'.join(errors[:3])}")

    lines.append("")
    lines.append("💡 每只股票的完整研报 PDF 正在推送中，请向上翻看")
    lines.append("> 也可单独发送「分析 股票名」获取单只研报")

    return _reply_payload("batch_pick", "\n".join(lines))


def _to_pick_markdown(plain: str) -> str:
    """将选股纯文本转为企微 markdown：纯文本 URL → [📎阅读原文](url) 可点击。"""
    import re as _re
    lines = plain.split("\n")
    out: list[str] = []
    prev_title: str | None = None  # 上一行标题（用于 URL 标签）
    url_pattern = _re.compile(r"^( {2,})(https?://\S+)\s*$")
    title_pattern = _re.compile(r"^· (.+)$")
    for line in lines:
        m = url_pattern.match(line)
        if m:
            url = m.group(2)
            # 优先用上一行的标题文本，否则用"阅读原文"
            label = (prev_title or "阅读原文")[:30]
            out.append(f"[📎{label}]({url})")
            prev_title = None
            continue
        # 记录标题行，供下一行 URL 使用
        tm = title_pattern.match(line)
        prev_title = tm.group(1)[:30] if tm else None
        out.append(line)
    return "\n".join(out)


def _save_chat_history(
    *,
    channel: str,
    user_id: str | None,
    chat_id: str | None,
    user_message: str,
    bot_intent: str | None,
    bot_reply: str | None,
    error: str | None,
    elapsed_ms: int,
) -> None:
    """持久化一条对话记录（受 CHAT_HISTORY_ENABLED 开关控制）。"""
    if not CHAT_HISTORY_ENABLED:
        return
    try:
        with SessionLocal() as session:
            session.add(
                ChatHistory(
                    channel=channel,
                    user_id=user_id,
                    chat_id=chat_id,
                    user_message=user_message,
                    bot_intent=bot_intent,
                    bot_reply=bot_reply,
                    error=error,
                    elapsed_ms=elapsed_ms,
                )
            )
            session.commit()
    except Exception:
        logger.exception("保存 chat_history 失败（不影响主流程）")


def handle_inbound_text(
    message: str,
    *,
    user_id: str | None = None,
    channel: str = "unknown",
    chat_id: str | None = None,
    mentioned: bool = False,
) -> dict[str, Any]:
    """解析用户消息并返回回复，同时持久化对话记录。

    新增可选参数（向后兼容）：
    - user_id: 企微用户 OpenID
    - channel: 通道标识（wecom_bot / wework_app / http_bridge / cli）
    - chat_id: 群聊 ID
    - mentioned: 消息是否显式 @了机器人（用于会话窗口判断）
    """
    t0 = time.time()
    result: dict[str, Any] | None = None
    error: str | None = None
    is_new_session = False
    try:
        # @ 消息：刷新会话窗口（在 impl 之前，以便 impl 内 session_active 检查生效）
        if mentioned and chat_id:
            is_new_session = touch_session(chat_id)
        result = _handle_inbound_text_impl(message, mentioned=mentioned, chat_id=chat_id, user_id=user_id)
        # 新会话追加有效时长提示（智能机器人渠道除外，因平台要求必须 @）
        if is_new_session and result and result.get("reply_text") and channel != "wecom_bot":
            from chat_session import get_session_store
            ttl_min = get_session_store().ttl_seconds // 60
            hint = SESSION_ACTIVE_HINT.format(ttl_min)
            result["reply_text"] += hint
            if result.get("reply_markdown"):
                result["reply_markdown"] += hint
        return result
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        raise
    finally:
        elapsed_ms = int((time.time() - t0) * 1000)
        _save_chat_history(
            channel=channel,
            user_id=user_id,
            chat_id=chat_id,
            user_message=message,
            bot_intent=result.get("intent") if result else None,
            bot_reply=result.get("reply_text") if result else None,
            error=error,
            elapsed_ms=elapsed_ms,
        )


def _handle_inbound_text_impl(
    message: str,
    *,
    mentioned: bool = False,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    init_db()
    raw = _normalize_user_text(message)

    # ── 会话窗口检查 ──
    # 非 @ 消息：需要活跃会话窗口，否则引导用户 @ 机器人
    if not mentioned and chat_id and not session_active(chat_id):
        # 排除无内容消息（空消息不应触发提示）
        if not raw:
            return _reply_payload("empty", "")
        return _reply_payload("session_required", SESSION_REQUIRED_TEXT)

    if not raw or raw.lower() in ("help", "帮助", "?", "？"):
        return _reply_payload("help", HELP_TEXT)

    low = raw.lower()

    # ── 模板卡片按钮点击 ──
    if raw.startswith("__btn__"):
        return _handle_button_click(raw[7:])

    # ── 退出会话窗口 ──
    if low in ("退出", "exit", "quit", "结束", "结束会话", "关闭"):
        if chat_id:
            expire_session(chat_id)
            return _reply_payload("session_end", "已结束当前会话窗口。再次发送消息时请 @我。")
        return _reply_payload("help", HELP_TEXT)

    # ── Claude Code 远程任务 ──
    if low.startswith("claude ") or low.startswith("@claude ") or low.startswith("claude,"):
        task_text = re.sub(r"^(?:@?claude[,\s]+)", "", raw, flags=re.IGNORECASE).strip()
        if not task_text:
            return _reply_payload(
                "claude_task",
                "请描述需要 Claude 处理的任务。\n"
                "示例：「claude 修改选股报告格式，把风险提示放在最前面」",
            )
        task = claude_task_queue.create_task(
            text=task_text,
            user_id=user_id or "",
            chat_id=chat_id or "",
        )
        tid_short = task["id"][:8]
        text_preview = task_text[:150] + ("…" if len(task_text) > 150 else "")
        return _reply_payload(
            "claude_task",
            f"📋 任务已入队\n\n"
            f"ID: `{tid_short}…`\n"
            f"内容: {text_preview}\n\n"
            f"Claude 将在 {_next_poll_eta()} 内开始处理，完成后企微推送结果。",
        )

    # ── Topic 摘要 ──
    if low in ("最新", "latest", "摘要"):
        payload = get_history_payload("latest", None)
        text = f"【最新 X 摘要】\n{payload['wechat_text']}"
        return _reply_payload("history_latest", text, count=payload["count"])

    if low in ("7天", "7d", "一周") or raw in ("最近7天",):
        payload = get_history_payload("7d", None)
        text = f"【最近 7 天】\n{payload['wechat_text']}"
        return _reply_payload("history_7d", text, count=payload["count"])

    if low in ("列表", "topics", "topic"):
        topics = list_topics(echo=False)
        lines = [
            f"- {t['topic_key']} · {t['display_name']} · {'开' if t['enabled'] else '关'}"
            for t in topics
        ]
        body = "\n".join(lines) if lines else "(暂无 topic)"
        return _reply_payload("topic_list", f"【Topic 列表】\n{body}")

    # ── 自选管理 ──
    if re.match(r"^(加入自选|添加自选|关注)\s+", raw):
        symbol = re.sub(r"^(加入自选|添加自选|关注)\s+", "", raw).strip()
        try:
            from zplan_shared.pick_watchlist import add_watch
            result = add_watch(symbol)
            return _reply_payload(
                "watch_add", f"✅ 已添加 {result['name']}({result['ts_code']}) 到自选清单。\n发送「我的自选」查看全部。",
                hint_name=result.get("name", ""),
            )
        except LookupError as e:
            return _reply_payload("watch_error", f"❌ {e}")
        except Exception as e:
            return _reply_payload("watch_error", f"添加失败: {e}")

    if re.match(r"^(移除自选|删除自选|取消关注)\s+", raw):
        symbol = re.sub(r"^(移除自选|删除自选|取消关注)\s+", "", raw).strip()
        try:
            from zplan_shared.pick_watchlist import remove_watch
            # resolve first to get name
            code, name = _resolve_watch_symbol(symbol)
            ok = remove_watch(code)
            if ok:
                return _reply_payload("watch_remove", f"✅ 已从自选移除 {name or code}({code})。")
            return _reply_payload("watch_remove", f"「{symbol}」不在自选清单中。")
        except LookupError as e:
            return _reply_payload("watch_error", f"❌ {e}")

    if low in ("我的自选", "自选列表", "自选", "关注列表"):
        try:
            from agents.user_position import format_watchlist_text
            return _reply_payload("watchlist", format_watchlist_text())
        except Exception as e:
            return _reply_payload("watchlist_error", f"获取自选失败: {e}")

    # ── 持仓管理 ──
    if re.match(r"^(我的持仓|持仓|持仓情况|仓位|我的仓位)$", raw):
        if not user_id:
            return _reply_payload("positions", "持仓功能需要用户身份。请在企微中 @我 使用。")
        try:
            from agents.user_position import format_positions_text
            return _reply_payload("positions", format_positions_text(user_id))
        except Exception as e:
            return _reply_payload("positions_error", f"获取持仓失败: {e}")

    buy_parsed = None
    try:
        from agents.user_position import parse_buy_command
        buy_parsed = parse_buy_command(raw)
    except ImportError:
        pass
    if buy_parsed:
        if not user_id:
            return _reply_payload("positions", "持仓功能需要用户身份。请在企微中 @我 使用。")
        try:
            from agents.user_position import add_position
            result = add_position(
                user_id, buy_parsed["symbol"],
                buy_parsed["shares"], buy_parsed["price"],
                notes=buy_parsed.get("notes"),
            )
            act = "已更新" if result.get("action") == "updated" else "已记录"
            price_str = f" @¥{result['buy_price']:.2f}" if result.get("buy_price") else ""
            return _reply_payload(
                "buy", f"✅ {act} {result['name']}({result['ts_code']}) "
                f"{result['shares']}股{price_str}。\n发送「我的持仓」查看。",
                hint_name=result.get("name", ""),
            )
        except LookupError as e:
            return _reply_payload("buy_error", f"❌ {e}")
        except Exception as e:
            return _reply_payload("buy_error", f"买入记录失败: {e}")

    sell_symbol = None
    try:
        from agents.user_position import parse_sell_command
        sell_symbol = parse_sell_command(raw)
    except ImportError:
        pass
    if sell_symbol:
        if not user_id:
            return _reply_payload("positions", "持仓功能需要用户身份。请在企微中 @我 使用。")
        try:
            from agents.user_position import remove_position
            info = remove_position(user_id, sell_symbol)
            if info:
                return _reply_payload(
                    "sell", f"✅ 已移除 {info['name']}({info['ts_code']}) {info['shares']}股。",
                    hint_name=info.get("name", ""),
                )
            return _reply_payload("sell", f"「{sell_symbol}」不在你的持仓中。")
        except LookupError as e:
            return _reply_payload("sell_error", f"❌ {e}")
        except Exception as e:
            return _reply_payload("sell_error", f"卖出记录失败: {e}")

    # ── 大盘预测 ──
    if low in ("大盘预测", "大盘", "预测", "市场预测", "forecast", "market forecast"):
        return get_latest_forecast()

    # ── 选股清单 ──
    if low in ("选股清单", "最新选股", "今日推荐", "top picks", "top picks!", "推荐"):
        return get_latest_picks()

    # ── 概念筛选 ──
    if re.match(r"^(筛选|题材|概念)\s*[：:\s]*(.+)", raw, re.IGNORECASE):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            result = _pick_reply(pick, str(pick["reply_text"]))
            _capture_pick_context(chat_id, result)
            return result

    # ── Topic 查询 ──
    m = re.match(r"查\s*(\S+)", raw)
    key = m.group(1) if m else raw
    topics = list_topics(echo=False)
    for t in topics:
        if key == t["topic_key"] or key == t["display_name"]:
            payload = get_history_payload("latest", t["topic_key"])
            text = f"【{t['display_name']}】\n{payload['wechat_text']}"
            return _reply_payload(
                "history_topic",
                text,
                topic_key=t["topic_key"],
                count=payload["count"],
            )

    # ── 股票对比 ──
    compare_pair = None
    try:
        from agents.compare import parse_compare_command
        compare_pair = parse_compare_command(raw)
    except ImportError:
        pass
    if compare_pair:
        try:
            from agents.compare import compare_two
            result = compare_two(compare_pair[0], compare_pair[1])
            return _reply_payload(
                result.get("intent", "compare"),
                result["reply_text"],
            )
        except Exception as e:
            return _reply_payload("compare_error", f"对比失败: {e}")

    # ── 批量分析：识别 "分析 A · 分析 B · 分析 C" ──
    batch_queries = _split_batch_queries(raw)
    if batch_queries:
        result = _handle_batch_pick(batch_queries, chat_id=chat_id)
        if result:
            return result

    # ── 选股分析（直接路由，绕过 Brain 减少一次 LLM 往返）───
    # 显式选股前缀：选股/分析/打分/研报/查股/评分 + 标的
    if re.match(r"^(选股|打分|分析|研报|评股|查股|评分)\s*[：:\s]", raw, re.IGNORECASE):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            result = _pick_reply(pick, str(pick["reply_text"]))
            _capture_pick_context(chat_id, result)
            return result

    # 简单股票名/代码 → 直接深度分析（误判由 try_handle_pick 返回 None 兜底）
    if _looks_like_stock_query(raw):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            result = _pick_reply(pick, str(pick["reply_text"]))
            _capture_pick_context(chat_id, result)
            return result

    # ── Brain 驱动的统一对话 ──
    # 带多轮对话记忆：读取历史 → Brain → 保存 Q&A
    try:
        from agents.brain import get_brain
        from chat_session import add_message, get_history, get_current_stock

        brain = get_brain()
        hist = get_history(chat_id) if chat_id else None
        cur_stock = get_current_stock(chat_id) if chat_id else None
        chat_result = brain.ask(raw, history=hist, current_stock=cur_stock)

        # 保存本轮对话到会话历史
        reply_text = chat_result.get("reply_markdown") or chat_result.get("reply_text", "")
        if chat_id and reply_text:
            add_message(chat_id, "user", raw)
            add_message(chat_id, "assistant", reply_text)

        # 若 Brain 返回了具体股票结果，更新会话上下文
        if chat_id and chat_result.get("ts_code"):
            from chat_session import set_current_stock
            set_current_stock(chat_id, chat_result["ts_code"],
                            chat_result.get("name", chat_result["ts_code"]))

        return _reply_payload(
            chat_result.get("intent", "brain_chat"),
            reply_text,
            ts_code=chat_result.get("ts_code"),
            card=chat_result.get("reply_template_card"),
            elapsed_s=chat_result.get("elapsed_s"),
        )
    except Exception as exc:
        # Brain 失败 → 回退 chat_engine
        logger.warning("Brain 失败，回退 chat_engine: %s", exc)
        try:
            from agents.chat_engine import llm_driven_chat

            chat_result = llm_driven_chat(raw)
            return _reply_payload(
                chat_result.get("intent", "llm_chat"),
                chat_result.get("reply_markdown", chat_result.get("reply_text", "")),
                ts_code=chat_result.get("ts_code"),
                card=chat_result.get("reply_template_card"),
                elapsed_s=chat_result.get("elapsed_s"),
                data_sources=chat_result.get("data_sources"),
            )
        except Exception as exc2:
            # 最终回退到旧版问答
            logger.warning("chat_engine 也失败，回退 info_query: %s", exc2)
            result = answer_info_question(raw)
            return _reply_payload(
                "info_query",
                result["text"],
                keywords=result["keywords"],
                hit_count=result["count"],
                hits=result["hits"],
            )
