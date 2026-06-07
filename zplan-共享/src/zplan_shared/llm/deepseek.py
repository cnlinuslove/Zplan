"""DeepSeek 统一 LLM 客户端（OpenAI 兼容 API）。

替代原 Gemini 客户端，提供：
- ``generate_json_with_deepseek()`` — 结构化 JSON 生成（选股深度研报/简评）
- ``generate_text_with_deepseek()`` — 纯文本生成（资讯问答）
- ``chat_with_deepseek()`` — 多轮对话（新闻摘要/快讯简报）

所有调用共享限速、重试与代理策略。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as time_module
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from zplan_shared.config import (
    DEEPSEEK_API_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
)
from zplan_shared.http_client import resolve_proxy_url

logger = logging.getLogger(__name__)

_last_call_mono = 0.0
_pace_lock = threading.Lock()

# ── errors ──────────────────────────────────────────────────────────


class DeepSeekError(RuntimeError):
    """LLM API 调用失败。"""


# ── 模型无关的通用别名（新代码请用这些）─────────────────────────
LLMError = DeepSeekError

# ── 向后兼容别名 ─────────────────────────────────────────────────
GeminiError = DeepSeekError


# ── helpers ─────────────────────────────────────────────────────────


def deepseek_available() -> bool:
    return bool(DEEPSEEK_API_KEY.strip())


# 向后兼容别名
gemini_available = deepseek_available


def _pace() -> None:
    gap = DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS
    if gap <= 0:
        return
    global _last_call_mono
    with _pace_lock:
        now = time_module.monotonic()
        wait_s = _last_call_mono + gap - now
        if wait_s > 0:
            time_module.sleep(wait_s)
        _last_call_mono = time_module.monotonic()


def _session() -> requests.Session:
    s = requests.Session()
    proxy_url, _ = resolve_proxy_url()
    if proxy_url:
        s.proxies.update({"http": proxy_url, "https": proxy_url})
    return s


def _build_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


def _completion_url() -> str:
    return f"{DEEPSEEK_API_BASE_URL.rstrip('/')}/chat/completions"


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _parse_json(text: str) -> dict[str, Any]:
    text = _strip_fence(text)
    if text.startswith("{"):
        return json.loads(text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise DeepSeekError(f"响应无 JSON: {text[:200]!r}")
    return json.loads(m.group(0))


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, DeepSeekError):
        s = str(exc)
        m = re.search(r"http (\d{3})", s)
        if m:
            code = int(m.group(1))
            return code == 429 or code >= 500
        return "rate_limit" in s.lower() or "server error" in s.lower()
    if isinstance(exc, json.JSONDecodeError):
        return True
    return False


def _parse_retry_seconds_from_text(text: str) -> float | None:
    """从 DeepSeek 429 响应的 retry-after 或错误消息中提取等待秒数。"""
    # 先尝试 retry-after header（由调用方传入）
    m = re.search(r"retry[_-]?after[=:]\s*(\d+)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*秒后", text)
    if m:
        return float(m.group(1))
    return None


def _deepseek_retry_wait(retry_state: Any) -> float:
    """429 时优先采用 API 返回的等待秒数。"""
    if retry_state.outcome is not None:
        exc = retry_state.outcome.exception()
        if exc is not None:
            secs = _parse_retry_seconds_from_text(str(exc))
            if secs is not None:
                return min(secs + 1.0, 90.0)
    return min(2 ** retry_state.attempt_number * 2, 60)


# ── low-level HTTP ──────────────────────────────────────────────────


def _post_chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    model: str | None = None,
    response_format: dict[str, str] | None = None,
    timeout: int | None = None,
) -> requests.Response:
    """发送 chat/completions 请求，返回原始 response 对象。"""
    use_model = model or DEEPSEEK_MODEL
    body: dict[str, Any] = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or max(2048, DEEPSEEK_MAX_OUTPUT_TOKENS),
    }
    if response_format:
        body["response_format"] = response_format

    _pace()
    return _session().post(
        _completion_url(),
        json=body,
        headers=_build_headers(),
        timeout=timeout or DEEPSEEK_TIMEOUT_SECONDS,
    )


def _extract_text_and_usage(resp: requests.Response) -> tuple[str, dict[str, Any]]:
    """从 chat/completions 响应中提取文本与 token 用量。"""
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise DeepSeekError(f"DeepSeek 无 choices: {json.dumps(body, ensure_ascii=False)[:300]}")
    msg = choices[0].get("message") or {}
    content = str(msg.get("content", "")).strip()
    if not content:
        finish = str(choices[0].get("finish_reason", ""))
        if finish and finish != "stop":
            raise DeepSeekError(f"DeepSeek 空响应 finish_reason={finish!r}")
        raise DeepSeekError("DeepSeek 返回空内容")

    usage_meta = body.get("usage") or {}
    usage = {
        "prompt_tokens": usage_meta.get("prompt_tokens"),
        "output_tokens": usage_meta.get("completion_tokens"),
        "total_tokens": usage_meta.get("total_tokens"),
        "model": body.get("model", "unknown"),
    }
    return content, usage


# ── structured JSON generation ──────────────────────────────────────


def _build_json_system_prompt(schema: dict[str, Any] | None) -> str:
    """构建 JSON 模式的 system prompt，将 schema 内嵌到提示中。"""
    base = "你是一个 JSON-only API。始终只输出一个合法的 JSON 对象，不要包含任何 markdown 围栏或额外文本。"
    if schema:
        base += (
            "\n\n输出必须符合以下 JSON Schema：\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
            + "\n\n所有字段名必须与 schema 完全一致，字符串使用中文。"
        )
    return base


@retry(
    wait=wait_exponential(multiplier=2, min=3, max=60),
    stop=stop_after_attempt(4),
    retry=retry_if_exception(_should_retry),
    reraise=True,
)
def generate_json_with_deepseek(
    *,
    prompt: str,
    response_schema: dict[str, Any] | None = None,
    temperature: float = 0.3,
    max_output_tokens: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """结构化 JSON 生成（替代原 Gemini generate_json_with_gemini）。

    使用 DeepSeek ``response_format: {"type": "json_object"}`` 强制 JSON 输出，
    schema 内嵌在 system prompt 中（DeepSeek 不支持 responseSchema）。
    """
    if not deepseek_available():
        raise DeepSeekError(
            "未配置 DEEPSEEK_API_KEY（请在 zplan-资讯/.env 设置 DEEPSEEK_API_KEY）"
        )

    messages = [
        {"role": "system", "content": _build_json_system_prompt(response_schema)},
        {"role": "user", "content": prompt},
    ]

    resp = _post_chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_output_tokens,
        model=model,
        response_format={"type": "json_object"},
    )

    if resp.status_code >= 400:
        raise DeepSeekError(
            f"DeepSeek HTTP {resp.status_code}: {(resp.text or '')[:400]}"
        )

    text_out, usage = _extract_text_and_usage(resp)

    try:
        parsed = _parse_json(text_out)
    except json.JSONDecodeError as exc:
        raise DeepSeekError(f"JSON 解析失败: {exc}; head={text_out[:300]!r}") from exc

    if usage:
        parsed["__usage__"] = usage
    return parsed


def pop_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """取出并移除 ``generate_json_with_deepseek`` 附带的 token 用量。"""
    return payload.pop("__usage__", None)


# ── text generation ─────────────────────────────────────────────────


@retry(
    wait=_deepseek_retry_wait,
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_should_retry),
    reraise=True,
)
def generate_text_with_deepseek(
    *,
    prompt: str,
    system_prompt: str | None = None,
    temperature: float = 0.25,
    max_output_tokens: int | None = None,
    model: str | None = None,
) -> str:
    """纯文本生成（替代原 Gemini answer_info_question 的 HTTP 调用）。

    不强制 JSON 输出，适合自由文本问答。
    """
    if not deepseek_available():
        raise DeepSeekError("未配置 DEEPSEEK_API_KEY")

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    resp = _post_chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_output_tokens,
        model=model,
    )

    if resp.status_code >= 400:
        raise DeepSeekError(f"DeepSeek HTTP {resp.status_code}: {(resp.text or '')[:400]}")

    text_out, _usage = _extract_text_and_usage(resp)
    return text_out


# ── chat (multi-message, used by summarise) ──────────────────────────


@retry(
    wait=_deepseek_retry_wait,
    stop=stop_after_attempt(6),
    retry=retry_if_exception(_should_retry),
    reraise=True,
)
def chat_json_with_deepseek(
    *,
    messages: list[dict[str, str]],
    temperature: float = 0.25,
    max_output_tokens: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """多轮对话 + JSON 强制输出（替代原 Gemini 摘要/简报调用）。

    用于新闻摘要、快讯简报等需要 JSON 结构但 prompt 已内嵌 schema 的场景。
    """
    if not deepseek_available():
        raise DeepSeekError("未配置 DEEPSEEK_API_KEY")

    resp = _post_chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_output_tokens,
        model=model,
        response_format={"type": "json_object"},
    )

    if resp.status_code >= 400:
        raise DeepSeekError(f"DeepSeek HTTP {resp.status_code}: {(resp.text or '')[:400]}")

    text_out, _usage = _extract_text_and_usage(resp)

    try:
        return _parse_json(text_out)
    except json.JSONDecodeError as exc:
        raise DeepSeekError(f"JSON 解析失败: {exc}; head={text_out[:300]!r}") from exc


# ── connectivity check ──────────────────────────────────────────────


def _check_deepseek_connectivity_once() -> dict[str, Any]:
    """最小 chat/completions 探测。"""
    if not deepseek_available():
        return {"ok": False, "error": "DEEPSEEK_API_KEY 未配置或为空"}

    base: dict[str, Any] = {}
    try:
        resp = _post_chat_completion(
            [{"role": "user", "content": '只回复合法 JSON：{"ok":true}'}],
            temperature=0,
            max_tokens=32,
            response_format={"type": "json_object"},
            timeout=min(30, DEEPSEEK_TIMEOUT_SECONDS),
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        return {**base, "ok": False, "error": f"network: {type(exc).__name__}: {exc}"[:300]}
    except requests.RequestException as exc:
        return {**base, "ok": False, "error": f"request: {type(exc).__name__}: {exc}"[:300]}

    if resp.status_code >= 400:
        err_text = (resp.text or "")[:500]
        out: dict[str, Any] = {
            **base,
            "ok": False,
            "http_status": resp.status_code,
            "error": err_text,
            "model": DEEPSEEK_MODEL,
        }
        if resp.status_code == 429:
            out["hint"] = (
                "DeepSeek 429 配额超限。请检查 API 用量：https://platform.deepseek.com/usage"
            )
        return out

    return {**base, "ok": True, "http_status": resp.status_code, "model": DEEPSEEK_MODEL}


def check_deepseek_connectivity() -> dict[str, Any]:
    """探测 DeepSeek API 是否可达。"""
    if not deepseek_available():
        return {"ok": False, "error": "DEEPSEEK_API_KEY 未配置或为空"}
    return _check_deepseek_connectivity_once()


# ── 模型无关的通用别名（新代码推荐使用）─────────────────────────
# 切换模型只需改 .env 中的 DEEPSEEK_API_BASE_URL + DEEPSEEK_MODEL

llm_available = deepseek_available
generate_json = generate_json_with_deepseek
generate_text = generate_text_with_deepseek
chat_json = chat_json_with_deepseek
check_llm_connectivity = check_deepseek_connectivity
