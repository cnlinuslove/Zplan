"""Gemini 结构化 JSON 生成（选股 / 资讯共用底层）。"""
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
    GEMINI_API_BASE_URL,
    GEMINI_API_KEY,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MIN_SECONDS_BETWEEN_CALLS,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_SECONDS,
)
from zplan_shared.http_client import resolve_proxy_url

logger = logging.getLogger(__name__)

_gemini_last_call_mono = 0.0
_gemini_pace_lock = threading.Lock()

_SAFETY = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
]


class GeminiError(RuntimeError):
    pass


def gemini_available() -> bool:
    return bool(GEMINI_API_KEY.strip())


def _pace() -> None:
    gap = GEMINI_MIN_SECONDS_BETWEEN_CALLS
    if gap <= 0:
        return
    global _gemini_last_call_mono
    with _gemini_pace_lock:
        now = time_module.monotonic()
        wait_s = _gemini_last_call_mono + gap - now
        if wait_s > 0:
            time_module.sleep(wait_s)
        _gemini_last_call_mono = time_module.monotonic()


def _session() -> requests.Session:
    s = requests.Session()
    proxy_url, _ = resolve_proxy_url()
    if proxy_url:
        s.proxies.update({"http": proxy_url, "https": proxy_url})
    return s


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
        raise GeminiError(f"响应无 JSON: {text[:200]!r}")
    return json.loads(m.group(0))


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, GeminiError):
        m = re.search(r"http (\d{3})", str(exc))
        if m:
            code = int(m.group(1))
            return code == 429 or code >= 500
        return True
    return isinstance(exc, json.JSONDecodeError)


@retry(
    wait=wait_exponential(multiplier=2, min=3, max=60),
    stop=stop_after_attempt(4),
    retry=retry_if_exception(_should_retry),
    reraise=True,
)
def generate_json_with_gemini(
    *,
    prompt: str,
    response_schema: dict[str, Any] | None = None,
    temperature: float = 0.3,
    max_output_tokens: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if not gemini_available():
        raise GeminiError("未配置 GEMINI_API_KEY（请在 zplan-资讯/.env 设置）")

    use_model = model or GEMINI_MODEL
    url = f"{GEMINI_API_BASE_URL.rstrip('/')}/models/{use_model}:generateContent"
    gen_cfg: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens or max(2048, GEMINI_MAX_OUTPUT_TOKENS),
        "responseMimeType": "application/json",
    }
    if response_schema:
        gen_cfg["responseSchema"] = response_schema

    _pace()
    resp = _session().post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "safetySettings": _SAFETY,
            "generationConfig": gen_cfg,
        },
        headers={"x-goog-api-key": GEMINI_API_KEY},
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise GeminiError(f"Gemini HTTP {resp.status_code}: {(resp.text or '')[:400]}")

    body = resp.json()
    cands = body.get("candidates") or []
    if not cands:
        raise GeminiError("Gemini 无 candidates")

    parts = (cands[0].get("content") or {}).get("parts") or []
    text_out = "".join(str(p.get("text", "")) for p in parts).strip()
    if not text_out:
        raise GeminiError(f"Gemini 空响应 finishReason={cands[0].get('finishReason')!r}")

    try:
        parsed = _parse_json(text_out)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"JSON 解析失败: {exc}; head={text_out[:300]!r}") from exc

    usage_meta = body.get("usageMetadata") or {}
    if usage_meta:
        parsed["__usage__"] = {
            "prompt_tokens": usage_meta.get("promptTokenCount"),
            "output_tokens": usage_meta.get("candidatesTokenCount"),
            "total_tokens": usage_meta.get("totalTokenCount"),
            "model": use_model,
        }
    return parsed


def pop_usage(payload: dict[str, Any]) -> dict[str, int | str | None] | None:
    """取出并移除 ``generate_json_with_gemini`` 附带的 token 用量。"""
    return payload.pop("__usage__", None)
