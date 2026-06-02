"""Gemini REST client for news summarization."""
from __future__ import annotations

import json
import logging
import re
import threading
import time as time_module
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import (
    GEMINI_API_BASE_URL,
    GEMINI_API_KEY,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MIN_SECONDS_BETWEEN_CALLS,
    GEMINI_MODEL,
    GEMINI_SUMMARY_CHARS_PER_ITEM,
    GEMINI_SUMMARY_MAX_ITEMS,
    GEMINI_TIMEOUT_SECONDS,
)
from outbound_http import get_proxied_requests_session, resolve_effective_proxy_url

logger = logging.getLogger(__name__)

_gemini_last_call_mono = 0.0
_gemini_pace_lock = threading.Lock()


def _pace_gemini_http() -> None:
    """任意两次 Gemini HTTP 之间强制间隔，降低 429（含 tenacity 重试）。"""
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


# 财经推文里易误触默认安全阈值，放宽到 BLOCK_ONLY_HIGH（仍拦截明确高危）
_GEMINI_SAFETY_SETTINGS: list[dict[str, str]] = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
]

# 结构化输出：避免模型在 overview/bullets 中写出未转义引号导致 json.loads 失败
_NEWS_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "2-4 句中文综述，禁止整段照抄某条帖子；少用英文半角双引号。",
        },
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 条中文要点，每条一句；少用英文半角双引号。",
        },
        "sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral"],
            "description": "本窗口整体情绪，三选一。",
        },
    },
    "required": ["overview", "bullets", "sentiment"],
}


class GeminiSummaryError(RuntimeError):
    pass


def _gemini_http_post(url: str, **kwargs: Any) -> requests.Response:
    return get_proxied_requests_session().post(url, **kwargs)


def gemini_outbound_proxy_info() -> dict[str, str | None]:
    proxy_url, source = resolve_effective_proxy_url()
    return {"outbound_proxy": proxy_url, "outbound_proxy_source": source}


class GeminiBlockedError(GeminiSummaryError):
    """安全策略拦截等：重试通常无效。"""


def _gemini_should_retry(exc: BaseException) -> bool:
    """429/5xx/网络/JSON 可重试；4xx（除 429）与安全拦截不重试。"""
    if isinstance(exc, GeminiBlockedError):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, GeminiSummaryError):
        s = str(exc)
        if "gemini blocked" in s:
            return False
        m = re.search(r"gemini http (\d{3})", s)
        if m:
            code = int(m.group(1))
            return code == 429 or code >= 500
        return True
    if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
        return True
    return False


def gemini_available() -> bool:
    return bool(GEMINI_API_KEY.strip())


