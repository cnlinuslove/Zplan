from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config import (
    NEWSAPI_KEY,
    SENTIMENT_WECHAT_SAMPLE_PER_SOURCE,
    SENTIMENT_WECHAT_PUSH,
    WECHAT_PUSH_WEBHOOK,
)
from sentiment_etl.timeutil import CN_TZ

logger = logging.getLogger(__name__)

_TEXT_LIMIT = 1800
_SOURCE_LABELS: dict[str, str] = {
    "em_financial_flash": "东方财富·全球快讯（AkShare / 东财）",
    "em_northbound_daily": "北向资金·日频（东财数据中心）",
    "em_northbound_intraday": "北向资金·分时（东财）",
    "em_margin_account": "融资融券·全市场账户（东财）",
    "em_index_turnover": "主要指数·日换手率（东财 index_zh_a_hist）",
    "newsapi": "NewsAPI（全球头条/关键词）",
    "google_rss": "Google News RSS（主题订阅）",
}


def _now_cn_str() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")


def _fmt_utc_as_cn(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "-"
    ts = pd.Timestamp(val)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    return ts.tz_convert(CN_TZ).strftime("%m-%d %H:%M")


def _inserted_line(label: str, inserted: dict[str, Any]) -> str:
    v = inserted.get(label)
    from_db = inserted.get("__from_db__")
    if isinstance(v, dict) and "error" in v:
        return f"本轮入库: 失败（{v['error'][:120]}）"
    if v is None:
        return "本轮入库: 未执行"
    if from_db:
        return f"库内累计: {v} 条（样例见下）"
    return f"本轮入库: +{v} 条"


def _section_divider() -> str:
    return "────────────────"


def _clip(text: str, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _section_flash(df: pd.DataFrame | None, inserted: dict[str, Any], n: int) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['em_financial_flash']}",
        _inserted_line("em_financial_flash", inserted),
    ]
    if df is None or df.empty:
        lines.append("样例: （无抓取数据）")
        return "\n".join(lines)
    sub = df.sort_values("published_at_utc", ascending=False).head(n)
    lines.append(f"样例（最新 {len(sub)} 条）:")
    for i, (_, r) in enumerate(sub.iterrows(), 1):
        lines.append(f"{i}. [{_fmt_utc_as_cn(r.get('published_at_utc'))}] {_clip(r.get('title'), 80)}")
        if r.get("summary"):
            lines.append(f"   {_clip(r.get('summary'), 100)}")
    return "\n".join(lines)


def _section_northbound_daily(df: pd.DataFrame | None, inserted: dict[str, Any]) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['em_northbound_daily']}",
        _inserted_line("em_northbound_daily", inserted),
    ]
    if df is None or df.empty:
        lines.append("样例: （无抓取数据）")
        return "\n".join(lines)
    primary = None
    for name in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
        sub = df[df["metric_name"] == name].dropna(subset=["metric_value"]).sort_values("as_of_utc")
        if not sub.empty:
            primary = (name, sub.iloc[-1])
            break
    if primary is None:
        lines.append("样例: 近期交易日指标均为空（东财未披露或尚未更新）")
        return "\n".join(lines)
    pname, last = primary
    d = _fmt_utc_as_cn(last.get("as_of_utc"))
    lines.append(f"最新交易日 {d} | {pname}: {last.get('metric_value')} 亿元（东财口径）")
    extras = []
    for name in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
        if name == pname:
            continue
        row = df[(df["as_of_utc"] == last["as_of_utc"]) & (df["metric_name"] == name)]
        if not row.empty and row.iloc[0]["metric_value"] is not None:
            extras.append(f"{name} {row.iloc[0]['metric_value']}")
    if extras:
        lines.append("   " + " | ".join(extras))
    return "\n".join(lines)


def _section_northbound_intraday(df: pd.DataFrame | None, inserted: dict[str, Any]) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['em_northbound_intraday']}",
        _inserted_line("em_northbound_intraday", inserted),
    ]
    if df is None or df.empty:
        lines.append("样例: （无抓取数据 / 或已关闭分时）")
        return "\n".join(lines)
    sub = df[df["metric_name"] == "北向资金"].sort_values("as_of_utc")
    if sub.empty:
        lines.append("样例: 无分时北向资金序列")
        return "\n".join(lines)
    tail = sub.tail(3)
    lines.append("样例（最近 3 个时点，单位万元）:")
    for _, r in tail.iterrows():
        lines.append(f"  · {_fmt_utc_as_cn(r['as_of_utc'])} 北向 {r['metric_value']}")
    return "\n".join(lines)


