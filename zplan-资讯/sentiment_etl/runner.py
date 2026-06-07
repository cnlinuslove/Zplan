from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from config import (
    GOOGLE_RSS_KEYWORDS,
    SENTIMENT_STALE_DAYS_INDEX,
    SENTIMENT_STALE_DAYS_MARGIN,
    SENTIMENT_STALE_DAYS_NORTHBOUND,
)
from zplan_shared.models import init_db
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

    try:
        import os

        from zplan_shared.news_linker import link_recent_news, news_link_coverage_stats

        relink = os.getenv("DAILY_NEWS_LINK_RELINK", "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        link_hours = int(os.getenv("DAILY_NEWS_LINK_HOURS", "168") or "168")
        link_limit = int(os.getenv("DAILY_NEWS_LINK_LIMIT", "800") or "800")
        stats["news_link"] = link_recent_news(
            hours=link_hours,
            limit_per_table=link_limit,
            relink=relink,
        )
        stats["coverage_48h"] = news_link_coverage_stats(hours=48)
    except Exception as exc:  # noqa: BLE001
        logger.warning("news_stock_link 补链失败: %s", exc)
        stats["news_link"] = {"error": str(exc)}

    stats["alerts"] = _collect_etl_alerts(stats)
    if stats["alerts"]:
        for msg in stats["alerts"]:
            logger.warning("[ETL告警] %s", msg)

    logger.info("sentiment_etl 完成: %s", stats.get("inserted"))

    if push_wechat:
        stats["wechat"] = push_etl_digest_to_wechat(stats)

    return stats


def _check_data_staleness() -> list[str]:
    """检查 market_sentiment 各 factor_kind 数据陈旧度（factor 级 + 指标级）。

    - factor 级：最新 as_of_utc 是否超过阈值
    - 指标级：关键指标（如北向资金当日成交净买额）最新非 NaN 日期是否远落后于
      factor 最新日期（说明 API 返回了日期但关键字段全是 NaN）
    """
    from datetime import datetime, timezone

    import pandas as pd
    from sqlalchemy import text

    from zplan_shared.models import SessionLocal

    thresholds: dict[str, int] = {
        "northbound_daily": SENTIMENT_STALE_DAYS_NORTHBOUND,
        "northbound_intraday": SENTIMENT_STALE_DAYS_NORTHBOUND,
        "margin_account": SENTIMENT_STALE_DAYS_MARGIN,
        "index_turnover": SENTIMENT_STALE_DAYS_INDEX,
    }
    # 关键指标：如果这些指标的最新非 NaN 日期与 factor 最新日期差距超过此天数，说明 API 已停更
    _critical_metrics: dict[str, list[str]] = {
        "northbound_daily": ["当日成交净买额", "当日资金流入", "买入成交额"],
        "margin_account": ["融资余额", "融资买入额"],
    }
    _metric_stale_days = 10  # 指标级陈旧阈值（与 factor 最新日期的差距）

    now = datetime.now(timezone.utc)
    alerts: list[str] = []

    try:
        with SessionLocal() as session:
            for factor_kind, max_stale_days in thresholds.items():
                # ── factor 级：最新 as_of_utc ──
                row = session.execute(
                    text(
                        "SELECT MAX(as_of_utc) FROM market_sentiment WHERE factor_kind = :k"
                    ),
                    {"k": factor_kind},
                ).fetchone()
                if not row or not row[0]:
                    continue
                latest = pd.Timestamp(row[0])
                if latest.tzinfo is None:
                    latest = latest.tz_localize(timezone.utc)
                else:
                    latest = latest.tz_convert(timezone.utc)
                age_days = (now - latest).total_seconds() / 86400.0
                if age_days > max_stale_days:
                    alerts.append(
                        f"{factor_kind}: 最新数据 {latest.strftime('%Y-%m-%d')} "
                        f"距今 {age_days:.0f} 天 > {max_stale_days} 天（阈值）"
                    )

                # ── 指标级：关键指标最新非 NaN 日期 ──
                for metric_name in _critical_metrics.get(factor_kind, []):
                    mrow = session.execute(
                        text(
                            "SELECT MAX(as_of_utc) FROM market_sentiment "
                            "WHERE factor_kind = :k AND metric_name = :m "
                            "AND metric_value IS NOT NULL"
                        ),
                        {"k": factor_kind, "m": metric_name},
                    ).fetchone()
                    if not mrow or not mrow[0]:
                        alerts.append(
                            f"{factor_kind}/{metric_name}: 全库无有效值，API 可能已停更该字段"
                        )
                        continue
                    mlatest = pd.Timestamp(mrow[0])
                    if mlatest.tzinfo is None:
                        mlatest = mlatest.tz_localize(timezone.utc)
                    else:
                        mlatest = mlatest.tz_convert(timezone.utc)
                    gap_days = (latest - mlatest).total_seconds() / 86400.0
                    if gap_days > _metric_stale_days:
                        alerts.append(
                            f"{factor_kind}/{metric_name}: 最新有效值 {mlatest.strftime('%Y-%m-%d')}"
                            f"，比 factor 最新日期落后 {gap_days:.0f} 天（API 可能已停更）"
                        )
    except Exception as exc:
        logger.warning("数据陈旧度检查失败: %s", exc)

    return alerts


def _collect_etl_alerts(stats: dict[str, Any]) -> list[str]:
    """监控 inserted=0、拉取失败、数据陈旧（P0 运维）。"""
    alerts: list[str] = []
    inserted = stats.get("inserted") or {}
    fetched = stats.get("fetched") or {}
    skip_zero_alert = {"newsapi"}

    for label, val in inserted.items():
        if isinstance(val, dict) and val.get("error"):
            err_msg = str(val["error"])[:160]
            # 区分 SSL 错误（代理/网络问题）vs 其他
            if "SSL" in err_msg or "SSLEOF" in err_msg:
                alerts.append(f"🔴 {label}: SSL/代理错误，源可能不可达")
            else:
                alerts.append(f"🔴 {label}: {err_msg}")
            continue
        if label in skip_zero_alert:
            continue
        n_ins = int(val) if isinstance(val, int) else 0
        raw = fetched.get(label)
        n_fetch = len(raw) if isinstance(raw, pd.DataFrame) else 0
        if isinstance(raw, Exception):
            alerts.append(f"🔴 {label}: fetch failed")
            continue
        if n_fetch > 0 and n_ins == 0:
            # 区分：拉取成功但全重复（数据源正常，去重生效）
            alerts.append(f"🟡 {label}: fetched={n_fetch} inserted=0（可能全重复）")
        elif n_fetch == 0 and n_ins == 0:
            alerts.append(f"🔴 {label}: fetched=0 inserted=0（源无新数据或拉取失败）")

    # 数据陈旧度检查
    staleness = _check_data_staleness()
    if staleness:
        for msg in staleness:
            alerts.append(f"🔴 数据陈旧: {msg}")

    cov = stats.get("coverage_48h") or {}
    if isinstance(cov, dict):
        for key in ("financial_alerts_coverage_pct", "global_news_coverage_pct"):
            pct = cov.get(key)
            if (
                isinstance(pct, (int, float))
                and pct < 5.0
                and cov.get(key.replace("_coverage_pct", "_total"), 0) > 10
            ):
                alerts.append(
                    f"🟡 48h {key}={pct}% 过低，请检查 news_stock_link / 简称词典"
                )
    return alerts


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
