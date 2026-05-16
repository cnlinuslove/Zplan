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
from topic_admin import list_topics

HELP_TEXT = """【Z-Plan 使用说明】

指令：
· 最新 — 各 topic 最新 X 摘要
· 7天 — 最近 7 天摘要
· 列表 — 全部 topic
· 查 + topic_key — 指定 topic 摘要

问答（直接发问题）：
· 默认现场拉取 Google RSS / 东财快讯等，并结合本地库整理
· 例：北向资金最近怎样、美联储加息、地缘冲突最新

发「帮助」可随时查看本说明。"""

HELP_MARKDOWN = f"### Z-Plan\n> {HELP_TEXT.replace(chr(10), chr(10) + '> ')}"


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
    raw = (message or "").strip()
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