def _strip_md_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json_block(text: str) -> dict[str, Any]:
    text = _strip_md_fence(text)
    if not text:
        raise GeminiSummaryError("gemini empty text response")
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise GeminiSummaryError(f"gemini response missing json object, text_head={text[:120]!r}")
    return json.loads(match.group(0))


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_retry_seconds_from_error(text: str) -> float | None:
    m = re.search(r"retry in ([\d.]+)\s*s", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _parse_gemini_quota_hint(error_text: str) -> str | None:
    """从 429 JSON 解析 quotaId，区分「每日」与「每分钟」限额（免费档常误读）。"""
    try:
        body = json.loads(error_text)
    except json.JSONDecodeError:
        body = None
    violations: list[dict[str, Any]] = []
    if isinstance(body, dict):
        err = body.get("error") or {}
        for detail in err.get("details") or []:
            if isinstance(detail, dict) and "violations" in detail:
                violations.extend(detail.get("violations") or [])
    quota_ids = [str(v.get("quotaId") or "") for v in violations if isinstance(v, dict)]
    if "GenerateRequestsPerDayPerProjectPerModel" in error_text or any(
        "PerDay" in q for q in quota_ids
    ):
        model = GEMINI_MODEL
        for v in violations:
            if isinstance(v, dict) and v.get("quotaDimensions", {}).get("model"):
                model = str(v["quotaDimensions"]["model"])
                break
        return (
            f"Gemini 免费档 **当日请求次数已用尽**（quotaId: 每日限额，"
            f"模型 {model} 约 20 次/天）。等几分钟无法恢复；需等到 **UTC 日切后**、"
            f"换 API Key/项目，或在 Google AI 开通计费。用量: https://ai.dev/rate-limit"
        )
    if any("PerMinute" in q or "PerMinutePerProject" in q for q in quota_ids):
        wait_s = _parse_retry_seconds_from_error(error_text)
        w = f"{wait_s:.0f}s" if wait_s else "约 1 分钟"
        return f"Gemini 每分钟请求次数已满（RPM），请等待 {w} 后重试。"
    if "free_tier" in error_text.lower() and "limit: 20" in error_text.lower():
        return (
            f"Gemini 免费档限额已触顶（{GEMINI_MODEL}，常见为 **20 次/天** 而非/不仅是每分钟）。"
            "请查看 https://ai.dev/rate-limit ；要今天继续测 LLM 需新 Key 或开通计费。"
        )
    return None


def _gemini_retry_wait(retry_state: Any) -> float:
    """429 时优先采用 API 返回的 retry 秒数。"""
    if retry_state.outcome is not None:
        exc = retry_state.outcome.exception()
        if exc is not None:
            secs = _parse_retry_seconds_from_error(str(exc))
            if secs is not None:
                return min(secs + 1.5, 90.0)
    return min(2 ** retry_state.attempt_number * 2, 60)


@retry(
    wait=wait_exponential(multiplier=2, min=3, max=55),
    stop=stop_after_attempt(6),
    retry=retry_if_exception(_gemini_should_retry),
    reraise=True,
)
def summarize_news_with_gemini(
    *,
    topic_display_name: str,
    window_hours: int,
    items: list[dict[str, str | None]],
    max_items: int | None = None,
    max_chars_per_item: int | None = None,
) -> tuple[str, str]:
    if not gemini_available():
        raise GeminiSummaryError("GEMINI_API_KEY not configured")

    cap_n = max_items if max_items is not None else GEMINI_SUMMARY_MAX_ITEMS
    cap_c = max_chars_per_item if max_chars_per_item is not None else GEMINI_SUMMARY_CHARS_PER_ITEM

    lines: list[str] = []
    for idx, item in enumerate(items[:cap_n], start=1):
        text = _clip(item.get("text") or "", cap_c)
        lines.append(f"[{idx}] {text}")

    prompt = f"""你是资深中文财经/宏观资讯编辑。下面编号 [1]、[2]… 是来自 X 的帖子原文片段（可能含噪声）。主题为「{topic_display_name}」，时间窗口约最近 {window_hours} 小时。

你必须输出合法 JSON（不要 markdown 代码围栏），结构固定为：
{{
  "overview": "用 2-4 句中文写一段「综述」，综合多条帖子的共识与分歧，不能整段照抄某一编号原文。",
  "bullets": [
    "用一句完整中文写要点，句末可标注依据如 据[1][3]。每条 40-120 字，共 3-6 条。"
  ],
  "sentiment": "positive 或 negative 或 neutral（三选一，小写）"
}}

硬性要求：
1. 禁止营销话术：Telegram、WhatsApp、免费信号、98% 准确率、Forex 喊单等与主题无关的推广一律不要在综述/要点中出现（可概括为「出现无关推广噪声」一句带过或不提）。
2. bullets 里禁止连续复制原文超过 15 个字；要改写、归纳。
3. 若绝大多数帖子与主题明显无关，overview 中说明「检索结果噪声较大、信息价值有限」，bullets 仍尽量从相对相关的帖中提炼。
4. 只输出 JSON；字符串内不要使用未转义的英文双引号 `"`（如需引号请用中文「」或单引号）。

帖子原文（编号即 [n]）：
{chr(10).join(lines) if lines else "(无帖子)"}"""

    url = f"{GEMINI_API_BASE_URL.rstrip('/')}/models/{GEMINI_MODEL}:generateContent"
    _pace_gemini_http()
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "safetySettings": _GEMINI_SAFETY_SETTINGS,
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": max(512, GEMINI_MAX_OUTPUT_TOKENS),
            "responseMimeType": "application/json",
            "responseSchema": _NEWS_SUMMARY_SCHEMA,
        },
    }
    # 使用 Header 传 Key（与官方文档一致），避免 ?key= 进 URL 被代理/日志干扰
    resp = _gemini_http_post(
        url,
        json=payload,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise GeminiSummaryError(f"gemini http {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise GeminiSummaryError("gemini returned no candidates")

    c0 = candidates[0]
    fr = str(c0.get("finishReason", "") or "")
    parts = (c0.get("content") or {}).get("parts") or []
    text_out = "".join(part.get("text", "") for part in parts).strip()

    if not text_out:
        fb = body.get("promptFeedback") or {}
        br = fb.get("blockReason") if isinstance(fb, dict) else None
        if fr == "SAFETY" or br:
            raise GeminiBlockedError(
                "gemini blocked: finishReason="
                f"{fr!r} blockReason={br!r} promptFeedback="
                f"{json.dumps(fb, ensure_ascii=False)[:400]}"
            )
        raise GeminiSummaryError(f"gemini empty text; finishReason={fr!r}")

    try:
        parsed = _extract_json_block(text_out)
    except json.JSONDecodeError as exc:
        raise GeminiSummaryError(f"gemini json parse: {exc}; head={text_out[:200]!r}") from exc

    overview = str(parsed.get("overview", "")).strip()
    bullets_raw = parsed.get("bullets")
    if isinstance(bullets_raw, str):
        bullets = [bullets_raw.strip()] if bullets_raw.strip() else []
    elif isinstance(bullets_raw, list):
        bullets = [str(b).strip() for b in bullets_raw if str(b).strip()]
    else:
        bullets = []

    sentiment = str(parsed.get("sentiment", "neutral")).strip().lower()
    if sentiment not in {"positive", "negative", "neutral"}:
        sentiment = "neutral"

    if not overview and not bullets:
        raise GeminiSummaryError("gemini returned empty overview and bullets")

    header = f"【{topic_display_name}】最近{window_hours}小时"
    lines_out: list[str] = [header, "", "【综述】", overview or "（暂无）", "", "【要点】"]
    for b in bullets[:8]:
        if not b.startswith("-"):
            b = f"- {b}"
        lines_out.append(b)
    lines_out.extend(["", f"（本窗口共 {len(items)} 条帖子原文已完整入库，可在 viewer / 数据库 news_items_raw 中按 run 查看。）"])
    summary = "\n".join(lines_out)
    return summary, sentiment


_DIGEST_FLASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "3-5 句中文宏观/市场综述，归纳下列快讯的共性主题与影响，禁止逐条复读标题。",
        },
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "takeaway": {
                        "type": "string",
                        "description": "一条完整中文要点，40-90 字，改写归纳勿照抄标题。",
                    },
                    "source_index": {
                        "type": "integer",
                        "description": "对应输入快讯编号，从 1 开始。",
                    },
                },
                "required": ["takeaway", "source_index"],
            },
            "description": "3-5 条要点，每条对应一条 source_index。",
        },
    },
    "required": ["overview", "highlights"],
}


