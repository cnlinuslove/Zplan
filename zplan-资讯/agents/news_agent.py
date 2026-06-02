from __future__ import annotations

import os
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from requests.exceptions import ConnectTimeout, ReadTimeout, RequestException

from sqlalchemy import desc, delete, select
from sqlalchemy.dialects.sqlite import insert
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from tenacity import RetryError

from agents.news_filter import filter_news_items
from config import (
    GEMINI_MIN_SECONDS_BETWEEN_TOPICS,
    GEMINI_SUMMARY_CHARS_PER_ITEM,
    GEMINI_SUMMARY_MAX_ITEMS,
    LLM_SUMMARY_ENABLED,
    NEWS_FETCH_LIMIT_PER_TOPIC,
    NEWS_WINDOW_HOURS,
    WECHAT_PUSH_DIGEST,
    WECHAT_PUSH_MODE,
    WECHAT_PUSH_WEBHOOK,
    X_API_BASE_URL,
    X_BEARER_TOKEN,
    X_HTTP_TIMEOUT_SECONDS,
    X_FETCH_USERNAMES,
    X_MAX_PAGES_PER_TOPIC,
    X_MAX_RESULTS_PER_PAGE,
    X_QUERY_EXCLUDE_SUFFIX,
    X_RATE_LIMIT_SLEEP_SECONDS,
    X_FAILOVER_TO_PLACEHOLDER,
)
from llm.gemini_client import gemini_available, summarize_news_with_gemini
from models import NewsItemRaw, NewsRun, SessionLocal, TopicConfig, init_db
from outbound_http import get_x_api_session

logger = logging.getLogger(__name__)


DEFAULT_TOPICS = [
    ("trump_updates", "特朗普动态", "Trump OR 特朗普"),
    ("crypto_sentiment", "数字货币情绪", "(Bitcoin OR BTC OR ETH OR 以太坊) (加密货币 OR ETF OR 链上 OR 交易所 OR SEC OR Binance OR 稳定币 OR 监管)"),
    ("us_market_hotspots", "美股情绪和热点", "US stocks OR Nasdaq OR S&P 500"),
    (
        "cn_market_hotspots",
        "A股情绪和热点",
        "(上证 OR 深证 OR 创业板 OR 沪深300 OR 北向 OR 证监会 OR 央行 OR 港股通) "
        "(A股 OR 陆股 OR 恒生 OR 指数 OR 板块 OR 财报)",
    ),
    ("us_cn_relation", "中美关系热点", "中美关系 OR US China relations"),
    ("israel_palestine", "巴以冲突热点", "Israel Palestine conflict"),
    ("us_russia_relation", "俄美关系热点", "US Russia relations"),
]


@dataclass
class RawNewsItem:
    source: str
    post_id: str
    author: str | None
    published_at: datetime
    text: str
    url: str | None


class XApiRateLimitError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class XApiHttpError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"x api http error: {status_code}")
        self.status_code = status_code
        self.body = body


class XApiServerError(XApiHttpError):
    pass


