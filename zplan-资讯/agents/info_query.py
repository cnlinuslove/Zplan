from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from config import (
    GEMINI_API_KEY,
    INFO_QUERY_LIVE_FETCH,
    INFO_QUERY_LIVE_MAX_KEYWORDS,
    INFO_QUERY_MAX_SOURCES,
    INFO_QUERY_SNIPPET_CHARS,
    LLM_SUMMARY_ENABLED,
    NEWSAPI_KEY,
)
from db_engine import build_engine
from models import init_db

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
_STOPWORDS = frozenset(
    {
        "什么",
        "怎么",
        "如何",
        "为什么",
        "哪些",
        "哪个",
        "请问",
        "帮我",
        "查一下",
        "查询",
        "搜索",
        "相关",
        "资讯",
        "新闻",
        "消息",
        "最新",
        "最近",
        "最近怎样",
        "最近怎么样",
        "怎样",
        "怎么样",
        "如何样",
        "情况",
        "走势",
        "表现",
        "今天",
        "昨天",
        "了吗",
        "呢",
        "the",
        "and",
        "for",
        "what",
        "how",
        "about",
        "news",
    }
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")
_EN_RE = re.compile(r"[a-zA-Z]{3,}")
_QUESTION_TAIL_RE = re.compile(
    r"(最近怎么样|最近怎样|怎么样|怎样|如何|什么情况|什么状况|走势如何|表现如何|近况).*$"
)
# 问题里出现下列词时，自动补充检索词（便于命中快讯/RSS）
_TOPIC_SEARCH_ALIASES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("北向", "沪股通", "深股通", "外资"), ("北向", "北向资金", "沪深港通", "沪股通")),
    (("两融", "融资融券"), ("两融", "融资融券", "融资余额")),
    (("原油", "石油", "油价"), ("原油", "石油", "OPEC")),
    (("美联储", "加息", "降息", "鲍威尔"), ("美联储", "加息", "利率")),
    (("地缘", "冲突", "战争"), ("地缘", "冲突", "中东")),
]


@dataclass
class InfoHit:
    source_label: str
    published_at_utc: datetime
    title: str
    snippet: str | None
    url: str | None
    score: int


def extract_keywords(question: str, max_kw: int = 6) -> list[str]:
    q = (question or "").strip()
    q = _QUESTION_TAIL_RE.sub("", q).strip() or q
    found: list[str] = []
    for triggers, aliases in _TOPIC_SEARCH_ALIASES:
        if any(t in question for t in triggers):
            found.extend(aliases)
    for m in _CJK_RE.finditer(q):
        w = m.group(0)
        if w not in _STOPWORDS and len(w) >= 2:
            found.append(w)
    for m in _EN_RE.finditer(q):
        w = m.group(0).lower()
        if w not in _STOPWORDS:
            found.append(w)
    if not found and len(q) >= 2:
        found = [q[:16]]
    out: list[str] = []
    seen: set[str] = set()
    for w in found:
        if w not in seen and w not in _STOPWORDS:
            seen.add(w)
            out.append(w)
        if len(out) >= max_kw:
            break
    return out


def _fmt_num(val: Any, digits: int = 2) -> str:
    try:
        return f"{float(val):.{digits}f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_cn_time(dt: datetime | Any) -> str:
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    return ts.tz_convert(CN_TZ).strftime("%m-%d %H:%M")


