"""
微信侧「发一句话 → 返回回复」的轻量意图解析。

设计给 OpenClaw / 中间件调用：微信收消息入口仍在编排层，本模块负责
根据用户文本生成回复（`reply_text` / `reply_markdown`）。
"""
from __future__ import annotations

import re
from typing import Any

from agents.info_query import answer_info_question
from agents.news_agent import get_history_payload
from models import init_db
from pick_wechat import try_handle_pick
from topic_admin import list_topics
from wechat_limits import WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8

HELP_TEXT = """【Z-Plan 使用说明】

资讯：
· 最新 — 各 topic 最新 X 摘要
· 7天 — 最近 7 天摘要
· 列表 — 全部 topic
· 查 + topic_key — 指定 topic 摘要

选股打分：
· 选股 爱普股份 — 规则+LLM 简评（可只发「爱普股份」）
· 打分 / 分析 / 研报 + 名称或 6 位代码
· 筛选 脑机接口 — 按题材成份（需先同步概念库）

问答（直接发问题）：
· 例：北向资金最近怎样、美联储加息

发「帮助」可随时查看本说明。"""

HELP_MARKDOWN = f"### Z-Plan\n> {HELP_TEXT.replace(chr(10), chr(10) + '> ')}"


def _normalize_user_text(message: str) -> str:
    """去掉企微 @机器人 前缀，便于匹配「帮助」等指令。"""
    raw = (message or "").strip()
    if not raw:
        return raw
    # @Zplan 帮助 / @Zplan  帮助
    raw = re.sub(r"^@\S+\s*", "", raw, count=1).strip()
    return raw or (message or "").strip()


def _reply_payload(intent: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "intent": intent,
        "reply_text": text,
        "reply_markdown": text if intent == "help" else f"### 资讯回复\n{text}",
        **extra,
    }


def handle_inbound_text(message: str) -> dict[str, Any]:
    init_db()
    raw = _normalize_user_text(message)
    if not raw or raw.lower() in ("help", "帮助", "?", "？"):
        return _reply_payload("help", HELP_TEXT)

    low = raw.lower()
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

    pick = try_handle_pick(raw)
    if pick and pick.get("reply_text"):
        text = truncate_wechat_utf8(str(pick["reply_text"]), WECHAT_TEXT_MAX_BYTES)
        return _reply_payload(
            str(pick.get("intent") or "pick"),
            text,
            ts_code=pick.get("ts_code"),
            run_id=pick.get("run_id"),
        )

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

    # 自然语言问答：检索 financial_alerts / global_news / 情绪因子
    result = answer_info_question(raw)
    return _reply_payload(
        "info_query",
        result["text"],
        keywords=result["keywords"],
        hit_count=result["count"],
        hits=result["hits"],
    )