def map_exception_to_user_error(exc: Exception) -> dict:
    normalized = unwrap_retry_exception(exc)
    if normalized is not exc:
        return map_exception_to_user_error(normalized)
    if isinstance(exc, XApiRateLimitError):
        retry_after = exc.retry_after_seconds or X_RATE_LIMIT_SLEEP_SECONDS
        return {
            "code": "X_RATE_LIMITED",
            "message": "X 接口触发限流，请稍后重试。",
            "action": "等待限流窗口恢复后再触发 run-once。",
            "retry_after_seconds": retry_after,
        }
    if isinstance(exc, XApiHttpError):
        status = exc.status_code
        body_lower = (exc.body or "").lower()
        if status in (401, 403):
            return {
                "code": "X_AUTH_INVALID",
                "message": "X 认证失败或权限不足。",
                "action": "检查 X_BEARER_TOKEN 是否有效，并确认应用具备 recent search 权限。",
                "status_code": status,
            }
        if status == 400:
            return {
                "code": "X_QUERY_INVALID",
                "message": "X 查询参数不合法。",
                "action": "检查 topic query 语法，避免非法操作符或超长表达式。",
                "status_code": status,
            }
        if status == 402:
            return {
                "code": "X_CREDITS_DEPLETED",
                "message": "X API 账户额度已用尽（CreditsDepleted）。",
                "action": "登录 X Developer Portal 充值或升级套餐；临时可设 X_FAILOVER_TO_PLACEHOLDER=true 保持任务不中断。",
                "status_code": status,
            }
        if status == 404:
            return {
                "code": "X_ENDPOINT_NOT_FOUND",
                "message": "X 接口地址不可用。",
                "action": "检查 X_API_BASE_URL 配置，确认 API 版本路径正确。",
                "status_code": status,
            }
        if status >= 500:
            return {
                "code": "X_SERVER_ERROR",
                "message": "X 服务端异常。",
                "action": "等待数分钟后重试；若持续失败，先切回占位模式保证流程不中断。",
                "status_code": status,
            }
        if "authorization" in body_lower or "token" in body_lower:
            return {
                "code": "X_AUTH_INVALID",
                "message": "X 认证信息异常。",
                "action": "重新生成并更新 X_BEARER_TOKEN。",
                "status_code": status,
            }
        return {
            "code": "X_REQUEST_FAILED",
            "message": "X 请求失败。",
            "action": "检查 token、query 和网络连通性。",
            "status_code": status,
        }
    if isinstance(exc, (ConnectTimeout, ReadTimeout)):
        return {
            "code": "X_NETWORK_TIMEOUT",
            "message": "连接 X 超时。",
            "action": "检查代理链路是否生效，或延长 X_HTTP_TIMEOUT_SECONDS。",
        }
    return {
        "code": "INTERNAL_ERROR",
        "message": "系统内部异常。",
        "action": "查看日志并重试；如持续失败，请定位堆栈。",
    }


def unwrap_retry_exception(exc: Exception) -> Exception:
    if isinstance(exc, RetryError) and exc.last_attempt is not None:
        inner = exc.last_attempt.exception()
        if isinstance(inner, Exception):
            return inner
    return exc


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_x_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return _ensure_utc(parsed)


_DEFAULT_X_EXCLUDE = (
    "-telegram -t.me -whatsapp -forexsignal -giveaway "
    '-"free signal" -"98% accuracy" -"dm me" -"signals available"'
)


def _build_x_query(topic: TopicConfig) -> str:
    """构造 X Recent Search 查询串：转推过滤、语言、全局排除营销噪声。"""
    base = topic.query.strip()
    if not base:
        base = "market"
    if "-is:retweet" not in base:
        base = f"({base}) -is:retweet"

    if topic.topic_key == "cn_market_hotspots":
        if "lang:" not in base:
            base = f"{base} lang:zh"
    elif "lang:" not in base:
        base = f"{base} (lang:en OR lang:zh)"

    extra = (X_QUERY_EXCLUDE_SUFFIX or _DEFAULT_X_EXCLUDE).strip()
    if extra:
        base = f"{base} {extra}"
    return base


def seed_default_topics() -> None:
    with SessionLocal() as session:
        for topic_key, display_name, query in DEFAULT_TOPICS:
            stmt = insert(TopicConfig).values(
                topic_key=topic_key,
                display_name=display_name,
                query=query,
                enabled=True,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["topic_key"],
                set_={
                    "display_name": stmt.excluded.display_name,
                    "query": stmt.excluded.query,
                },
            )
            session.execute(stmt)
        session.commit()


def get_enabled_topics() -> list[TopicConfig]:
    with SessionLocal() as session:
        rows = session.execute(
            select(TopicConfig).where(TopicConfig.enabled.is_(True)).order_by(TopicConfig.id.asc())
        ).scalars()
        return list(rows)


@retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3))
def fetch_x_news_for_topic(topic: TopicConfig, since_dt: datetime) -> list[RawNewsItem]:
    return fetch_news_for_topic(topic, since_dt, force_placeholder=False)


def fetch_news_for_topic(
    topic: TopicConfig, since_dt: datetime, force_placeholder: bool = False
) -> list[RawNewsItem]:
    if force_placeholder:
        return _fetch_x_news_placeholder(topic, since_dt)
    if X_BEARER_TOKEN:
        try:
            return _fetch_x_news_from_api(topic, since_dt)
        except Exception as exc:
            if X_FAILOVER_TO_PLACEHOLDER:
                logger.warning(
                    "[WARN] topic=%s X抓取失败(%s)，降级占位抓取。",
                    topic.topic_key,
                    exc.__class__.__name__,
                )
                return _fetch_x_news_placeholder(topic, since_dt)
            raise
    return _fetch_x_news_placeholder(topic, since_dt)


