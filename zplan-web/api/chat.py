"""/api/v1/chat — SSE 流式对话 + 会话管理。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select

from services.chat_service import handle_chat_text, stream_llm_response
from zplan_shared.models import SessionLocal, WebChatMessage, WebChatSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


class ChatSendRequest(BaseModel):
    text: str
    session_id: str | None = None  # 可选：续接已有会话
    stream: bool = True  # false=非流式完整返回


# ── 会话管理 ──


def _get_or_create_session(session_id: str | None) -> int:
    """从 session_id（UUID 字符串）找到或创建 DB session，返回 DB id。"""
    db = SessionLocal()
    try:
        if session_id:
            try:
                sid = int(session_id)
                row = db.scalar(select(WebChatSession).where(WebChatSession.id == sid))
                if row:
                    row.updated_at_utc = datetime.utcnow()
                    db.commit()
                    return row.id
            except (ValueError, TypeError):
                pass
        # 新建会话
        title = "新对话"
        sess = WebChatSession(title=title)
        db.add(sess)
        db.commit()
        db.refresh(sess)
        return sess.id
    finally:
        db.close()


def _save_message(
    db_session_id: int,
    role: str,
    content: str,
    intent: str | None = None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    elapsed_ms: int | None = None,
) -> None:
    """持久化一条聊天消息。"""
    db = SessionLocal()
    try:
        db.add(
            WebChatMessage(
                session_id=db_session_id,
                role=role,
                content=content,
                intent=intent,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                cost_usd=_estimate_cost(prompt_tokens or 0, output_tokens or 0),
                elapsed_ms=elapsed_ms,
            )
        )
        db.commit()
    finally:
        db.close()


def _estimate_cost(prompt_tokens: int, output_tokens: int) -> float:
    """DeepSeek V3 定价：$0.27/M input, $1.10/M output."""
    return (prompt_tokens / 1_000_000) * 0.27 + (output_tokens / 1_000_000) * 1.10


# ── SSE 流式 ──


@router.post("/chat/send")
async def chat_send(req: ChatSendRequest):
    """发送消息，SSE 流式返回 LLM token。"""
    user_text = req.text.strip()
    if not user_text:
        return {"ok": False, "error": "empty text"}

    db_session_id = _get_or_create_session(req.session_id)
    # 保存用户消息
    _save_message(db_session_id, "user", user_text)

    if not req.stream:
        # 非流式：复用 wechat_interact
        payload = handle_chat_text(user_text)
        reply = payload.get("reply_markdown") or payload.get("reply_text") or ""
        _save_message(db_session_id, "assistant", reply, intent=payload.get("intent"))
        return {"ok": True, "session_id": db_session_id, **payload}

    # 流式
    system_prompt = (
        "你是 A 股量化分析助手。用户可能询问股票分析、选股推荐、行情数据、新闻资讯等问题。"
        "回答用中文，简洁专业。如果用户发的是股票代码或名称，请给出技术面、财务面、资讯面的综合分析。"
        "如果用户要求选股或推荐，请列出具体的股票代码和简要理由。"
    )

    async def _event_stream():
        full_text = ""
        usage = {}
        try:
            async for event in stream_llm_response(user_text, system_prompt):
                if event["type"] == "token":
                    full_text += event["text"]
                    yield f"data: {json.dumps({'type': 'token', 'text': event['text']}, ensure_ascii=False)}\n\n"
                elif event["type"] == "done":
                    usage = event.get("usage", {})
                    intent = event.get("intent", "")
                    _save_message(
                        db_session_id,
                        "assistant",
                        full_text,
                        intent=intent,
                        prompt_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                    )
                    yield f"data: {json.dumps({'type': 'done', 'session_id': db_session_id, 'intent': intent, 'cached': event.get('cached', False)}, ensure_ascii=False)}\n\n"
                elif event["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': event['message']}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("SSE stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": str(db_session_id),
        },
    )


# ── 会话历史 ──


@router.get("/chat/sessions")
async def list_sessions(limit: int = 20, offset: int = 0):
    """列出所有聊天会话。"""
    db = SessionLocal()
    try:
        stmt = (
            select(WebChatSession)
            .order_by(desc(WebChatSession.updated_at_utc))
            .offset(offset)
            .limit(limit)
        )
        rows = db.scalars(stmt).all()
        return {
            "ok": True,
            "sessions": [
                {
                    "id": r.id,
                    "title": r.title or f"会话 {r.id}",
                    "is_active": r.is_active,
                    "created_at": r.created_at_utc.isoformat() if r.created_at_utc else None,
                    "updated_at": r.updated_at_utc.isoformat() if r.updated_at_utc else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.get("/chat/sessions/{session_id}/messages")
async def get_messages(session_id: int, limit: int = 50, offset: int = 0):
    """获取指定会话的历史消息。"""
    db = SessionLocal()
    try:
        stmt = (
            select(WebChatMessage)
            .where(WebChatMessage.session_id == session_id)
            .order_by(WebChatMessage.created_at_utc)
            .offset(offset)
            .limit(limit)
        )
        rows = db.scalars(stmt).all()
        return {
            "ok": True,
            "session_id": session_id,
            "messages": [
                {
                    "id": r.id,
                    "role": r.role,
                    "content": r.content,
                    "intent": r.intent,
                    "cost_usd": r.cost_usd,
                    "elapsed_ms": r.elapsed_ms,
                    "created_at": r.created_at_utc.isoformat() if r.created_at_utc else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.delete("/chat/sessions/{session_id}")
async def delete_session(session_id: int):
    """删除会话及其所有消息。"""
    db = SessionLocal()
    try:
        sess = db.scalar(select(WebChatSession).where(WebChatSession.id == session_id))
        if sess:
            db.delete(sess)
            db.commit()
            return {"ok": True, "deleted": True}
        return {"ok": False, "deleted": False, "error": "not found"}
    finally:
        db.close()
