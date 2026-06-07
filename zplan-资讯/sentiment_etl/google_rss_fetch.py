from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

import feedparser
import pandas as pd
import requests

from config import GOOGLE_RSS_HL, GOOGLE_RSS_WHEN, HTTP_USER_AGENT
from sentiment_etl.rss_title import display_source_name, split_aggregator_title

logger = logging.getLogger(__name__)

_GOOGLE_RSS_TMPL = "https://news.google.com/rss/search?q={q}&hl={hl}&gl=CN&ceid=CN:zh-Hans"


def _published_to_utc_naive(entry: Any) -> datetime:
    pp = getattr(entry, "published_parsed", None)
    if pp:
        return datetime(*pp[:6], tzinfo=timezone.utc).replace(tzinfo=None)
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if not raw:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        dt = parsedate_to_datetime(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).replace(tzinfo=None)


def fetch_google_news_rss_df(
    keywords: Iterable[str],
    hl: str | None = None,
    timeout_seconds: float = 25.0,
) -> pd.DataFrame:
    """
    Google News RSS（feedparser）-> 标准 DataFrame。
    列: published_at_utc, title, article_url, rss_keyword, source_name（从标题解析的媒体，如东方财富）

    通过 GOOGLE_RSS_WHEN 追加 Google News 时间过滤（如 ``when:24h``），
    减少跨窗口重复文章，提升 inserted/fetched 比。
    """
    hl_use = hl or GOOGLE_RSS_HL
    headers = {"User-Agent": HTTP_USER_AGENT}
    when_suffix = f" when:{GOOGLE_RSS_WHEN}" if GOOGLE_RSS_WHEN else ""
    rows: list[dict] = []
    for kw in keywords:
        q = str(kw).strip()
        if not q:
            continue
        q_with_when = f"{q}{when_suffix}"
        url = _GOOGLE_RSS_TMPL.format(q=urllib.parse.quote_plus(q_with_when), hl=urllib.parse.quote_plus(hl_use))
        try:
            resp = requests.get(url, headers=headers, timeout=timeout_seconds)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google RSS 拉取失败 keyword=%s: %s", q, exc)
            continue
        for entry in parsed.entries or []:
            title = (getattr(entry, "title", None) or "").strip()
            link = (getattr(entry, "link", None) or "").strip()
            if not title or not link:
                continue
            pub = _published_to_utc_naive(entry)
            clean_title, publisher = split_aggregator_title(title)
            rows.append(
                {
                    "published_at_utc": pub,
                    "title": clean_title or title,
                    "title_raw": title,
                    "article_url": link,
                    "rss_keyword": q[:120],
                    "source_name": display_source_name(title),
                }
            )
    return pd.DataFrame(rows)