def can_reach_x_api() -> bool:
    """Network reachability only (HTTP response received). 402/401 still counts as reachable."""
    if not X_BEARER_TOKEN:
        return False

    url = f"{X_API_BASE_URL.rstrip('/')}/tweets/search/recent"
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    params = {"query": "market", "max_results": 10}
    timeout = min(20, max(8, X_HTTP_TIMEOUT_SECONDS))
    try:
        session = get_x_api_session()
        resp = session.get(url, headers=headers, params=params, timeout=timeout)
        return resp.status_code < 500
    except (ConnectTimeout, ReadTimeout, RequestException):
        return False


def _fetch_x_news_placeholder(topic: TopicConfig, since_dt: datetime) -> list[RawNewsItem]:
    _ = since_dt
    logger.info("[INFO] topic=%s 使用占位抓取器，query=%s", topic.topic_key, topic.query)
    now = datetime.now(timezone.utc)
    return [
        RawNewsItem(
            source="x_placeholder",
            post_id=f"{topic.topic_key}-{int(now.timestamp())}-{idx}",
            author="system",
            published_at=now - timedelta(minutes=idx * 3),
            text=f"[{topic.display_name}] 示例资讯 {idx + 1}：{topic.query}",
            url=None,
        )
        for idx in range(min(3, NEWS_FETCH_LIMIT_PER_TOPIC))
    ]


def _fetch_x_news_from_api(topic: TopicConfig, since_dt: datetime) -> list[RawNewsItem]:
    logger.info("[INFO] topic=%s 使用X API抓取", topic.topic_key)
    target_count = max(1, NEWS_FETCH_LIMIT_PER_TOPIC)
    per_page = min(max(10, X_MAX_RESULTS_PER_PAGE), 100, target_count)
    url = f"{X_API_BASE_URL.rstrip('/')}/tweets/search/recent"
    start_time = _ensure_utc(since_dt).isoformat().replace("+00:00", "Z")
    query = _build_x_query(topic)
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    rows: list[RawNewsItem] = []
    seen_post_ids: set[str] = set()
    next_token: str | None = None

    for page in range(X_MAX_PAGES_PER_TOPIC):
        if len(rows) >= target_count:
            break
        params = {
            "query": query,
            "max_results": per_page,
            "tweet.fields": "created_at,text",
            "start_time": start_time,
        }
        if X_FETCH_USERNAMES:
            params["tweet.fields"] = "created_at,author_id,text"
            params["expansions"] = "author_id"
            params["user.fields"] = "username,name"
        if next_token:
            params["next_token"] = next_token

        payload = _x_api_get_json(url=url, params=params, headers=headers)
        users: dict[str, str] = {}
        if X_FETCH_USERNAMES:
            for user in payload.get("includes", {}).get("users", []):
                uid = str(user.get("id", ""))
                if uid:
                    users[uid] = user.get("username") or user.get("name")

        for item in payload.get("data", []):
            post_id = str(item.get("id", "")).strip()
            if (not post_id) or (post_id in seen_post_ids):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            author: str | None = None
            if X_FETCH_USERNAMES:
                author_id = str(item.get("author_id", "")).strip()
                author = users.get(author_id)
            seen_post_ids.add(post_id)
            rows.append(
                RawNewsItem(
                    source="x_api",
                    post_id=post_id,
                    author=author,
                    published_at=_parse_x_time(item.get("created_at")),
                    text=text,
                    url=f"https://x.com/i/web/status/{post_id}",
                )
            )
            if len(rows) >= target_count:
                break

        next_token = payload.get("meta", {}).get("next_token")
        logger.info(
            "[INFO] topic=%s X抓取 page=%s items=%s has_next=%s",
            topic.topic_key,
            page + 1,
            len(rows),
            bool(next_token),
        )
        if not next_token:
            break
    filtered = filter_news_items(topic.topic_key, rows)
    if filtered:
        return filtered[:target_count]
    logger.warning("[WARN] topic=%s 过滤后无结果，保留原始 %s 条", topic.topic_key, len(rows))
    return rows[:target_count]


