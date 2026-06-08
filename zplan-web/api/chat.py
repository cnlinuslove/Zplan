"""/api/v1/chat — SSE 流式对话 + 会话管理。"""

from __future__ import annotations

import json
import logging
import re
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

    # 加载历史消息作为上下文（最近 20 轮）
    history_messages = []
    last_stock_code = None
    last_stock_name = None
    db = SessionLocal()
    try:
        history_rows = db.scalars(
            select(WebChatMessage)
            .where(WebChatMessage.session_id == db_session_id)
            .order_by(WebChatMessage.created_at_utc)
            .limit(40)
        ).all()
        for msg in history_rows:
            # 从历史消息中提取股票上下文
            m = re.search(r'([0368]\d{5})', msg.content)
            if m:
                last_stock_code = m.group(1)
        # 构建 LLM 历史
        history_messages = [
            {"role": m.role, "content": m.content}
            for m in history_rows[:-1]  # 排除当前消息（刚存的）
        ]
    finally:
        db.close()

    if not req.stream:
        # 非流式：复用 wechat_interact
        payload = handle_chat_text(user_text)
        reply = payload.get("reply_markdown") or payload.get("reply_text") or ""
        _save_message(db_session_id, "assistant", reply, intent=payload.get("intent"))
        return {"ok": True, "session_id": db_session_id, **payload}

    # 流式 — 解析当前消息中的股票
    enriched_context = ""
    stock_code_match = re.search(r'([0368]\d{5})', user_text)
    resolved_code = stock_code_match.group(1) if stock_code_match else None

    # 如果没有代码，尝试从文本中提取股票名称并查库
    if not resolved_code:
        name_patterns = [
            r'(?:分析|查看|看看|研究|查)\s*[：:\s]*([一-鿿\w]{2,8})',
            r'([一-鿿\w]{2,8})\s*(?:分析|怎么样|如何|走势|行情)',
        ]
        for pat in name_patterns:
            m = re.search(pat, user_text)
            if m:
                name = m.group(1).strip()
                if name and not name.isdigit() and len(name) >= 2:
                    try:
                        from zplan_shared.models import SessionLocal as _Db, StockList as _SL
                        from sqlalchemy import select as _sel
                        _db = _Db()
                        try:
                            stock = _db.scalar(
                                _sel(_SL).where(_SL.name == name, _SL.market == "a")
                            )
                            if stock:
                                resolved_code = stock.ts_code
                        finally:
                            _db.close()
                    except Exception:
                        pass
                break

    # 如果当前消息无股票代码，继承历史上下文
    if not resolved_code and last_stock_code:
        resolved_code = last_stock_code
        # 查名字
        try:
            from zplan_shared.models import SessionLocal as _Db, StockList as _SL
            from sqlalchemy import select as _sel
            _db = _Db()
            try:
                s = _db.scalar(_sel(_SL).where(_SL.ts_code == resolved_code, _SL.market == "a"))
                if s:
                    last_stock_name = s.name
            finally:
                _db.close()
        except Exception:
            pass

    if resolved_code:
        code = resolved_code
        try:
            from zplan_shared.models import SessionLocal as _Db, DailyPrice as _DP, StockList as _SL
            from sqlalchemy import desc as _desc, select as _sel
            _db = _Db()
            try:
                _stock = _db.scalar(_sel(_SL).where(_SL.ts_code == code, _SL.market == "a"))
                if _stock:
                    _bars = _db.scalars(
                        _sel(_DP).where(_DP.ts_code == code, _DP.market == "a")
                        .order_by(_desc(_DP.trade_date)).limit(60)
                    ).all()
                    if _bars:
                        closes = [b.close for b in reversed(_bars) if b.close]
                        dates = [str(b.trade_date) for b in reversed(_bars)]
                        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
                        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
                        latest = _bars[0]
                        enriched_context = (
                            f"【实时行情】{_stock.name}({code}) 行业{_stock.industry or '-'}\n"
                            f"最新收盘: {latest.close} (日期{latest.trade_date})\n"
                            f"涨跌幅: {latest.pct_chg:.2f}% 换手率: {latest.turnover_rate:.2f}%\n"
                            f"成交量: {latest.volume:.0f} 成交额: {latest.amount:.0f}\n"
                            f"MA5: {ma5:.2f} MA20: {ma20:.2f}\n"
                            f"近5日收盘: {[f'{c:.2f}' for c in closes[-5:]]}\n"
                        )
            finally:
                _db.close()
        except Exception:
            pass

    # 多轮对话上下文提示
    stock_hint = ""
    if resolved_code and last_stock_name:
        stock_hint = f"当前讨论的股票: {last_stock_name}({resolved_code})。"
    elif resolved_code:
        stock_hint = f"当前讨论的股票代码: {resolved_code}。"

    system_prompt = (
        "你是 A 股量化投资分析助手，具备专业技术分析能力。支持多轮对话，记住上下文。\n\n"
        f"{stock_hint}\n"
        "## 回答要求\n"
        "当用户询问股票时，请按以下结构给出完整分析：\n"
        "1. **趋势分析**：结合均线、涨跌幅判断短期/中期趋势，说明支撑位和阻力位\n"
        "2. **技术面**：分析量价关系、换手率、KDJ/MACD 等指标\n"
        "3. **操作建议**：给出具体的买入价位、目标价位、止损价位\n"
        "4. **风险提示**：列出主要风险因素\n\n"
        "## 多轮对话\n"
        "如果用户追问同一只股票（如买入时机、目标价、止损调整等），请基于之前的分析继续深入回答，"
        "不要重复完整分析框架，而是聚焦用户的具体问题。\n\n"
        "## 风格\n"
        "专业但易懂，用具体数据说话。"
        f"{enriched_context}"
    )

    async def _event_stream():
        full_text = ""
        usage = {}
        try:
            async for event in stream_llm_response(user_text, system_prompt, history=history_messages):
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
                    # 如果有关联股票代码，附上图表链接
                    chart_info = None
                    if resolved_code:
                        chart_info = {
                            "ts_code": resolved_code,
                            "chart_url": f"/api/v1/market/stocks/{resolved_code}/chart",
                            "detail_url": f"/market/{resolved_code}",
                        }
                    yield f"data: {json.dumps({'type': 'done', 'session_id': db_session_id, 'intent': intent, 'cached': event.get('cached', False), 'chart': chart_info}, ensure_ascii=False)}\n\n"
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
