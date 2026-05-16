from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import requests

from config import (
    HTTP_USER_AGENT,
    NEWSAPI_BASE_URL,
    NEWSAPI_KEY,
    NEWSAPI_LANGUAGE,
    NEWSAPI_MODE,
    NEWSAPI_PAGE_SIZE,
    NEWSAPI_QUERY,
    NEWSAPI_TIMEOUT_SECONDS,
    NEWSAPI_TOP_HEADLINES_COUNTRY,
)
from sentiment_etl.hashing import sha256_hex
from sentiment_etl.timeutil import parse_published_to_utc_naive

logger = logging.getLogger(__name__)


def fetch_newsapi_articles_df(*, query: str | None = None) -> pd.DataFrame:
    """
    NewsAPI JSON -> 标准 DataFrame。
    列: published_at_utc, source_name, title, description, article_url

    query: 现场检索时传入；将使用 everything 端点按关键词搜索。
    """
    if not NEWSAPI_KEY:
        logger.info("NEWSAPI_KEY 未配置，跳过 NewsAPI。")
        return pd.DataFrame(
            columns=["published_at_utc", "source_name", "title", "description", "article_url"]
        )
    q = (query or "").strip()
    if q:
        mode = "everything"
    else:
        mode = NEWSAPI_MODE if NEWSAPI_MODE in ("top_headlines", "everything") else "top_headlines"
    url = f"{NEWSAPI_BASE_URL}/{mode}"
    params: dict[str, Any] = {"apiKey": NEWSAPI_KEY, "pageSize": min(max(NEWSAPI_PAGE_SIZE, 1), 100)}
    if mode == "top_headlines":
        params["country"] = NEWSAPI_TOP_HEADLINES_COUNTRY
    else:
        params["q"] = q or NEWSAPI_QUERY
        params["language"] = NEWSAPI_LANGUAGE
        params["sortBy"] = "publishedAt"

    headers = {"User-Agent": HTTP_USER_AGENT}
    resp = requests.get(url, params=params, headers=headers, timeout=NEWSAPI_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {data.get('message') or data}")

    articles = data.get("articles") or []
    rows = []
    for a in articles:
        src = a.get("source") or {}
        src_name = src.get("name") if isinstance(src, dict) else str(src)
        title = (a.get("title") or "").strip()
        desc = (a.get("description") or "").strip() or None
        link = (a.get("url") or "").strip()
        pub = parse_published_to_utc_naive(a.get("publishedAt"), assume_tz="UTC")
        if not title:
            continue
        if not link:
            link = f"newsapi://digest/{sha256_hex(title + str(pub))}"
        rows.append(
            {
                "published_at_utc": pub,
                "source_name": str(src_name or "")[:250],
                "title": title,
                "description": desc,
                "article_url": link,
            }
        )
    return pd.DataFrame(rows)