@retry(
    retry=retry_if_exception_type((XApiRateLimitError, XApiServerError)),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
)
def _x_api_get_json(url: str, params: dict, headers: dict) -> dict:
    session = get_x_api_session()
    resp = session.get(url, params=params, headers=headers, timeout=X_HTTP_TIMEOUT_SECONDS)
    if resp.status_code == 429:
        reset_ts = resp.headers.get("x-rate-limit-reset")
        sleep_seconds = X_RATE_LIMIT_SLEEP_SECONDS
        if reset_ts and reset_ts.isdigit():
            now_ts = int(time.time())
            sleep_seconds = max(1, int(reset_ts) - now_ts)
        logger.warning("[WARN] X API限流，休眠 %s 秒后重试", sleep_seconds)
        time.sleep(sleep_seconds)
        raise XApiRateLimitError("x api rate limited", retry_after_seconds=sleep_seconds)
    if resp.status_code >= 500:
        raise XApiServerError(status_code=resp.status_code, body=resp.text[:500])
    if resp.status_code >= 400:
        raise XApiHttpError(status_code=resp.status_code, body=resp.text[:500])
    return resp.json()


def _summarize_items_rule_based(topic: TopicConfig, items: list[RawNewsItem]) -> tuple[str, str]:
    """无 Gemini 时的降级：明确为「原文摘录」并提示配置 API，避免伪装成智能摘要。"""
    header = f"【{topic.display_name}】最近{NEWS_WINDOW_HOURS}小时"
    hint = (
        "【说明】当前为规则摘录（非归纳摘要）。配置环境变量 GEMINI_API_KEY 后，"
        "将使用 Gemini 生成「综述 + 要点」并自动过滤推广噪声。"
    )
    blocks: list[str] = []
    for i, item in enumerate(items[:6], start=1):
        t = item.text.strip()
        if len(t) > 800:
            t = t[:799] + "…"
        blocks.append(f"—— [{i}] 原文 ——\n{t}")
    footer = f"——\n（共 {len(items)} 条全文已写入 news_items_raw，按本 run 关联可查，未截断保存。）"
    summary = "\n\n".join([header, hint, *blocks, footer])

    text_blob = " ".join(i.text.lower() for i in items)
    positive_hits = sum(k in text_blob for k in ["上涨", "利好", "突破", "rebound", "bull", "大涨"])
    negative_hits = sum(k in text_blob for k in ["下跌", "利空", "冲突", "risk", "bear", "暴跌"])
    sentiment = "neutral"
    if positive_hits > negative_hits:
        sentiment = "positive"
    elif negative_hits > positive_hits:
        sentiment = "negative"
    return summary, sentiment


def summarize_items(topic: TopicConfig, items: list[RawNewsItem]) -> tuple[str, str]:
    if not items:
        return f"{topic.display_name}: 本窗口暂无新资讯。", "neutral"

    if LLM_SUMMARY_ENABLED and gemini_available():
        try:
            payload = [{"author": item.author, "text": item.text} for item in items]
            summary, sentiment = summarize_news_with_gemini(
                topic_display_name=topic.display_name,
                window_hours=NEWS_WINDOW_HOURS,
                items=payload,
                max_items=GEMINI_SUMMARY_MAX_ITEMS,
                max_chars_per_item=GEMINI_SUMMARY_CHARS_PER_ITEM,
            )
            logger.info("[INFO] topic=%s 使用 Gemini 摘要", topic.topic_key)
            return summary, sentiment
        except Exception as exc:
            root = unwrap_retry_exception(exc)
            logger.warning(
                "[WARN] topic=%s Gemini 摘要失败，降级规则摘要: %s",
                topic.topic_key,
                root,
            )

    return _summarize_items_rule_based(topic, items)


