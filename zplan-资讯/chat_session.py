"""
会话窗口：用户 @Zplan 一次后，N 分钟内同群消息无需再次 @。
同时存储最近 N 轮对话历史，供 Brain 多轮推理。

设计要点：
- 纯内存，重启丢失（可接受 — 用户重新 @ 一次即可）
- 按 chat_id 追踪，支持群聊和单聊
- 每次 @ 消息刷新 TTL；非 @ 消息在窗口内放行
- 每个会话保留最近 10 轮对话（user/assistant 配对）
"""
from __future__ import annotations

import threading
import time
from typing import Optional

# 默认 2 小时
DEFAULT_TTL_SECONDS = 2 * 60 * 60
MAX_HISTORY_ROUNDS = 10  # 每个会话最多保留轮数


class ChatSessionStore:
    """线程安全的内存会话窗口 + 对话历史。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._sessions: dict[str, float] = {}  # chat_id → expire_at
        self._history: dict[str, list[dict[str, str]]] = {}  # chat_id → messages
        self._current_stock: dict[str, dict[str, str]] = {}  # chat_id → {ts_code, name}
        self._last_intent: dict[str, str] = {}  # chat_id → intent
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def touch(self, chat_id: str) -> bool:
        """记录一次显式 @ 交互，刷新窗口。返回 True 表示新建会话。"""
        with self._lock:
            expire_at = self._sessions.get(chat_id)
            was_active = expire_at is not None and time.time() <= expire_at
            self._sessions[chat_id] = time.time() + self._ttl
            if not was_active:
                self._history.pop(chat_id, None)  # 新会话清空旧历史
                self._current_stock.pop(chat_id, None)  # 清空旧股票上下文
                self._last_intent.pop(chat_id, None)
            return not was_active

    def is_active(self, chat_id: str) -> bool:
        """该 chat 当前是否在会话窗口内。"""
        with self._lock:
            expire_at = self._sessions.get(chat_id)
            if expire_at is None:
                return False
            if time.time() > expire_at:
                del self._sessions[chat_id]
                self._history.pop(chat_id, None)
                self._current_stock.pop(chat_id, None)
                self._last_intent.pop(chat_id, None)
                return False
            return True

    def remaining_seconds(self, chat_id: str) -> int:
        """窗口剩余秒数（0 表示已过期或未激活）。"""
        with self._lock:
            expire_at = self._sessions.get(chat_id)
            if expire_at is None:
                return 0
            remaining = int(expire_at - time.time())
            if remaining <= 0:
                del self._sessions[chat_id]
                self._history.pop(chat_id, None)
                return 0
            return remaining

    def expire(self, chat_id: str) -> None:
        """手动终止会话窗口。"""
        with self._lock:
            self._sessions.pop(chat_id, None)
            self._history.pop(chat_id, None)
            self._current_stock.pop(chat_id, None)
            self._last_intent.pop(chat_id, None)

    def add_message(self, chat_id: str, role: str, content: str) -> None:
        """追加一条消息到对话历史。role: 'user' | 'assistant'。"""
        if not content:
            return
        with self._lock:
            if chat_id not in self._history:
                self._history[chat_id] = []
            hist = self._history[chat_id]
            hist.append({"role": role, "content": content[:2000]})
            # 保留最近 N 轮（每轮 user+assistant = 2 条）
            max_msgs = MAX_HISTORY_ROUNDS * 2
            if len(hist) > max_msgs:
                self._history[chat_id] = hist[-max_msgs:]

    def get_history(self, chat_id: str) -> list[dict[str, str]]:
        """获取该会话的对话历史（最近 N 轮），返回 messages 列表。"""
        with self._lock:
            hist = self._history.get(chat_id, [])
            return list(hist)  # 返回副本

    def set_current_stock(self, chat_id: str, ts_code: str, name: str) -> None:
        """记录当前会话正在讨论的股票，供 Brain 多轮推理使用。"""
        with self._lock:
            self._current_stock[chat_id] = {"ts_code": ts_code, "name": name}

    def get_current_stock(self, chat_id: str) -> dict[str, str] | None:
        """获取当前讨论股票，None 表示无上下文。"""
        with self._lock:
            return self._current_stock.get(chat_id)

    def set_last_intent(self, chat_id: str, intent: str) -> None:
        """记录上一次对话意图，辅助追问题路由。"""
        with self._lock:
            self._last_intent[chat_id] = intent

    def get_last_intent(self, chat_id: str) -> str | None:
        """获取上一次对话意图。"""
        with self._lock:
            return self._last_intent.get(chat_id)

    def active_count(self) -> int:
        """当前活跃会话数（用于监控）。"""
        now = time.time()
        with self._lock:
            stale = [cid for cid, exp in self._sessions.items() if now > exp]
            for cid in stale:
                del self._sessions[cid]
                self._history.pop(cid, None)
                self._current_stock.pop(cid, None)
                self._last_intent.pop(cid, None)
            return len(self._sessions)


# 全局单例
_store: Optional[ChatSessionStore] = None
_lock = threading.Lock()


def get_session_store() -> ChatSessionStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                from zplan_shared.config import CHAT_SESSION_TTL_MINUTES

                ttl = max(1, int(CHAT_SESSION_TTL_MINUTES)) * 60
                _store = ChatSessionStore(ttl_seconds=ttl)
    return _store


# ── 便捷函数 ──


def session_active(chat_id: str | None) -> bool:
    """该 chat 当前是否在会话窗口内。chat_id 为 None 时返回 False。"""
    if not chat_id:
        return False
    return get_session_store().is_active(chat_id)


def touch_session(chat_id: str | None) -> bool:
    """记录一次显式交互（@ 消息），刷新窗口。返回 True 表示新建会话。"""
    if chat_id:
        return get_session_store().touch(chat_id)
    return False


def expire_session(chat_id: str | None) -> None:
    """手动终止会话窗口（可用于「退出」指令）。"""
    if chat_id:
        get_session_store().expire(chat_id)


def add_message(chat_id: str | None, role: str, content: str) -> None:
    """追加一条消息到会话历史。"""
    if chat_id:
        get_session_store().add_message(chat_id, role, content)


def get_history(chat_id: str | None) -> list[dict[str, str]]:
    """获取会话对话历史。"""
    if not chat_id:
        return []
    return get_session_store().get_history(chat_id)


def set_current_stock(chat_id: str | None, ts_code: str, name: str) -> None:
    """记录当前会话正在讨论的股票。"""
    if chat_id:
        get_session_store().set_current_stock(chat_id, ts_code, name)


def get_current_stock(chat_id: str | None) -> dict[str, str] | None:
    """获取当前讨论股票，None 表示无上下文。"""
    if not chat_id:
        return None
    return get_session_store().get_current_stock(chat_id)


def set_last_intent(chat_id: str | None, intent: str) -> None:
    """记录上一次对话意图。"""
    if chat_id:
        get_session_store().set_last_intent(chat_id, intent)


def get_last_intent(chat_id: str | None) -> str | None:
    """获取上一次对话意图。"""
    if not chat_id:
        return None
    return get_session_store().get_last_intent(chat_id)
