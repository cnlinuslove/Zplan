"""LLM 客户端（已切换至 DeepSeek，保留原函数名向后兼容）。

新闻摘要 / 快讯简报 / 资讯问答 / 连通性探测。
"""

from __future__ import annotations

import json
import logging
import re
import time as time_module
from typing import Any

import requests

from config import (
    DEEPSEEK_API_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MIN_SECONDS_BETWEEN_CALLS,
    GEMINI_MODEL,
    GEMINI_SUMMARY_CHARS_PER_ITEM,
    GEMINI_SUMMARY_MAX_ITEMS,
    LLM_SUMMARY_ENABLED,
)
from outbound_http import get_proxied_requests_session, resolve_effective_proxy_url

logger = logging.getLogger(__name__)

# ── 模型选择：优先 DEEPSEEK_MODEL，回退到 GEMINI_MODEL ────────────


def _effective_model() -> str:
    """返回实际使用的模型名。"""
    return DEEPSEEK_MODEL or GEMINI_MODEL or "deepseek-chat"


def _effective_max_tokens(default_min: int = 512) -> int:
    return max(default_min, DEEPSEEK_MAX_OUTPUT_TOKENS or GEMINI_MAX_OUTPUT_TOKENS or 4096)


# ── DeepSeek 限额提示（替代原 Gemini 429 解析）───────────────────────


def _parse_deepseek_quota_hint(error_text: str) -> str | None:
    """从 DeepSeek 错误响应中提取可读的限额提示。"""
    try:
        body = json.loads(error_text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        err = body.get("error") or {}
        msg = str(err.get("message") or "")
        code = str(err.get("code") or "")
        if "429" in code or "rate" in msg.lower():
            return (
                f"DeepSeek 请求频率超限（HTTP 429）。"
                "请稍后重试，或访问 https://platform.deepseek.com/usage 查看用量。"
            )
        if "401" in code or "auth" in msg.lower():
            return "DeepSeek API Key 无效（HTTP 401），请检查 .env 中 DEEPSEEK_API_KEY"
        if "insufficient" in msg.lower():
            return f"DeepSeek 账户余额不足，请充值：https://platform.deepseek.com/top_up"
    return None


# ── HTTP helpers ─────────────────────────────────────────────────────


def _http_post(url: str, **kwargs: Any) -> requests.Response:
    return get_proxied_requests_session().post(url, **kwargs)


def _completion_url() -> str:
    return f"{DEEPSEEK_API_BASE_URL.rstrip('/')}/chat/completions"


def deepseek_available() -> bool:
    return bool(DEEPSEEK_API_KEY.strip())


# 向后兼容别名
gemini_available = deepseek_available


def gemini_outbound_proxy_info() -> dict[str, str | None]:
    proxy_url, source = resolve_effective_proxy_url()
    return {"outbound_proxy": proxy_url, "outbound_proxy_source": source}


# ── 旧异常类保留（向后兼容）─────────────────────────────────────────


class GeminiSummaryError(RuntimeError):
    """LLM 摘要/问答失败。"""
    pass


class GeminiBlockedError(GeminiSummaryError):
    """内容安全拦截（DeepSeek 极少触发，保留兼容）。"""
    pass


# ── JSON 解析 ────────────────────────────────────────────────────────


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
        raise GeminiSummaryError("LLM 空响应")
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise GeminiSummaryError(f"LLM 响应无 JSON 对象, text_head={text[:120]!r}")
    return json.loads(match.group(0))


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ── 低层 DeepSeek chat/completions ──────────────────────────────────


def _chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.25,
    max_tokens: int | None = None,
    model: str | None = None,
    response_format: dict[str, str] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    timeout: int | None = None,
) -> requests.Response:
    """发送 chat/completions 请求。"""
    body: dict[str, Any] = {
        "model": model or _effective_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or _effective_max_tokens(),
    }
    if response_format:
        body["response_format"] = response_format
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice

    return _http_post(
        _completion_url(),
        json=body,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=timeout or DEEPSEEK_TIMEOUT_SECONDS,
    )