def _window_dedupe_key(topic_key: str, window_start: datetime, window_end: datetime) -> str:
    raw = f"{topic_key}|{window_start.isoformat()}|{window_end.isoformat()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def persist_run_and_items(
    topic_key: str,
    window_start: datetime,
    window_end: datetime,
    summary: str,
    sentiment: str,
    items: list[RawNewsItem],
) -> int:
    dedupe_key = _window_dedupe_key(topic_key, window_start, window_end)

    with SessionLocal() as session:
        run = session.execute(
            select(NewsRun).where(
                NewsRun.topic_key == topic_key,
                NewsRun.window_start == window_start,
                NewsRun.window_end == window_end,
            )
        ).scalar_one_or_none()

        if run is None:
            run = NewsRun(
                topic_key=topic_key,
                window_start=window_start,
                window_end=window_end,
                summary=summary,
                sentiment=sentiment,
                dedupe_key=dedupe_key,
            )
            session.add(run)
            session.flush()
        else:
            run.summary = summary
            run.sentiment = sentiment
            run.dedupe_key = dedupe_key
            session.flush()
            session.execute(delete(NewsItemRaw).where(NewsItemRaw.run_id == run.id))
            session.flush()
            logger.info("[INFO] topic=%s 已更新同时间窗 run_id=%s 的摘要与原文", topic_key, run.id)

        if items:
            stmt = insert(NewsItemRaw).values(
                [
                    {
                        "run_id": run.id,
                        "source": item.source,
                        "post_id": item.post_id,
                        "author": item.author,
                        "published_at": item.published_at.replace(tzinfo=None),
                        "text": item.text,
                        "url": item.url,
                    }
                    for item in items
                ]
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["source", "post_id"])
            session.execute(stmt)

        session.commit()
        return run.id


def _sentiment_label(sentiment: str) -> str:
    mapping = {"positive": "偏多", "negative": "偏空", "neutral": "中性"}
    return mapping.get(sentiment or "neutral", "中性")


def format_topic_for_wechat(topic_key: str, display_name: str, summary: str, sentiment: str) -> str:
    label = _sentiment_label(sentiment)
    if WECHAT_PUSH_MODE == "markdown":
        title = display_name or topic_key
        body = summary.replace("\n", "\n> ")
        return f"### {title} · {label}\n> {body}"
    return f"【{display_name}·{label}】\n{summary}"


