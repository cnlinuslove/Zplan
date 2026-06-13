"""
会话窗口：用户 @Zplan 一次后，N 分钟内同群消息无需再次 @。
同时存储最近 N 轮对话历史，供 Brain 多轮推理。

设计要点：
- 纯内存 + SQLite 持久化：重启后从 DB 恢复活跃会话
- 按 chat_id 追踪，支持群聊和单聊
- 每次 @ 消息刷新 TTL；非 @ 消息在窗口内放行
- 每个会话保留最近 10 轮对话（user/assistant 配对）
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 默认 2 小时
DEFAULT_TTL_SECONDS = 2 * 60 * 60
MAX_HISTORY_ROUNDS = 10  # 每个会话最多保留轮数


def _resolve_db_path() -> Path:
    """解析 SQLite 数据库路径（与 zplan_shared.config 一致）。"""
    import os
    from pathlib import Path

    explicit = os.getenv("ZPLAN_ROOT", "").strip()
    if explicit:
        root = Path(explicit).expanduser().resolve()
    else:
        mono_root = Path(__file__).resolve().parents[2]  # my_stock_ai/
        for name in ("zplan-资讯", "zplan"):
            candidate = mono_root / name
            if candidate.is_dir():
                root = candidate.resolve()
                break
        else:
            root = Path.cwd().resolve()
    return root / "zplan.db"


class ChatSessionStore:
    """线程安全的内存会话窗口 + 对话历史（SQLite 持久化）。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._sessions: dict[str, float] = {}  # chat_id → expire_at
        self._history: dict[str, list[dict[str, str]]] = {}  # chat_id → messages
        self._current_stock: dict[str, dict[str, str]] = {}  # chat_id → {ts_code, name}
        self._last_intent: dict[str, str] = {}  # chat_id → intent
        self._lock = threading.Lock()
        self._db_path = _resolve_db_path()

        # 启动时从 DB 恢复未过期会话
        self._load_all()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    # ── SQLite 持久化 ─────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """获取独立 sqlite3 连接（避免与 SQLAlchemy 竞争）。"""
        conn = sqlite3.connect(str(self._db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _save(self, chat_id: str) -> None:
        """将单个 chat_id 的完整状态 upsert 到 chat_session_state 表。"""
        try:
            state = {
                "history": self._history.get(chat_id, []),
                "current_stock": self._current_stock.get(chat_id),
                "last_intent": self._last_intent.get(chat_id),
            }
            expires_at = self._sessions.get(chat_id, time.time() + self._ttl)
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO chat_session_state (chat_id, state_json, expires_at, updated_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (chat_id, json.dumps(state, ensure_ascii=False), expires_at),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("chat_session 持久化失败 (chat_id=%s)", chat_id, exc_info=True)

    def _delete(self, chat_id: str) -> None:
        """从 DB 中删除指定会话。"""
        try:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM chat_session_state WHERE chat_id=?", (chat_id,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("chat_session DB 删除失败 (chat_id=%s)", chat_id, exc_info=True)

    def _load_all(self) -> None:
        """从 SQLite 加载所有未过期会话（启动时调用）。"""
        now = time.time()
        try:
            conn = self._get_conn()
            try:
                # 检查表是否存在
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_session_state'"
                )
                if not cursor.fetchone():
                    return  # 表尚未创建（首次运行）

                rows = conn.execute(
                    "SELECT chat_id, state_json, expires_at FROM chat_session_state WHERE expires_at > ?",
                    (now,),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            logger.warning("chat_session 从 DB 加载失败，使用空状态", exc_info=True)
            return

        loaded = 0
        with self._lock:
            for chat_id, state_json, expires_at in rows:
                try:
                    state = json.loads(state_json)
                    self._sessions[chat_id] = expires_at
                    history = state.get("history", [])
                    if history:
                        self._history[chat_id] = history[-MAX_HISTORY_ROUNDS * 2:]
                    cs = state.get("current_stock")
                    if cs:
                        self._current_stock[chat_id] = cs
                    li = state.get("last_intent")
                    if li:
                        self._last_intent[chat_id] = li
                    loaded += 1
                except (json.JSONDecodeError, TypeError):
                    logger.warning("chat_session 解析失败 (chat_id=%s)", chat_id)
        if loaded:
            logger.info("chat_session 从 DB 恢复 %d 个活跃会话", loaded)

    # ── 会话窗口操作 ──────────────────────────────────────────────

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
        self._save(chat_id)
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
                # 异步清理 DB（不阻塞）
                t = threading.Thread(target=self._delete, args=(chat_id,), daemon=True)
                t.start()
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
        self._delete(chat_id)

    # ── 对话历史 ──────────────────────────────────────────────────

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
        self._save(chat_id)

    def get_history(self, chat_id: str) -> list[dict[str, str]]:
        """获取该会话的对话历史（最近 N 轮），返回 messages 列表。"""
        with self._lock:
            hist = self._history.get(chat_id, [])
            return list(hist)  # 返回副本

    # ── 股票上下文 ────────────────────────────────────────────────

    def set_current_stock(self, chat_id: str, ts_code: str, name: str) -> None:
        """记录当前会话正在讨论的股票，供 Brain 多轮推理使用。"""
        with self._lock:
            self._current_stock[chat_id] = {"ts_code": ts_code, "name": name}
        self._save(chat_id)

    def get_current_stock(self, chat_id: str) -> dict[str, str] | None:
        """获取当前讨论股票，None 表示无上下文。"""
        with self._lock:
            return self._current_stock.get(chat_id)

    def set_last_intent(self, chat_id: str, intent: str) -> None:
        """记录上一次对话意图，辅助追问题路由。"""
        with self._lock:
            self._last_intent[chat_id] = intent
        self._save(chat_id)

    def get_last_intent(self, chat_id: str) -> str | None:
        """获取上一次对话意图。"""
        with self._lock:
            return self._last_intent.get(chat_id)

    # ── 监控 ──────────────────────────────────────────────────────

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