def _extract_text_and_check(resp: requests.Response) -> str:
    """从 chat/completions 响应提取文本，异常时抛 GeminiSummaryError。"""
    if resp.status_code >= 400:
        raise GeminiSummaryError(
            f"LLM HTTP {resp.status_code}: {(resp.text or '')[:300]}"
        )
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise GeminiSummaryError("LLM 返回无 choices")
    msg = choices[0].get("message") or {}
    text_out = str(msg.get("content", "")).strip()
    if not text_out:
        finish = str(choices[0].get("finish_reason", ""))
        if finish and finish != "stop":
            raise GeminiSummaryError(f"LLM 空响应 finish_reason={finish!r}")
        raise GeminiSummaryError("LLM 返回空内容")
    return text_out


# ── 重试逻辑 ─────────────────────────────────────────────────────────


def _llm_should_retry(exc: BaseException) -> bool:
    if isinstance(exc, GeminiBlockedError):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, GeminiSummaryError):
        s = str(exc)
        if "gemini blocked" in s:
            return False
        m = re.search(r"LLM HTTP (\d{3})", s)
        if m:
            code = int(m.group(1))
            return code == 429 or code >= 500
        return True
    if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
        return True
    return False


# ── 新闻摘要 ─────────────────────────────────────────────────────────

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


def summarize_news_with_gemini(
    *,
    topic_display_name: str,
    window_hours: int,
    items: list[dict[str, str | None]],
    max_items: int | None = None,
    max_chars_per_item: int | None = None,
) -> tuple[str, str]:
    """新闻摘要（已切换 DeepSeek，保留函数名兼容）。"""
    if not deepseek_available():
        raise GeminiSummaryError("DEEPSEEK_API_KEY 未配置")

    cap_n = max_items if max_items is not None else GEMINI_SUMMARY_MAX_ITEMS
    cap_c = max_chars_per_item if max_chars_per_item is not None else GEMINI_SUMMARY_CHARS_PER_ITEM

    lines: list[str] = []
    for idx, item in enumerate(items[:cap_n], start=1):
        text = _clip(item.get("text") or "", cap_c)
        lines.append(f"[{idx}] {text}")

    schema_text = json.dumps(_NEWS_SUMMARY_SCHEMA, ensure_ascii=False, indent=2)

    system = (
        "你是一个 JSON-only API。始终只输出一个合法的 JSON 对象，不要包含 markdown 围栏。\n\n"
        f"输出必须符合以下 JSON Schema：\n{schema_text}"
    )

    user = f"""你是资深中文财经/宏观资讯编辑。下面编号 [1]、[2]… 是来自 X 的帖子原文片段（可能含噪声）。主题为「{topic_display_name}」，时间窗口约最近 {window_hours} 小时。

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

    resp = _chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.25,
        max_tokens=_effective_max_tokens(512),
        response_format={"type": "json_object"},
    )

    text_out = _extract_text_and_check(resp)

    try:
        parsed = _extract_json_block(text_out)
    except json.JSONDecodeError as exc:
        raise GeminiSummaryError(f"LLM JSON 解析: {exc}; head={text_out[:200]!r}") from exc

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
        raise GeminiSummaryError("LLM 返回空 overview 与 bullets")

    header = f"【{topic_display_name}】最近{window_hours}小时"
    lines_out: list[str] = [header, "", "【综述】", overview or "（暂无）", "", "【要点】"]
    for b in bullets[:8]:
        if not b.startswith("-"):
            b = f"- {b}"
        lines_out.append(b)
    lines_out.extend([
        "",
        f"（本窗口共 {len(items)} 条帖子原文已完整入库，可在 viewer / 数据库 news_items_raw 中按 run 查看。）",
    ])
    summary = "\n".join(lines_out)
    return summary, sentiment


# ── 快讯简报 ─────────────────────────────────────────────────────────

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


def summarize_flash_digest_with_gemini(
    items: list[dict[str, str | None]],
    *,
    max_items: int = 8,
) -> dict[str, Any]:
    """东财快讯 → 简报综述（已切换 DeepSeek）。"""
    if not deepseek_available():
        raise GeminiSummaryError("DEEPSEEK_API_KEY 未配置")

    lines: list[str] = []
    for idx, item in enumerate(items[:max_items], start=1):
        title = _clip(item.get("title") or "", 120)
        summary = _clip(item.get("summary") or "", 200)
        lines.append(f"[{idx}] 标题: {title}\n摘要: {summary}")

    schema_text = json.dumps(_DIGEST_FLASH_SCHEMA, ensure_ascii=False, indent=2)

    system = (
        "你是一个 JSON-only API。始终只输出一个合法的 JSON 对象，不要包含 markdown 围栏。\n\n"
        f"输出必须符合以下 JSON Schema：\n{schema_text}"
    )

    user = f"""你是 A 股/宏观资讯编辑。以下为东方财富全球快讯（编号 [1]、[2]…）。

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

    resp = _chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.25,
        max_tokens=_effective_max_tokens(768),
        response_format={"type": "json_object"},
    )

    text_out = _extract_text_and_check(resp)

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
        raise GeminiSummaryError("LLM 空摘要 overview/highlights")
    return {"overview": overview, "highlights": highlights[:6]}