def push_to_wechat(message: str, *, mode: str | None = None) -> bool:
    import requests

    from wechat_limits import WECHAT_MARKDOWN_MAX_BYTES, WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8

    if not WECHAT_PUSH_WEBHOOK:
        logger.warning("[WARN] 未配置 WECHAT_PUSH_WEBHOOK，跳过推送。")
        return False

    use_mode = (mode or WECHAT_PUSH_MODE).lower()
    if use_mode == "markdown":
        content = truncate_wechat_utf8(message, WECHAT_MARKDOWN_MAX_BYTES)
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
    else:
        content = truncate_wechat_utf8(message, WECHAT_TEXT_MAX_BYTES)
        payload = {"msgtype": "text", "text": {"content": content}}
    try:
        resp = requests.post(WECHAT_PUSH_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("errcode", 0) not in (0, None):
            logger.warning("[WARN] 微信推送业务失败: %s", data)
            return False
        logger.info("[INFO] 微信推送成功")
        return True
    except Exception as exc:
        logger.warning("[WARN] 微信推送失败: %s", exc)
        return False


def push_summary_to_wechat(message: str) -> bool:
    return push_to_wechat(message)


def send_wechat_test_message() -> dict:
    sample = (
        "### Z-Plan 测试 · 中性\n"
        "> 【测试】微信推送链路正常。\n"
        "> 配置 WECHAT_PUSH_WEBHOOK 后，每轮 run-once 将推送 Gemini 摘要。"
    )
    ok = push_to_wechat(sample)
    return {
        "ok": ok,
        "webhook_configured": bool(WECHAT_PUSH_WEBHOOK),
        "push_mode": WECHAT_PUSH_MODE,
    }


def query_summary_latest(topic_key: str | None = None) -> list[NewsRun]:
    with SessionLocal() as session:
        stmt = select(NewsRun)
        if topic_key:
            stmt = stmt.where(NewsRun.topic_key == topic_key)
        stmt = stmt.order_by(desc(NewsRun.created_at)).limit(20)
        return list(session.execute(stmt).scalars())


def query_summary_last_days(days: int = 7, topic_key: str | None = None) -> list[NewsRun]:
    start = datetime.utcnow() - timedelta(days=days)
    with SessionLocal() as session:
        stmt = select(NewsRun).where(NewsRun.created_at >= start)
        if topic_key:
            stmt = stmt.where(NewsRun.topic_key == topic_key)
        stmt = stmt.order_by(desc(NewsRun.created_at)).limit(200)
        return list(session.execute(stmt).scalars())


def run_news_cycle(now: datetime | None = None, *, push_wechat: bool = True) -> dict[str, int]:
    init_db()
    seed_default_topics()

    cycle_end = _ensure_utc(now or datetime.now(timezone.utc))
    cycle_start = cycle_end - timedelta(hours=NEWS_WINDOW_HOURS)
    topics = get_enabled_topics()
    smoke = os.getenv("SMOKE_TEST", "").lower() in ("1", "true", "yes")
    if smoke:
        topics = topics[:2]
        logger.info("[INFO] SMOKE_TEST：仅跑前 %s 个 topic，跳过微信推送", len(topics))
    use_x_api = can_reach_x_api() if X_BEARER_TOKEN else False

    logger.info("[INFO] 启动资讯周期任务，topics=%s, use_x_api=%s", len(topics), use_x_api)

    pushed = 0
    saved = 0
    digest_blocks: list[str] = []
    first_topic = True
    for topic in topics:
        items = fetch_news_for_topic(topic, cycle_start, force_placeholder=not use_x_api)
        if (
            not first_topic
            and LLM_SUMMARY_ENABLED
            and gemini_available()
            and GEMINI_MIN_SECONDS_BETWEEN_TOPICS > 0
            and items
        ):
            time.sleep(GEMINI_MIN_SECONDS_BETWEEN_TOPICS)
        first_topic = False
        summary, sentiment = summarize_items(topic, items)
        run_id = persist_run_and_items(
            topic_key=topic.topic_key,
            window_start=cycle_start.replace(tzinfo=None),
            window_end=cycle_end.replace(tzinfo=None),
            summary=summary,
            sentiment=sentiment,
            items=items,
        )
        saved += 1 if run_id else 0
        block = format_topic_for_wechat(
            topic.topic_key, topic.display_name, summary, sentiment
        )
        digest_blocks.append(block)
        if push_wechat and not smoke and not WECHAT_PUSH_DIGEST:
            pushed += 1 if push_to_wechat(block) else 0
        logger.info("[INFO] topic=%s 完成，run_id=%s", topic.topic_key, run_id)

    if push_wechat and not smoke and WECHAT_PUSH_DIGEST and digest_blocks:
        header = f"## Z-Plan 资讯简报 ({cycle_end.strftime('%m-%d %H:%M')} UTC)"
        digest = header + "\n\n" + "\n\n".join(digest_blocks)
        pushed += 1 if push_to_wechat(digest) else 0

    return {"topics": len(topics), "saved_runs": saved, "pushed": pushed}


def format_runs_for_wechat(runs: list[NewsRun]) -> str:
    if not runs:
        return "暂无可用摘要。"
    lines = []
    for row in runs:
        lines.append(
            f"[{row.created_at.strftime('%m-%d %H:%M')}] {row.topic_key} ({row.sentiment or 'na'})\n{row.summary}"
        )
    return "\n\n".join(lines[:5])


def runs_to_dicts(runs: list[NewsRun]) -> list[dict]:
    return [
        {
            "id": row.id,
            "topic_key": row.topic_key,
            "window_start": row.window_start.isoformat(),
            "window_end": row.window_end.isoformat(),
            "summary": row.summary,
            "sentiment": row.sentiment,
            "created_at": row.created_at.isoformat(),
            "dedupe_key": row.dedupe_key,
        }
        for row in runs
    ]


def get_history_payload(mode: str, topic_key: str | None = None) -> dict:
    if mode == "latest":
        rows = query_summary_latest(topic_key=topic_key)
    else:
        rows = query_summary_last_days(days=7, topic_key=topic_key)
    return {
        "mode": mode,
        "topic_key": topic_key,
        "count": len(rows),
        "items": runs_to_dicts(rows),
        "wechat_text": format_runs_for_wechat(rows),
    }


def payload_to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