@retry(
    wait=wait_exponential(multiplier=2, min=3, max=55),
    stop=stop_after_attempt(4),
    retry=retry_if_exception(_gemini_should_retry),
    reraise=True,
)
def summarize_flash_digest_with_gemini(
    items: list[dict[str, str | None]],
    *,
    max_items: int = 8,
) -> dict[str, Any]:
    """东财快讯 → 简报综述（含要点与来源编号，链接由调用方拼接）。"""
    if not gemini_available():
        raise GeminiSummaryError("GEMINI_API_KEY not configured")

    lines: list[str] = []
    for idx, item in enumerate(items[:max_items], start=1):
        title = _clip(item.get("title") or "", 120)
        summary = _clip(item.get("summary") or "", 200)
        lines.append(f"[{idx}] 标题: {title}\n摘要: {summary}")

    prompt = f"""你是 A 股/宏观资讯编辑。以下为东方财富全球快讯（编号 [1]、[2]…）。

输出合法 JSON（无 markdown 围栏）：
{{
  "overview": "3-5 句中文综述：归纳主题、影响面、与 A 股/港股的关联；不得逐条罗列标题。",
  "highlights": [
    {{"takeaway": "一句改写要点", "source_index": 1}}
  ]
}}

要求：
- highlights 3-5 条，source_index 必须对应下方编号
- 禁止复制标题超过 12 个连续汉字
- 字符串内少用英文双引号

快讯：
{chr(10).join(lines) if lines else "(无)"}"""

    url = f"{GEMINI_API_BASE_URL.rstrip('/')}/models/{GEMINI_MODEL}:generateContent"
    _pace_gemini_http()
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "safetySettings": _GEMINI_SAFETY_SETTINGS,
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": max(768, GEMINI_MAX_OUTPUT_TOKENS),
            "responseMimeType": "application/json",
            "responseSchema": _DIGEST_FLASH_SCHEMA,
        },
    }
    resp = _gemini_http_post(
        url,
        json=payload,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise GeminiSummaryError(f"gemini http {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise GeminiSummaryError("gemini returned no candidates")
    text_out = "".join(
        part.get("text", "")
        for part in ((candidates[0].get("content") or {}).get("parts") or [])
    ).strip()
    if not text_out:
        raise GeminiSummaryError("gemini empty digest response")

    parsed = _extract_json_block(text_out)
    overview = str(parsed.get("overview", "")).strip()
    highlights_raw = parsed.get("highlights") or []
    highlights: list[dict[str, Any]] = []
    if isinstance(highlights_raw, list):
        for h in highlights_raw:
            if not isinstance(h, dict):
                continue
            takeaway = str(h.get("takeaway", "")).strip()
            try:
                source_index = int(h.get("source_index", 0))
            except (TypeError, ValueError):
                continue
            if takeaway and source_index >= 1:
                highlights.append({"takeaway": takeaway, "source_index": source_index})
    if not overview and not highlights:
        raise GeminiSummaryError("gemini empty digest overview/highlights")
    return {"overview": overview, "highlights": highlights[:6]}


def _answer_info_question_http(prompt: str) -> str:
    url = f"{GEMINI_API_BASE_URL.rstrip('/')}/models/{GEMINI_MODEL}:generateContent"
    _pace_gemini_http()
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "safetySettings": _GEMINI_SAFETY_SETTINGS,
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": 1536},
    }
    resp = _gemini_http_post(
        url,
        json=payload,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        timeout=GEMINI_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise GeminiSummaryError(f"gemini http {resp.status_code}: {(resp.text or '')[:300]}")
    body = resp.json()
    cands = body.get("candidates") or []
    if not cands:
        raise GeminiSummaryError("gemini no candidates")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text_out = "".join(str(p.get("text", "")) for p in parts).strip()
    if not text_out:
        raise GeminiSummaryError("gemini empty answer")
    return text_out.strip()


@retry(
    wait=_gemini_retry_wait,
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_gemini_should_retry),
    reraise=True,
)
def answer_info_question_with_gemini(
    *,
    question: str,
    hits: list[Any],
    data_context: str | None = None,
) -> str:
    """生成【结论】【观点】两段；引用来源清单由调用方另行附上。"""
    if not gemini_available():
        raise GeminiSummaryError("GEMINI_API_KEY not configured")

    lines: list[str] = []
    for idx, h in enumerate(hits[:8], start=1):
        src = getattr(h, "source_label", None) or (h.get("source") if isinstance(h, dict) else "?")
        title = getattr(h, "title", None) or (h.get("title") if isinstance(h, dict) else "")
        snip = getattr(h, "snippet", None) or (h.get("snippet") if isinstance(h, dict) else "")
        url = getattr(h, "url", None) or (h.get("url") if isinstance(h, dict) else "")
        lines.append(
            f"[{idx}] 来源={src} | 标题={_clip(str(title), 220)} | "
            f"摘要={_clip(str(snip or ''), 400)} | 链接={url or '无'}"
        )

    data_block = (data_context or "").strip() or "（无量化数据上下文）"
    news_block = "\n".join(lines) if lines else "（无新闻片段，仅可依据量化数据作答）"

    prompt = f"""你是资深 A 股/宏观资讯分析师。用户问：{question.strip()}

【量化数据（东财等，优先采信）】
{data_block}

【新闻片段 [1][2]…（每条含来源、标题、摘要、链接）】
{news_block}

请只输出以下两段（纯文本，不要 markdown # 号，不要编造未给出的数字或链接）：

【结论】
一句话：偏多 / 偏空 / 中性 / 数据不足；必须写出关键数字（如净流入亿元、融资余额等），禁止空泛词如「买卖交织」「表现复杂」。

【观点】
- 共 2～4 条，每条独立一行，以「·」开头。
- 每条必须标注依据：写「据数据」或「据[n]」；若引用新闻，简述事实并带 [n]。
- 若没有新闻只有数据，明确写「本条仅据东财数据，暂无相关新闻标题」。
- 可写短期关注点或风险，但要具体。

总字数不超过 450 字。不要输出【引用来源】，来源列表由系统另附。"""

    return _answer_info_question_http(prompt)


def _check_gemini_connectivity_once() -> dict[str, Any]:
    """最小 generateContent 探测；返回 ok/http_status/error 片段，响应中绝不包含 API Key。"""
    if not gemini_available():
        return {"ok": False, "error": "GEMINI_API_KEY 未配置或为空"}
    base: dict[str, Any] = dict(gemini_outbound_proxy_info())
    url = f"{GEMINI_API_BASE_URL.rstrip('/')}/models/{GEMINI_MODEL}:generateContent"
    _pace_gemini_http()
    payload = {
        "contents": [{"parts": [{"text": '只回复合法 JSON：{"ok":true}'}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 32,
            "responseMimeType": "application/json",
        },
    }
    try:
        resp = _gemini_http_post(
            url,
            json=payload,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=min(30, GEMINI_TIMEOUT_SECONDS),
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
            "model": GEMINI_MODEL,
        }
        if resp.status_code == 429:
            parsed = _parse_gemini_quota_hint(err_text)
            out["hint"] = parsed or (
                f"Gemini 429 配额超限（{GEMINI_MODEL}），详见 https://ai.dev/rate-limit"
            )
            if "GenerateRequestsPerDayPerProjectPerModel" in err_text or (
                parsed and ("当日" in parsed or "次/天" in parsed)
            ):
                out["quota_kind"] = "daily"
            elif parsed and "每分钟" in parsed:
                out["quota_kind"] = "rpm"
            else:
                out["quota_kind"] = "unknown"
        return out
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        return {**base, "ok": False, "error": f"invalid json body: {exc}"}
    cands = body.get("candidates") or []
    if not cands:
        return {
            **base,
            "ok": False,
            "http_status": resp.status_code,
            "error": "no candidates in response",
            "model": GEMINI_MODEL,
        }
    return {**base, "ok": True, "http_status": resp.status_code, "model": GEMINI_MODEL}


def check_gemini_connectivity() -> dict[str, Any]:
    """探测 Gemini；遇 429 时按 API 提示等待后自动重试一次。"""
    if not gemini_available():
        return {"ok": False, "error": "GEMINI_API_KEY 未配置或为空"}
    first = _check_gemini_connectivity_once()
    if first.get("ok") or first.get("http_status") != 429:
        return first
    wait_s = _parse_retry_seconds_from_error(str(first.get("error", "")))
    if wait_s is None:
        return first
    time_module.sleep(min(wait_s + 1.5, 90))
    second = _check_gemini_connectivity_once()
    second["retried_after_seconds"] = round(wait_s + 1.5, 1)
    if not second.get("ok"):
        parsed = _parse_gemini_quota_hint(str(second.get("error", "")))
        second.setdefault(
            "hint",
            parsed
            or "仍失败：若 quota_kind=daily 则需等 UTC 日切、换新 API Key 或开通计费。",
        )
    return second