def _section_margin(df: pd.DataFrame | None, inserted: dict[str, Any]) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['em_margin_account']}",
        _inserted_line("em_margin_account", inserted),
    ]
    if df is None or df.empty:
        lines.append("样例: （无抓取数据）")
        return "\n".join(lines)
    for name in ("融资余额", "融券余额", "融资买入额"):
        sub = df[df["metric_name"] == name].sort_values("as_of_utc")
        if not sub.empty:
            r = sub.iloc[-1]
            lines.append(
                f"最新 {_fmt_utc_as_cn(r['as_of_utc'])} | {name}: {r['metric_value']} 亿（东财口径）"
            )
    return "\n".join(lines)


def _section_turnover(df: pd.DataFrame | None, inserted: dict[str, Any]) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['em_index_turnover']}",
        _inserted_line("em_index_turnover", inserted),
    ]
    if df is None or df.empty:
        lines.append("样例: 未抓到（常见原因: 网络/代理无法访问 push2.eastmoney.com）")
        return "\n".join(lines)
    lines.append("样例（各指数最近一日换手率 %）:")
    for sym in df["subject"].drop_duplicates():
        sub = df[(df["subject"] == sym) & (df["metric_name"] == "换手率")].sort_values("as_of_utc")
        if sub.empty:
            continue
        r = sub.iloc[-1]
        lines.append(f"  · 指数 {sym} | {_fmt_utc_as_cn(r['as_of_utc'])} | 换手率 {r['metric_value']}%")
    return "\n".join(lines)


def _section_newsapi(df: pd.DataFrame | None, inserted: dict[str, Any], n: int) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['newsapi']}",
        _inserted_line("newsapi", inserted),
    ]
    if not NEWSAPI_KEY:
        lines.append("状态: NEWSAPI_KEY 未配置，已跳过抓取")
        return "\n".join(lines)
    if df is None or df.empty:
        lines.append("样例: （本次无文章返回）")
        return "\n".join(lines)
    sub = df.sort_values("published_at_utc", ascending=False).head(n)
    lines.append(f"样例（最新 {len(sub)} 条）:")
    for i, (_, r) in enumerate(sub.iterrows(), 1):
        src = r.get("source_name") or "媒体"
        lines.append(
            f"{i}. [{_fmt_utc_as_cn(r.get('published_at_utc'))}] [{src}] {_clip(r.get('title'), 70)}"
        )
        if r.get("description"):
            lines.append(f"   {_clip(r.get('description'), 90)}")
    return "\n".join(lines)


def _section_google_rss(df: pd.DataFrame | None, inserted: dict[str, Any], n: int) -> str:
    lines = [
        _section_divider(),
        f"【来源】{_SOURCE_LABELS['google_rss']}",
        _inserted_line("google_rss", inserted),
    ]
    if df is None or df.empty:
        lines.append("样例: （无抓取数据）")
        return "\n".join(lines)
    sub = df.sort_values("published_at_utc", ascending=False).head(n)
    lines.append(f"样例（最新 {len(sub)} 条）:")
    for i, (_, r) in enumerate(sub.iterrows(), 1):
        kw = r.get("rss_keyword") or "订阅"
        lines.append(
            f"{i}. [{_fmt_utc_as_cn(r.get('published_at_utc'))}] [关键词:{kw}] {_clip(r.get('title'), 75)}"
        )
    return "\n".join(lines)


def build_etl_digest_messages(stats: dict[str, Any]) -> list[str]:
    """按来源生成纯文本，必要时拆成多条（企业微信单条上限 1800 字）。"""
    inserted = dict(stats.get("inserted") or {})
    fetched: dict[str, Any] = stats.get("fetched") or {}
    from_db = bool(stats.get("from_db"))
    if from_db:
        inserted["__from_db__"] = True
    n = max(1, SENTIMENT_WECHAT_SAMPLE_PER_SOURCE)

    header = (
        f"【Z-Plan 多源资讯 ETL 样例】\n"
        f"推送时间（北京时间）: {_now_cn_str()}\n"
        f"说明: 以下为各数据源本轮抓取样例，便于你调整版式；时间均为北京时间。"
    )

    sections = [
        _section_flash(fetched.get("em_financial_flash"), inserted, n),
        _section_northbound_daily(fetched.get("em_northbound_daily"), inserted),
        _section_northbound_intraday(fetched.get("em_northbound_intraday"), inserted),
        _section_margin(fetched.get("em_margin_account"), inserted),
        _section_turnover(fetched.get("em_index_turnover"), inserted),
        _section_newsapi(fetched.get("newsapi"), inserted, n),
        _section_google_rss(fetched.get("google_rss"), inserted, n),
    ]

    chunks: list[str] = []
    current = header
    for sec in sections:
        candidate = current + "\n" + sec
        if len(candidate) <= _TEXT_LIMIT:
            current = candidate
        else:
            if current.strip():
                chunks.append(current)
            current = f"【Z-Plan 多源资讯 ETL 样例 · 续】\n{sec}"
            if len(current) > _TEXT_LIMIT:
                chunks.append(current[:_TEXT_LIMIT])
                current = ""
    if current.strip():
        chunks.append(current)

    for i, body in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunks[i - 1] = f"[{i}/{len(chunks)}]\n{body}"
    return chunks


