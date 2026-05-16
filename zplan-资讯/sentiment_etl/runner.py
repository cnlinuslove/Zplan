from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from config import GOOGLE_RSS_KEYWORDS
from models import init_db
from sentiment_etl.akshare_em import (
    fetch_em_financial_flash_df,
    fetch_em_index_turnover_df,
    fetch_em_margin_market_factors_df,
    fetch_em_northbound_daily_factors_df,
    fetch_em_northbound_intraday_factors_df,
)
from sentiment_etl.google_rss_fetch import fetch_google_news_rss_df
from sentiment_etl.load_db import (
    load_financial_alerts_df,
    load_global_news_df,
    load_market_sentiment_df,
)
from sentiment_etl.newsapi_fetch import fetch_newsapi_articles_df
from sentiment_etl.wechat_digest import push_etl_digest_to_wechat

logger = logging.getLogger(__name__)


def _safe_fetch(label: str, fn: Callable[[], pd.DataFrame]) -> tuple[str, pd.DataFrame | Exception]:
    try:
        return label, fn()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] 失败: %s", label, exc)
        return label, exc


def _load_flash(df: pd.DataFrame) -> int:
    return load_financial_alerts_df(df)


def _load_sentiment(df: pd.DataFrame) -> int:
    return load_market_sentiment_df(df)


def run_sentiment_etl(*, push_wechat: bool = True) -> dict[str, Any]:
    """
    串联 Extract -> Transform(DataFrame) -> Load(SQLite)。
    各数据源互不影响；可选推送各源样例到企业微信（纯文本）。
    """
    init_db()
    stats: dict[str, Any] = {"inserted": {}, "fetched": {}}

    pipelines: list[tuple[str, Callable[[], pd.DataFrame], Callable[[pd.DataFrame], int]]] = [
        ("em_financial_flash", fetch_em_financial_flash_df, _load_flash),
        ("em_northbound_daily", fetch_em_northbound_daily_factors_df, _load_sentiment),
        ("em_northbound_intraday", fetch_em_northbound_intraday_factors_df, _load_sentiment),
        ("em_margin_account", fetch_em_margin_market_factors_df, _load_sentiment),
        ("em_index_turnover", fetch_em_index_turnover_df, _load_sentiment),
        ("newsapi", fetch_newsapi_articles_df, lambda df: load_global_news_df(df, channel="newsapi")),
    ]

    for label, fetch_fn, load_fn in pipelines:
        _, res = _safe_fetch(label, fetch_fn)
        if isinstance(res, Exception):
            stats["inserted"][label] = {"error": str(res)}
            stats["fetched"][label] = None
            continue
        stats["fetched"][label] = res
        if res.empty and label == "newsapi":
            stats["inserted"][label] = 0
        else:
            stats["inserted"][label] = load_fn(res)

    kws = [x.strip() for x in GOOGLE_RSS_KEYWORDS.split(",") if x.strip()]
    label = "google_rss"
    _, res = _safe_fetch(label, lambda: fetch_google_news_rss_df(kws))
    if isinstance(res, Exception):
        stats["inserted"][label] = {"error": str(res)}
        stats["fetched"][label] = None
    else:
        stats["fetched"][label] = res
        if res.empty:
            stats["inserted"][label] = 0
        else:
            load_df = res.copy()
            if "source_name" not in load_df.columns or load_df["source_name"].isna().all():
                from sentiment_etl.rss_title import display_source_name

                load_df["source_name"] = load_df["title"].map(
                    lambda t: display_source_name(str(t or ""))
                )
            load_df["description"] = None
            stats["inserted"][label] = load_global_news_df(load_df, channel="google_rss")

    logger.info("sentiment_etl 完成: %s", stats.get("inserted"))

    if push_wechat:
        stats["wechat"] = push_etl_digest_to_wechat(stats)

    return stats


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if len(sys.argv) > 1 and sys.argv[1] == "--push-only":
        from sentiment_etl.wechat_digest import push_digest_from_db

        print(push_digest_from_db())
    else:
        print(run_sentiment_etl())