def _clip(s: str, n: int) -> str:
    t = " ".join((s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _score_text(text: str, keywords: list[str]) -> int:
    low = text.lower()
    return sum(2 if kw in text or kw.lower() in low else 0 for kw in keywords)


def _search_financial_alerts(conn: Any, keywords: list[str], limit: int) -> list[InfoHit]:
    if not keywords:
        return []
    clauses = []
    params: dict[str, Any] = {"lim": limit * 3}
    for i, kw in enumerate(keywords):
        key = f"k{i}"
        params[key] = f"%{kw}%"
        clauses.append(f"(title LIKE :{key} OR summary LIKE :{key})")
    where = " OR ".join(clauses)
    sql = text(
        f"SELECT published_at_utc, title, summary, article_url FROM financial_alerts "
        f"WHERE {where} ORDER BY published_at_utc DESC LIMIT :lim"
    )
    rows = conn.execute(sql, params).mappings().all()
    hits: list[InfoHit] = []
    for r in rows:
        title = str(r["title"] or "")
        summary = str(r["summary"] or "") if r["summary"] else ""
        blob = title + summary
        sc = _score_text(blob, keywords)
        if sc <= 0:
            continue
        hits.append(
            InfoHit(
                source_label="东方财富·快讯",
                published_at_utc=r["published_at_utc"],
                title=title,
                snippet=summary or None,
                url=str(r["article_url"]) if r["article_url"] else None,
                score=sc,
            )
        )
    return hits


def _search_global_news(conn: Any, keywords: list[str], limit: int) -> list[InfoHit]:
    if not keywords:
        return []
    clauses = []
    params: dict[str, Any] = {"lim": limit * 3}
    for i, kw in enumerate(keywords):
        key = f"k{i}"
        params[key] = f"%{kw}%"
        clauses.append(
            f"(title LIKE :{key} OR description LIKE :{key} OR rss_keyword LIKE :{key})"
        )
    where = " OR ".join(clauses)
    sql = text(
        f"SELECT channel, source_name, published_at_utc, title, description, article_url, rss_keyword "
        f"FROM global_news WHERE {where} ORDER BY published_at_utc DESC LIMIT :lim"
    )
    rows = conn.execute(sql, params).mappings().all()
    hits: list[InfoHit] = []
    channel_label = {
        "newsapi": "NewsAPI",
        "google_rss": "Google News RSS",
    }
    for r in rows:
        title = str(r["title"] or "")
        desc = str(r["description"] or "") if r["description"] else ""
        blob = title + desc + str(r.get("rss_keyword") or "")
        sc = _score_text(blob, keywords)
        if sc <= 0:
            continue
        ch = str(r["channel"] or "")
        media = str(r["source_name"] or "").strip()
        title_raw = title
        from sentiment_etl.rss_title import display_source_name, split_aggregator_title

        if ch == "google_rss":
            label = display_source_name(title_raw, media)
            clean, _pub = split_aggregator_title(title_raw)
            if clean:
                title = clean
        else:
            src = channel_label.get(ch, ch or "海外资讯")
            label = media if media else src
        hits.append(
            InfoHit(
                source_label=label,
                published_at_utc=r["published_at_utc"],
                title=title,
                snippet=desc or None,
                url=str(r["article_url"]) if r["article_url"] else None,
                score=sc,
            )
        )
    return hits


def _latest_factor_rows(
    conn: Any,
    *,
    factor_kind: str,
    metric_names: tuple[str, ...],
) -> list[Any]:
    """各指标取各自最新非空一条（避免「最新交易日」仅有部分字段）。"""
    rows: list[Any] = []
    for name in metric_names:
        r = conn.execute(
            text(
                "SELECT metric_name, metric_value, as_of_utc FROM market_sentiment "
                "WHERE factor_kind = :k AND metric_name = :m AND metric_value IS NOT NULL "
                "ORDER BY as_of_utc DESC LIMIT 1"
            ),
            {"k": factor_kind, "m": name},
        ).mappings().first()
        if r:
            rows.append(r)
    return rows


def _live_northbound_snippet() -> str | None:
    """现场拉东财北向日频（避免本地库指标过旧）。"""
    try:
        from sentiment_etl.akshare_em import fetch_em_northbound_daily_factors_df

        df = fetch_em_northbound_daily_factors_df(max_rows=30)
        if df is None or df.empty:
            return None
        for name in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
            sub = df[df["metric_name"] == name].dropna(subset=["metric_value"]).sort_values("as_of_utc")
            if not sub.empty:
                lines = ["【数据·北向资金·东财·现场】"]
                latest_day = sub["as_of_utc"].max()
                lines.append(f"指标最新截至 {_fmt_cn_time(latest_day)}")
                age_days = (pd.Timestamp.now(tz=CN_TZ) - pd.Timestamp(latest_day).tz_localize("UTC").tz_convert(CN_TZ)).days
                if age_days > 10:
                    lines.append("（提示：日频数据较旧，请结合下方新闻来源中较新日期条目）")
                for n in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
                    s = df[df["metric_name"] == n].dropna(subset=["metric_value"]).sort_values("as_of_utc")
                    if s.empty:
                        continue
                    r = s.iloc[-1]
                    lines.append(
                        f"  · {n}: {_fmt_num(r['metric_value'])} 亿元 ({_fmt_cn_time(r['as_of_utc'])})"
                    )
                return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("现场北向数据失败: %s", exc)
    return None


def _sentiment_snapshot(conn: Any, question: str, *, prefer_live: bool = False) -> str | None:
    """问题含情绪/资金关键词时，附最新因子快照。"""
    q = question
    lines: list[str] = []
    if any(k in q for k in ("北向", "外资", "沪股通", "深股通")):
        if prefer_live:
            live = _live_northbound_snippet()
            if live:
                lines.append(live)
                return "\n".join(lines) if lines else None
        metrics = ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额")
        row = _latest_factor_rows(conn, factor_kind="northbound_daily", metric_names=metrics)
        if row:
            latest_day = max(r["as_of_utc"] for r in row)
            lines.append(f"【数据·北向资金·东财】指标最新截至 {_fmt_cn_time(latest_day)}")
            order = {n: i for i, n in enumerate(metrics)}
            row = sorted(row, key=lambda r: order.get(r["metric_name"], 99))
            for r in row:
                day_note = _fmt_cn_time(r["as_of_utc"])
                lines.append(
                    f"  · {r['metric_name']}: {_fmt_num(r['metric_value'])} 亿元 ({day_note})"
                )
        else:
            lines.append("【数据·北向资金·东财】本地暂无日频数据，请先运行: python3 -m sentiment_etl.runner")
    if any(k in q for k in ("两融", "融资", "融券", "杠杆")):
        metrics = ("融资余额", "融券余额", "融资买入额")
        row = _latest_factor_rows(conn, factor_kind="margin_account", metric_names=metrics)
        if row:
            day = _fmt_cn_time(row[0]["as_of_utc"])
            lines.append(f"【数据·两融账户·东财】最新交易日 {day}")
            for r in row:
                lines.append(f"  · {r['metric_name']}: {_fmt_num(r['metric_value'])} 亿")
    if any(k in q for k in ("换手率", "上证", "深证", "创业板", "指数")):
        row = conn.execute(
            text(
                "SELECT subject, metric_value, as_of_utc FROM market_sentiment "
                "WHERE factor_kind = 'index_turnover' AND metric_name = '换手率' "
                "ORDER BY as_of_utc DESC LIMIT 3"
            )
        ).mappings().all()
        if row:
            lines.append("【数据·指数换手率·东财】")
            for r in row:
                lines.append(
                    f"  指数{r['subject']} {_fmt_cn_time(r['as_of_utc'])} 换手率 {r['metric_value']}%"
                )
    return "\n".join(lines) if lines else None


def _df_row_to_hits(
    df: pd.DataFrame,
    *,
    source_label: str,
    keywords: list[str],
    title_col: str = "title",
    snippet_col: str | None = "summary",
    url_col: str = "article_url",
    time_col: str = "published_at_utc",
    live_bonus: int = 50,
) -> list[InfoHit]:
    hits: list[InfoHit] = []
    if df is None or df.empty:
        return hits
    for _, r in df.iterrows():
        title = str(r.get(title_col, "") or "").strip()
        if not title:
            continue
        snip = None
        if snippet_col and r.get(snippet_col) is not None:
            snip = str(r.get(snippet_col) or "").strip() or None
        desc_col = snippet_col if snippet_col in df.columns else None
        if desc_col is None and "description" in df.columns:
            snip = str(r.get("description") or "").strip() or None
        blob = title + (snip or "")
        sc = _score_text(blob, keywords) + live_bonus
        if keywords and sc <= live_bonus:
            continue
        pub = r.get(time_col)
        if pub is None or (isinstance(pub, float) and pd.isna(pub)):
            pub = datetime.now(timezone.utc).replace(tzinfo=None)
        hits.append(
            InfoHit(
                source_label=source_label,
                published_at_utc=pub,
                title=title,
                snippet=snip,
                url=str(r[url_col]) if url_col in df.columns and r.get(url_col) else None,
                score=sc,
            )
        )
    return hits


def fetch_live_hits(question: str, keywords: list[str], limit: int = 12) -> tuple[list[InfoHit], list[str]]:
    """
    按问题关键词现场拉取（不依赖本地库是否已有）。
    返回 (hits, status_lines)。
    """
    status: list[str] = []
    hits: list[InfoHit] = []
    kws = keywords[: max(1, INFO_QUERY_LIVE_MAX_KEYWORDS)]

    # Google News RSS：每个关键词一次订阅
    try:
        from sentiment_etl.google_rss_fetch import fetch_google_news_rss_df

        rss_df = fetch_google_news_rss_df(kws)
        if rss_df is None or rss_df.empty:
            status.append("Google RSS: 0 条")
        else:
            status.append(f"Google RSS: {len(rss_df)} 条")
            from sentiment_etl.rss_title import display_source_name

            for _, r in rss_df.iterrows():
                kw = str(r.get("rss_keyword") or "")
                title = str(r.get("title", "") or "")
                publisher = display_source_name(
                    str(r.get("title_raw") or title),
                    str(r.get("source_name") or ""),
                )
                label = publisher
                blob = title + publisher + kw
                sc = _score_text(blob, keywords) + 50
                if keywords and sc <= 50:
                    continue
                hits.append(
                    InfoHit(
                        source_label=label,
                        published_at_utc=r.get("published_at_utc"),
                        title=title,
                        snippet=None,
                        url=str(r.get("article_url") or "") or None,
                        score=sc,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("现场 Google RSS 失败: %s", exc)
        status.append(f"Google RSS: 失败")

    # NewsAPI：用关键词 OR 拼接
    try:
        from sentiment_etl.newsapi_fetch import fetch_newsapi_articles_df

        if NEWSAPI_KEY:
            q = " OR ".join(kws) if kws else question[:80]
            api_df = fetch_newsapi_articles_df(query=q)
            n = len(api_df) if api_df is not None and not api_df.empty else 0
            status.append(f"NewsAPI: {n} 条")
            if api_df is not None and not api_df.empty:
                for _, r in api_df.iterrows():
                    media = str(r.get("source_name") or "").strip()
                    label = "NewsAPI·现场" + (f"·{media}" if media else "")
                    title = str(r.get("title", "") or "")
                    desc = str(r.get("description") or "").strip() or None
                    sc = _score_text(title + (desc or ""), keywords) + 50
                    if keywords and sc <= 50:
                        continue
                    hits.append(
                        InfoHit(
                            source_label=label,
                            published_at_utc=r.get("published_at_utc"),
                            title=title,
                            snippet=desc,
                            url=str(r.get("article_url") or "") or None,
                            score=sc,
                        )
                    )
        else:
            status.append("NewsAPI: 未配置 Key")
    except Exception as exc:  # noqa: BLE001
        logger.warning("现场 NewsAPI 失败: %s", exc)
        status.append("NewsAPI: 失败")

    # 东财快讯：拉最新一批再按关键词过滤
    try:
        from sentiment_etl.akshare_em import fetch_em_financial_flash_df

        flash_df = fetch_em_financial_flash_df()
        n_raw = len(flash_df) if flash_df is not None and not flash_df.empty else 0
        flash_hits = _df_row_to_hits(
            flash_df,
            source_label="东方财富·快讯·现场",
            keywords=keywords,
            snippet_col="summary",
            live_bonus=50,
        )
        status.append(f"东财快讯: 拉取 {n_raw} 条，命中 {len(flash_hits)} 条")
        hits.extend(flash_hits)
    except Exception as exc:  # noqa: BLE001
        logger.warning("现场东财快讯失败: %s", exc)
        status.append("东财快讯: 失败")

    hits.sort(key=lambda h: (h.score, h.published_at_utc), reverse=True)
    seen: set[str] = set()
    deduped: list[InfoHit] = []
    for h in hits:
        key = _clip(h.title, 80)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
        if len(deduped) >= limit:
            break
    return deduped, status


def _merge_hits(local: list[InfoHit], live: list[InfoHit], limit: int) -> list[InfoHit]:
    merged = local + live
    merged.sort(key=lambda h: (h.score, h.published_at_utc), reverse=True)
    seen: set[str] = set()
    out: list[InfoHit] = []
    for h in merged:
        key = _clip(h.title, 80)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def search_info_hits(question: str, limit: int = 8) -> tuple[list[str], list[InfoHit]]:
    init_db()
    keywords = extract_keywords(question)
    engine = build_engine()
    hits: list[InfoHit] = []
    with engine.connect() as conn:
        hits.extend(_search_financial_alerts(conn, keywords, limit))
        hits.extend(_search_global_news(conn, keywords, limit))
    hits.sort(key=lambda h: (h.score, h.published_at_utc), reverse=True)
    # 去重：同标题近似
    seen: set[str] = set()
    deduped: list[InfoHit] = []
    for h in hits:
        key = _clip(h.title, 80)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
        if len(deduped) >= limit:
            break
    return keywords, deduped


def _format_sources_block(hits: list[InfoHit], max_items: int | None = None) -> str:
    """可核对的一览：来源、时间、标题、摘要、链接。"""
    n = max_items if max_items is not None else INFO_QUERY_MAX_SOURCES
    if not hits:
        return "【引用来源】\n（本次未命中新闻标题，结论仅来自上方东财量化数据。）"
    cap = INFO_QUERY_SNIPPET_CHARS
    lines = [
        "────",
        f"【引用来源 · 共 {min(len(hits), n)} 条】",
        "（经 Google News 检索收录；下列「来源」为原文媒体名）",
    ]
    for i, h in enumerate(hits[:n], 1):
        lines.append(f"\n[{i}] 来源: {h.source_label} | {_fmt_cn_time(h.published_at_utc)}")
        lines.append(f"标题: {h.title[:220]}")
        if h.snippet:
            lines.append(f"摘要: {_clip(h.snippet, cap)}")
        if h.url and str(h.url).startswith("http"):
            lines.append(f"链接: {str(h.url)[:240]}")
    return "\n".join(lines)


def _rule_based_viewpoint(question: str, snap: str | None) -> str:
    """无 Gemini 或作补充时，从数据块生成简短观点。"""
    if not snap:
        return ""
    q = question
    lines = ["【核心观点·据东财数据】"]
    if "北向" in q or "外资" in q:
        if "净买额" in snap or "资金流入" in snap:
            lines.append("· 以上为东财北向资金日频指标；净买额/资金流入为正偏利好，为负偏谨慎。")
        else:
            lines.append("· 北向日频净流入指标近期未更新，以下游新闻与沪深300等字段为辅。")
    elif "两融" in q or "融资" in q:
        lines.append("· 融资余额上升通常反映杠杆风险偏好抬升；需结合指数涨跌综合判断。")
    else:
        lines.append("· 以下为东财数据中心量化字段，请结合具体数值自行判断多空。")
    lines.append("· 观点不构成投资建议；新闻详情见下方引用来源。")
    return "\n".join(lines)


def _resolve_generation_mode(
    use_gemini: bool,
    insight: str | None,
    *,
    gemini_error: str | None = None,
) -> str:
    if insight:
        return "gemini"
    if not use_gemini:
        return "rule_disabled"
    if not LLM_SUMMARY_ENABLED:
        return "rule_llm_off"
    if not GEMINI_API_KEY.strip():
        return "rule_no_key"
    if gemini_error:
        return "rule_gemini_failed"
    return "rule_fallback"


def _friendly_gemini_error(err: str | None) -> str:
    try:
        from llm.gemini_client import _parse_gemini_quota_hint

        if err and ("429" in err or "quota" in err.lower()):
            parsed = _parse_gemini_quota_hint(err)
            if parsed:
                return parsed.replace("**", "")
    except Exception:  # noqa: BLE001
        pass
    e = (err or "").lower()
    if "429" in e or "resource_exhausted" in e or "quota" in e:
        return (
            "Gemini 配额已用尽（HTTP 429）。免费档 gemini-2.5-flash 多为 20 次/天，"
            "等几分钟无效；请打开 https://ai.dev/rate-limit 查看，或换新 API Key / 开通计费"
        )
    if "403" in e or "api key" in e:
        return "Gemini API Key 无效或无权限（HTTP 403），请检查 .env 中 GEMINI_API_KEY"
    if "timeout" in e or "connection" in e:
        return "无法连接 Gemini（网络超时），请检查 VPN 能否访问 generativelanguage.googleapis.com"
    return _clip(err or "未知错误", 100)


def _generation_mode_label(mode: str, gemini_error: str | None = None) -> str:
    fail_detail = _friendly_gemini_error(gemini_error)
    labels = {
        "gemini": "【生成方式】Gemini 已对下方检索结果与数据做归纳（见【结论】【观点】）",
        "rule_disabled": "【生成方式】未使用 LLM（本次请求关闭 Gemini，规则模板 + 标题摘录）",
        "rule_llm_off": "【生成方式】未使用 LLM（.env 中 LLM_SUMMARY_ENABLED=false）",
        "rule_no_key": "【生成方式】未使用 LLM（未配置 GEMINI_API_KEY）",
        "rule_gemini_failed": f"【生成方式】未使用 LLM（{fail_detail}）",
        "rule_fallback": "【生成方式】未使用 LLM（规则模板 + 标题摘录，未生成【结论】【观点】）",
    }
    return labels.get(mode, labels["rule_fallback"])


def _assemble_answer_text(
    question: str,
    keywords: list[str],
    hits: list[InfoHit],
    *,
    live_status: list[str] | None,
    prefer_live_sentiment: bool,
    use_gemini: bool,
) -> tuple[str, str]:
    kw_show = "、".join(keywords) if keywords else "（自动）"
    header = [
        f"【问答】{question.strip()}",
        f"检索词: {kw_show}",
    ]
    if live_status:
        header.append("现场: " + " | ".join(live_status))
    header.append("")

    init_db()
    engine = build_engine()
    with engine.connect() as conn:
        snap = _sentiment_snapshot(conn, question, prefer_live=prefer_live_sentiment)

    body_parts: list[str] = []
    if snap:
        body_parts.append(snap)
        body_parts.append("")

    insight = None
    gemini_error: str | None = None
    if use_gemini and _gemini_enabled() and (hits or snap):
        try:
            from llm.gemini_client import answer_info_question_with_gemini, gemini_available

            if gemini_available():
                insight = answer_info_question_with_gemini(
                    question=question,
                    hits=hits,
                    data_context=snap,
                )
        except Exception as exc:  # noqa: BLE001
            gemini_error = str(exc)
            logger.warning("Gemini 问答失败: %s", exc)

    gen_mode = _resolve_generation_mode(use_gemini, insight, gemini_error=gemini_error)
    header.append(_generation_mode_label(gen_mode, gemini_error))
    header.append("")

    if insight:
        body_parts.append(insight)
    else:
        vp = _rule_based_viewpoint(question, snap)
        if vp:
            body_parts.append(vp)
        if hits:
            body_parts.append("")
            body_parts.append("【要点摘录】")
            for i, h in enumerate(hits[:4], 1):
                body_parts.append(f"· [{i}] {_clip(h.title, 100)}（{h.source_label}）")

    body_parts.append("")
    body_parts.append(_format_sources_block(hits))

    text = "\n".join(header + body_parts)
    if len(text) > 1800:
        # 优先保留：头、数据、结论观点、来源标题+链接
        short_hits = hits[:3]
        body_parts = []
        if snap:
            body_parts.extend([snap, ""])
        if insight:
            body_parts.append(_clip(insight, 650))
        else:
            body_parts.append(_rule_based_viewpoint(question, snap))
        body_parts.extend(["", _format_sources_block(short_hits, max_items=3)])
        text = "\n".join(header + body_parts)
    return text[:1800], gen_mode


def _format_hits_plain(
    question: str,
    keywords: list[str],
    hits: list[InfoHit],
    *,
    live_status: list[str] | None = None,
    prefer_live_sentiment: bool = False,
) -> str:
    kw_show = "、".join(keywords) if keywords else "（全文）"
    mode = "现场检索+本地库" if live_status else "本地库"
    lines = [
        f"【问答·{mode}】{question.strip()}",
        f"检索词: {kw_show}",
    ]
    if live_status:
        lines.append("现场: " + " | ".join(live_status))
    lines.append("")
    engine = build_engine()
    with engine.connect() as conn:
        snap = _sentiment_snapshot(conn, question, prefer_live=prefer_live_sentiment)
    if snap:
        lines.append(snap)
        lines.append("")
    if not hits:
        if snap:
            lines.append("【说明】未检索到相关新闻标题；上列为东财量化数据（非资讯）。")
            lines.append("可换关键词（如 北向、沪股通）或运行: python3 -m sentiment_etl.runner")
        else:
            lines.append("未找到匹配资讯（现场源与本地库均无命中）。")
            lines.append("可换关键词重试，或运行 python3 -m sentiment_etl.runner 全量同步。")
        return "\n".join(lines)
    lines.append(_format_sources_block(hits))
    return "\n".join(lines)[:1800]


def _gemini_enabled() -> bool:
    return LLM_SUMMARY_ENABLED and bool(GEMINI_API_KEY.strip())


def answer_info_question(
    question: str,
    *,
    use_gemini: bool | None = None,
    live: bool | None = None,
) -> dict[str, Any]:
    """根据用户问题检索资讯；默认现场拉取各源后再整理。"""
    q = (question or "").strip()
    if len(q) < 2:
        return {
            "text": "请输入至少 2 个字的问题，例如：北向资金最近怎样、美联储加息",
            "keywords": [],
            "hits": [],
            "count": 0,
            "live": False,
        }
    keywords = extract_keywords(q)
    do_live = live if live is not None else INFO_QUERY_LIVE_FETCH
    live_status: list[str] = []
    local_kw, local_hits = search_info_hits(q, limit=10)
    if not keywords:
        keywords = local_kw
    hits = local_hits
    if do_live:
        live_hits, live_status = fetch_live_hits(q, keywords, limit=12)
        hits = _merge_hits(local_hits, live_hits, limit=10)
    do_gemini = use_gemini if use_gemini is not None else LLM_SUMMARY_ENABLED
    text, generation_mode = _assemble_answer_text(
        q,
        keywords,
        hits,
        live_status=live_status if do_live else None,
        prefer_live_sentiment=do_live,
        use_gemini=do_gemini,
    )
    return {
        "text": text,
        "generation_mode": generation_mode,
        "llm_used": generation_mode == "gemini",
        "keywords": keywords,
        "hits": [
            {
                "source": h.source_label,
                "title": h.title,
                "published_at": _fmt_cn_time(h.published_at_utc),
                "url": h.url,
                "score": h.score,
            }
            for h in hits
        ],
        "count": len(hits),
        "live": do_live,
        "live_status": live_status,
    }
