"""桥接 zplan-选股 → 企业微信回复（子进程，避免资讯 venv 缺选股依赖）。"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PICK_ROOT = Path(__file__).resolve().parent.parent / "zplan-选股"
_PICK_PY = _PICK_ROOT / ".venv" / "bin" / "python"
_PICK_SCRIPT = _PICK_ROOT / "scripts" / "wechat_pick_reply.py"


def try_handle_pick(message: str) -> dict[str, Any] | None:
    """
    识别「选股 爱普股份」「爱普股份」「筛选 脑机接口」等。
    返回与 wechat_interact 同结构的 dict；无法识别时返回 None。
    """
    if not message or not message.strip():
        return None
    if not _PICK_PY.is_file() or not _PICK_SCRIPT.is_file():
        logger.warning("选股 Agent 未安装：%s", _PICK_ROOT)
        return {
            "ok": True,
            "intent": "pick_error",
            "reply_text": (
                "选股模块未就绪：请先在 zplan-选股 执行 ./scripts/bootstrap_env.sh"
            ),
        }

    try:
        proc = subprocess.run(
            [str(_PICK_PY), str(_PICK_SCRIPT), message.strip()],
            cwd=str(_PICK_ROOT),
            capture_output=True,
            text=True,
            timeout=int(__import__("os").getenv("PICK_WECHAT_TIMEOUT", "180")),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": True,
            "intent": "pick_timeout",
            "reply_text": "选股分析超时，请稍后重试或设置 PICK_WECHAT_USE_LLM=false 仅规则分。",
        }
    except Exception as exc:
        logger.exception("pick subprocess failed: %s", exc)
        return {
            "ok": True,
            "intent": "pick_error",
            "reply_text": f"选股调用失败：{exc}",
        }

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        logger.error("pick subprocess exit %s: %s", proc.returncode, err)
        return {
            "ok": True,
            "intent": "pick_error",
            "reply_text": f"选股进程异常 (code={proc.returncode})：{err or '见日志'}",
        }

    try:
        raw_stdout = proc.stdout.strip() or "{}"
        data = json.loads(raw_stdout)
    except json.JSONDecodeError:
        logger.error(
            "pick subprocess JSON decode failed, stdout len=%d, head=[%s], tail=[%s]",
            len(proc.stdout or ""),
            (proc.stdout or "")[:200],
            (proc.stdout or "")[-200:],
        )
        return {
            "ok": True,
            "intent": "pick_error",
            "reply_text": "选股返回解析失败",
        }

    if not data.get("ok") or data.get("intent") == "pick_skip":
        return None
    return data
