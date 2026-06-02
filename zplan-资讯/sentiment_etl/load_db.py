from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.models import FinancialAlert, GlobalNews, MarketSentiment, SessionLocal
from sentiment_etl.hashing import sha256_hex


def _ts_to_dt(val: object) -> datetime:
    if isinstance(val, datetime):
        return val
    ts = pd.Timestamp(val)
    return ts.to_pydatetime()


def load_financial_alerts_df(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        url = str(r.get("article_url", "") or "").strip()
        if not url:
            continue
        s = r.get("summary")
        if s is None or (isinstance(s, float) and pd.isna(s)):
            sumv = None
        else:
            sumv = str(s).strip() or None
        rows.append(
            {
                "url_hash": sha256_hex(url),
                "source": "eastmoney_flash",
                "published_at_utc": _ts_to_dt(r.get("published_at_utc")),
                "title": str(r.get("title", "") or "")[:20000],
                "summary": sumv,
                "article_url": url[:20000],
            }
        )
    if not rows:
        return 0
    with SessionLocal() as session:
        stmt = insert(FinancialAlert).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=[FinancialAlert.url_hash])
        res = session.execute(stmt)
        session.commit()
        return res.rowcount or 0


def load_market_sentiment_df(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "factor_kind": str(r.get("factor_kind", "") or "")[:48],
                "as_of_utc": _ts_to_dt(r.get("as_of_utc")),
                "subject": str(r.get("subject", "") or "")[:32],
                "metric_name": str(r.get("metric_name", "") or "")[:128],
                "metric_value": float(r["metric_value"])
                if r.get("metric_value") is not None and not pd.isna(r.get("metric_value"))
                else None,
                "extra_json": r.get("extra_json"),
            }
        )
    if not rows:
        return 0
    with SessionLocal() as session:
        stmt = insert(MarketSentiment).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                MarketSentiment.factor_kind,
                MarketSentiment.as_of_utc,
                MarketSentiment.subject,
                MarketSentiment.metric_name,
            ],
            set_={
                "metric_value": stmt.excluded.metric_value,
                "extra_json": stmt.excluded.extra_json,
            },
        )
        res = session.execute(stmt)
        session.commit()
        return res.rowcount or 0


def load_global_news_df(df: pd.DataFrame, channel: str) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        url = str(r.get("article_url", "") or "").strip()
        if not url:
            continue
        desc = r.get("description")
        desc_s = str(desc) if desc is not None and not (isinstance(desc, float) and pd.isna(desc)) else None
        rss_kw = r.get("rss_keyword")
        rss_s = str(rss_kw)[:128] if rss_kw is not None and str(rss_kw).strip() else None
        rows.append(
            {
                "url_hash": sha256_hex(url),
                "channel": channel[:32],
                "published_at_utc": _ts_to_dt(r.get("published_at_utc")),
                "source_name": str(r.get("source_name", "") or "")[:256],
                "title": str(r.get("title", "") or "")[:20000],
                "description": desc_s[:20000] if desc_s else None,
                "article_url": url[:20000],
                "rss_keyword": rss_s,
            }
        )
    if not rows:
        return 0
    with SessionLocal() as session:
        stmt = insert(GlobalNews).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=[GlobalNews.url_hash])
        res = session.execute(stmt)
        session.commit()
        return res.rowcount or 0