def _stats_from_db() -> dict[str, Any]:
    """从 SQLite 读取各源最新样例，用于仅推送（无需重跑 ETL）。"""
    import pandas as pd
    from sqlalchemy import text

    from db_engine import build_engine
    engine = build_engine()
    stats: dict[str, Any] = {"inserted": {}, "fetched": {}}

    with engine.connect() as conn:
        fa = pd.read_sql(
            text(
                "SELECT published_at_utc, title, summary, article_url FROM financial_alerts "
                "ORDER BY published_at_utc DESC LIMIT :n"
            ),
            conn,
            params={"n": SENTIMENT_WECHAT_SAMPLE_PER_SOURCE * 2},
        )
        stats["fetched"]["em_financial_flash"] = fa
        stats["inserted"]["em_financial_flash"] = conn.execute(
            text("SELECT COUNT(*) FROM financial_alerts")
        ).scalar()

        for kind, key in (
            ("northbound_daily", "em_northbound_daily"),
            ("northbound_intraday", "em_northbound_intraday"),
            ("margin_account", "em_margin_account"),
            ("index_turnover", "em_index_turnover"),
        ):
            ms = pd.read_sql(
                text(
                    "SELECT factor_kind, as_of_utc, subject, metric_name, metric_value "
                    "FROM market_sentiment WHERE factor_kind = :k "
                    "ORDER BY as_of_utc DESC LIMIT 2000"
                ),
                conn,
                params={"k": kind},
            )
            stats["fetched"][key] = ms
            stats["inserted"][key] = len(ms)

        gn_api = pd.read_sql(
            text(
                "SELECT published_at_utc, source_name, title, description, article_url "
                "FROM global_news WHERE channel = 'newsapi' "
                "ORDER BY published_at_utc DESC LIMIT :n"
            ),
            conn,
            params={"n": SENTIMENT_WECHAT_SAMPLE_PER_SOURCE * 2},
        )
        stats["fetched"]["newsapi"] = gn_api
        stats["inserted"]["newsapi"] = conn.execute(
            text("SELECT COUNT(*) FROM global_news WHERE channel = 'newsapi'")
        ).scalar()

        gn_rss = pd.read_sql(
            text(
                "SELECT published_at_utc, title, article_url, rss_keyword "
                "FROM global_news WHERE channel = 'google_rss' "
                "ORDER BY published_at_utc DESC LIMIT :n"
            ),
            conn,
            params={"n": SENTIMENT_WECHAT_SAMPLE_PER_SOURCE * 2},
        )
        stats["fetched"]["google_rss"] = gn_rss
        stats["inserted"]["google_rss"] = conn.execute(
            text("SELECT COUNT(*) FROM global_news WHERE channel = 'google_rss'")
        ).scalar()

    return stats


def push_etl_digest_to_wechat(stats: dict[str, Any]) -> dict[str, Any]:
    if not SENTIMENT_WECHAT_PUSH:
        return {"pushed": False, "reason": "SENTIMENT_WECHAT_PUSH=false"}
    if not WECHAT_PUSH_WEBHOOK:
        return {"pushed": False, "reason": "未配置 WECHAT_PUSH_WEBHOOK"}

    from wechat_push import push_wechat_text

    messages = build_etl_digest_messages(stats)
    ok_count = 0
    for msg in messages:
        if push_wechat_text(msg):
            ok_count += 1
    result = {"pushed": ok_count > 0, "parts": len(messages), "parts_ok": ok_count}
    logger.info("ETL 微信 digest: %s", result)
    return result


def push_digest_from_db() -> dict[str, Any]:
    """仅根据库内最新数据组稿并推送（跳过重抓）。"""
    stats = _stats_from_db()
    stats["from_db"] = True
    return push_etl_digest_to_wechat(stats)