# ── 资讯问答 ─────────────────────────────────────────────────────────


def answer_info_question_with_gemini(
    *,
    question: str,
    hits: list[Any],
    data_context: str | None = None,
) -> str:
    """资讯问答（已切换 DeepSeek，保留函数名兼容）。"""
    if not deepseek_available():
        raise GeminiSummaryError("DEEPSEEK_API_KEY 未配置")

    lines: list[str] = []
    for idx, h in enumerate(hits[:8], start=1):
        src = (
            getattr(h, "source_label", None)
            or (h.get("source") if isinstance(h, dict) else "?")
        )
        title = (
            getattr(h, "title", None)
            or (h.get("title") if isinstance(h, dict) else "")
        )
        snip = (
            getattr(h, "snippet", None)
            or (h.get("snippet") if isinstance(h, dict) else "")
        )
        url = (
            getattr(h, "url", None)
            or (h.get("url") if isinstance(h, dict) else "")
        )
        lines.append(
            f"[{idx}] 来源={src} | 标题={_clip(str(title), 220)} | "
            f"摘要={_clip(str(snip or ''), 400)} | 链接={url or '无'}"
        )

    data_block = (data_context or "").strip() or "（无量化数据上下文）"
    news_block = "\n".join(lines) if lines else "（无新闻片段，仅可依据量化数据作答）"

    system = "你是资深 A 股/宏观资讯分析师。回答必须简洁、有依据、输出纯文本。"

    user = f"""你是资深 A 股/宏观资讯分析师。用户问：{question.strip()}

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

    resp = _chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.25,
        max_tokens=1536,
    )

    return _extract_text_and_check(resp)


# ── 连通性探测 ───────────────────────────────────────────────────────


def _check_llm_connectivity_once() -> dict[str, Any]:
    """最小 chat/completions 探测。"""
    if not deepseek_available():
        return {"ok": False, "error": "DEEPSEEK_API_KEY 未配置或为空"}
    base: dict[str, Any] = dict(gemini_outbound_proxy_info())
    try:
        resp = _chat_completion(
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
            "model": _effective_model(),
        }
        if resp.status_code == 429:
            hint = _parse_deepseek_quota_hint(err_text)
            out["hint"] = hint or "DeepSeek 429 配额超限"
        return out

    return {**base, "ok": True, "http_status": resp.status_code, "model": _effective_model()}


def check_gemini_connectivity() -> dict[str, Any]:
    """探测 LLM API 是否可达（已切换 DeepSeek，保留函数名兼容）。"""
    if not deepseek_available():
        return {"ok": False, "error": "DEEPSEEK_API_KEY 未配置或为空"}
    return _check_llm_connectivity_once()
