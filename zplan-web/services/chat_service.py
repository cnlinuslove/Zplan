"""Chat 服务：连接 FastAPI → wechat_interact → LLM，支持 SSE 流式输出。"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import (
    DEEPSEEK_API_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    LLM_CACHE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# 确保 zplan-资讯 在 sys.path 以便 import wechat_interact
_INFO_DIR = Path(__file__).resolve().parents[2] / "zplan-资讯"
if str(_INFO_DIR) not in sys.path:
    sys.path.insert(0, str(_INFO_DIR))

_SHARED_SRC = Path(__file__).resolve().parents[2] / "zplan-共享" / "src"
if str(_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_SHARED_SRC))


# ── LLM 缓存 ──

def _cache_key(system_prompt: str, user_text: str, model: str) -> str:
    raw = f"{model}:{system_prompt}:{user_text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _cached_response(cache_key_str: str) -> dict | None:
    """从 SQLite 读取缓存的 LLM 响应（未过期）。"""
    from zplan_shared.models import LlmResponseCache, SessionLocal

    db: Session = SessionLocal()
    try:
        row = db.scalar(
            select(LlmResponseCache).where(LlmResponseCache.cache_key == cache_key_str)
        )
        if row is None:
            return None
        age = (datetime.utcnow() - row.created_at_utc).total_seconds()
        if age > row.ttl_seconds:
            return None
        return json.loads(row.response_json)
    finally:
        db.close()


def _save_cache(cache_key_str: str, response: dict, prompt_tokens: int, output_tokens: int) -> None:
    """写入 LLM 响应缓存。"""
    from zplan_shared.models import LlmResponseCache, SessionLocal

    db: Session = SessionLocal()
    try:
        existing = db.scalar(
            select(LlmResponseCache).where(LlmResponseCache.cache_key == cache_key_str)
        )
        if existing:
            existing.response_json = json.dumps(response, ensure_ascii=False)
            existing.prompt_tokens = prompt_tokens
            existing.output_tokens = output_tokens
            existing.created_at_utc = datetime.utcnow()
        else:
            db.add(
                LlmResponseCache(
                    cache_key=cache_key_str,
                    response_json=json.dumps(response, ensure_ascii=False),
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    ttl_seconds=LLM_CACHE_TTL_SECONDS,
                )
            )
        db.commit()
    finally:
        db.close()


# ── 流式 LLM 调用 ──


async def stream_llm_response(
    user_text: str,
    system_prompt: str = "",
    model: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """
    流式调用 DeepSeek chat/completions，逐 token yield。

    Yields:
        {"type": "token", "text": "..."}
        {"type": "done", "intent": "...", "full_text": "...", "usage": {...}}
    """
    model = model or DEEPSEEK_MODEL
    ck = _cache_key(system_prompt, user_text, model)

    # 检查缓存
    cached = _cached_response(ck)
    if cached:
        logger.info("LLM 缓存命中 %s", ck[:16])
        full_text = cached.get("text", "")
        # 模拟流式输出（逐个字符，给前端渲染时间）
        for char in full_text:
            yield {"type": "token", "text": char}
        yield {"type": "done", "intent": cached.get("intent", ""), "full_text": full_text, "cached": True}
        return

    # 构建消息——包含历史对话
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    # 多轮对话历史
    if history:
        for h in history[-20:]:  # 最近 20 条
            if h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})

    client = AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_API_BASE_URL,
        timeout=DEEPSEEK_TIMEOUT_SECONDS,
    )

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=DEEPSEEK_MAX_OUTPUT_TOKENS,
            stream=True,
            temperature=0.3,
            stream_options={"include_usage": True},
        )

        full_text = ""
        usage = {}
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                token = chunk.choices[0].delta.content
                full_text += token
                yield {"type": "token", "text": token}
            if hasattr(chunk, "usage") and chunk.usage:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                }

        # 保存缓存
        intent = _classify_intent(full_text)
        cache_data = {"text": full_text, "intent": intent}
        pt = usage.get("prompt_tokens", 0)
        ot = usage.get("completion_tokens", 0)
        if pt and ot:
            _save_cache(ck, cache_data, pt, ot)

        yield {"type": "done", "intent": intent, "full_text": full_text, "usage": usage}

    except Exception as exc:
        logger.exception("LLM 流式调用失败")
        yield {"type": "error", "message": str(exc)}


# ── 意图路由（复用 wechat_interact，但非流式时用） ──


def _classify_intent(text: str) -> str:
    """根据文本内容简单分类意图。"""
    import re

    stock_code_re = re.compile(r"^[0368]\d{5}$|^\d{6}$")
    first_line = text.strip().split("\n")[0][:100]

    if any(kw in first_line for kw in ["评分", "选股", "推荐", "打分", "技术面", "综合评分"]):
        return "pick"
    if stock_code_re.match(first_line.strip()) or "分析" in first_line:
        return "pick"
    if any(kw in first_line for kw in ["新闻", "资讯", "快讯", "最新"]):
        return "info_query"
    if any(kw in first_line for kw in ["筛选", "题材", "概念", "板块"]):
        return "screen"
    if any(kw in first_line for kw in ["帮助", "help"]):
        return "help"
    return "chat"


def handle_chat_text(user_text: str) -> dict:
    """
    非流式处理：复用 wechat_interact.handle_inbound_text()。
    返回完整的 reply dict。
    """
    from wechat_interact import handle_inbound_text

    t0 = time.monotonic()
    payload = handle_inbound_text(
        user_text,
        user_id="web_user",
        channel="web",
        chat_id="web",
        mentioned=False,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    payload["elapsed_ms"] = elapsed_ms
    return payload
