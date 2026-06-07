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
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

logger = logging.getLogger(__name__)

from agents.info_query import answer_info_question
from agents.news_agent import get_history_payload
from chat_session import session_active, touch_session, expire_session
from config import CHAT_HISTORY_ENABLED
from models import init_db
from pick_wechat import try_handle_pick
from topic_admin import list_topics
from wechat_limits import WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8
from zplan_shared.models import (
    ChatHistory,
    DailyPrice,
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

其它：
· 选股清单 → 今日推荐列表
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

    as_of = (
        run.trade_date_as_of.strftime("%m-%d")
        if run.trade_date_as_of
        else run.created_at_utc.strftime("%m-%d %H:%M")
    )
    lines = [f"【最新选股 · {as_of}】"]
    lines.append(f"规则 {run.rule_version}" + (" · LLM" if run.llm_enabled else ""))
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
        title=f"最新选股 · {as_of}",
        desc=f"Top {len(entries)} · 规则 {run.rule_version}",
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


def _reply_payload(
    intent: str, text: str, *, card: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
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


def _pick_reply(pick: dict[str, Any], text: str) -> dict[str, Any]:
    """构建选股回复，同时尝试推送走势图 + PDF 报告（若已生成）。"""
    chart_path = pick.get("chart_path")
    if chart_path:
        try:
            from wechat_push import push_wechat_image
            push_wechat_image(chart_path)
        except Exception:
            logger.warning("走势图推送失败", exc_info=True)

    pdf_path = pick.get("pdf_path")
    if pdf_path:
        try:
            from wechat_push import push_wechat_file
            push_wechat_file(pdf_path)
        except Exception:
            logger.warning("PDF 推送失败", exc_info=True)

    return _reply_payload(
        str(pick.get("intent") or "pick"),
        text,
        ts_code=pick.get("ts_code"),
        run_id=pick.get("run_id"),
        chart_path=chart_path,
        pdf_path=pdf_path,
    )


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
        result = _handle_inbound_text_impl(message, mentioned=mentioned, chat_id=chat_id)
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

    # ── 退出会话窗口 ──
    if low in ("退出", "exit", "quit", "结束", "结束会话", "关闭"):
        if chat_id:
            expire_session(chat_id)
            return _reply_payload("session_end", "已结束当前会话窗口。再次发送消息时请 @我。")
        return _reply_payload("help", HELP_TEXT)

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

    # ── 选股清单 ──
    if low in ("选股清单", "最新选股", "今日推荐", "top picks", "top picks!", "推荐"):
        return get_latest_picks()

    # ── 概念筛选 ──
    if re.match(r"^(筛选|题材|概念)\s*[：:\s]*(.+)", raw, re.IGNORECASE):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            text = truncate_wechat_utf8(
                str(pick["reply_text"]), WECHAT_TEXT_MAX_BYTES
            )
            return _pick_reply(pick, text)

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

    # ── 选股分析（直接路由，绕过 Brain 减少一次 LLM 往返）───
    # 显式选股前缀：选股/分析/打分/研报/查股/评分 + 标的
    if re.match(r"^(选股|打分|分析|研报|评股|查股|评分)\s*[：:\s]", raw, re.IGNORECASE):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            text = truncate_wechat_utf8(
                str(pick["reply_text"]), WECHAT_TEXT_MAX_BYTES
            )
            return _pick_reply(pick, text)

    # 简单股票名/代码 → 直接深度分析（误判由 try_handle_pick 返回 None 兜底）
    if _looks_like_stock_query(raw):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            text = truncate_wechat_utf8(
                str(pick["reply_text"]), WECHAT_TEXT_MAX_BYTES
            )
            return _pick_reply(pick, text)

    # ── Brain 驱动的统一对话 ──
    # 带多轮对话记忆：读取历史 → Brain → 保存 Q&A
    try:
        from agents.brain import get_brain
        from chat_session import add_message, get_history

        brain = get_brain()
        hist = get_history(chat_id) if chat_id else None
        chat_result = brain.ask(raw, history=hist)

        # 保存本轮对话到会话历史
        reply_text = chat_result.get("reply_markdown") or chat_result.get("reply_text", "")
        if chat_id and reply_text:
            add_message(chat_id, "user", raw)
            add_message(chat_id, "assistant", reply_text)

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
