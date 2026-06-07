"""新闻入库后关联个股：代码正则 + 简称词典（P0）。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.models import (
    FinancialAlert,
    GlobalNews,
    NewsStockLink,
    SessionLocal,
    StockList,
    init_db,
)

logger = logging.getLogger(__name__)

# A 股 6 位代码（多种写法）
_CODE_RE_A1 = re.compile(
    r"(?<![0-9])([0368]\d{5})(?:\.(?:SH|SZ|BJ))?(?![0-9])",
    re.IGNORECASE,
)
_CODE_RE_A2 = re.compile(
    r"(?:SH|SZ|BJ)\s*[:：]?\s*([0368]\d{5})(?![0-9])",
    re.IGNORECASE,
)
_CODE_RE_A3 = re.compile(r"[\[【（(]([0368]\d{5})[\]】）)]")

# 港股 5 位代码（通常以 0 开头，如 00700.HK）
_CODE_RE_HK1 = re.compile(
    r"(?<![0-9])(\d{5})(?:\.HK)?(?![0-9])",
    re.IGNORECASE,
)
_CODE_RE_HK2 = re.compile(
    r"HK\s*[:：]?\s*(\d{5})(?![0-9])",
    re.IGNORECASE,
)

_CODE_RES: tuple[re.Pattern[str], ...] = (
    _CODE_RE_A1,
    _CODE_RE_A2,
    _CODE_RE_A3,
    _CODE_RE_HK1,
    _CODE_RE_HK2,
)

# 常见非股票 6 位噪声（年份、日期片段等弱过滤）
_CODE_BLOCKLIST = frozenset({"202400", "202500", "202600"})

# 媒体/平台简称：也是上市公司但出现在标题末尾「来源」时不应链股
_MEDIA_ALIAS_BLOCKLIST = frozenset(
    {
        "东方财富",
        "新华网",
        "同花顺",
        "财联社",
        "证券时报",
        "第一财经",
        "证券之星",
        "南方财经",
        "新浪财经",
        "雪球",
        "金融界",
        "和讯",
        "财新",
        "澎湃",
        "每经",
        "中证网",
    }
)
_MEDIA_STOCK_CODES = frozenset({"300059", "603888"})  # 东方财富、新华网

_NAME_SUFFIXES = (
    "股份有限公司",
    "集团股份有限公司",
    "有限公司",
    "股份",
    "集团",
    "控股",
)

_ATTRIBUTION_SEPS = (" - ", " — ", " | ", "丨", " – ")

_EVENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("earnings_preview", re.compile(r"业绩预告|业绩预增|业绩预减|业绩快报")),
    ("share_reduction", re.compile(r"减持|清仓式减持")),
    ("regulatory", re.compile(r"监管|问询函|立案调查|处罚")),
    ("cooperation", re.compile(r"战略合作|签约|合作协议")),
    ("buyback", re.compile(r"回购")),
]


@dataclass(frozen=True)
class StockMatch:
    ts_code: str
    confidence: float
    matched_by: str
    event_type: str | None = None


def _normalize_code(raw: str) -> str | None:
    """统一代码格式：A 股 6 位，港股 5 位。"""
    c = raw.strip().upper()
    # 去掉后缀
    if c.endswith(".HK"):
        c = c[:-3]
    elif any(c.endswith(f".{s}") for s in ("SH", "SZ", "BJ")):
        c = c.split(".", 1)[0]

    if not c.isdigit():
        return None
    if c in _CODE_BLOCKLIST:
        return None

    # 5 位 → 港股（补前导零）
    if len(c) == 5:
        # 以 0 开头的 5 位代码是典型港股
        return c.zfill(5)
    # 4 位 → 可能是港股省略前导零（如 0700 → 00700）
    if len(c) == 4 and c[0] != "0":
        return c.zfill(5)

    # A 股：6 位代码需以 0/3/6/8 开头
    if len(c) == 6 and c[0] in "0368":
        return c
    # 5 位以非 0 开头 → 可能是 A 股缺前导零
    if len(c) == 5 and c[0] in "368":
        return c.zfill(6)
    if len(c) == 6 and c.isdigit():
        return c  # 放宽：接受所有 6 位数字（含港股通可能的新代码）

    return None


def _alias_is_source_attribution(text: str, alias: str) -> bool:
    """标题尾部「 - 东方财富」类来源标注，不算正文提及个股。"""
    for sep in _ATTRIBUTION_SEPS:
        idx = text.rfind(sep)
        if idx >= 0 and alias in text[idx:]:
            return True
    return False


def load_stock_alias_dict() -> dict[str, str]:
    """简称/全称片段 → ts_code（长名称优先匹配）。"""
    init_db()
    aliases: dict[str, str] = {}
    with SessionLocal() as session:
        for ts_code, name in session.execute(select(StockList.ts_code, StockList.name)):
            code = str(ts_code).zfill(6)
            nm = str(name or "").strip()
            if not nm or len(nm) < 2:
                continue
            if nm in _MEDIA_ALIAS_BLOCKLIST:
                continue
            aliases[nm] = code
            compact = nm.replace(" ", "")
            if compact and compact not in aliases:
                aliases[compact] = code
            if nm.endswith("A") and len(nm) > 2:
                base = nm[:-1]
                if len(base) >= 2:
                    aliases[base] = code
            for suf in _NAME_SUFFIXES:
                if nm.endswith(suf) and len(nm) > len(suf) + 1:
                    short = nm[: -len(suf)].strip()
                    if len(short) >= 2 and short not in _MEDIA_ALIAS_BLOCKLIST:
                        aliases.setdefault(short, code)
    return aliases


def detect_event_type(text: str) -> str | None:
    for label, pat in _EVENT_PATTERNS:
        if pat.search(text):
            return label
    return None


def match_stocks_in_text(
    text: str,
    *,
    alias_dict: dict[str, str] | None = None,
) -> list[StockMatch]:
    if not text or not text.strip():
        return []
    aliases = alias_dict if alias_dict is not None else load_stock_alias_dict()
    found: dict[str, StockMatch] = {}
    event_type = detect_event_type(text)

    for pat in _CODE_RES:
        for m in pat.finditer(text):
            code = _normalize_code(m.group(1))
            if not code:
                continue
            found[code] = StockMatch(
                ts_code=code,
                confidence=0.95,
                matched_by="regex_code",
                event_type=event_type,
            )

    # 简称：按长度降序，避免「银行」误伤
    for alias in sorted(aliases.keys(), key=len, reverse=True):
        if len(alias) < 3:
            continue
        if alias in _MEDIA_ALIAS_BLOCKLIST:
            continue
        if alias not in text:
            continue
        if _alias_is_source_attribution(text, alias):
            continue
        code = aliases[alias]
        if code not in found:
            conf = 0.9 if len(alias) >= 4 else 0.85
            found[code] = StockMatch(
                ts_code=code,
                confidence=conf,
                matched_by="name_dict",
                event_type=event_type,
            )
    return list(found.values())


def _link_rows_for_news(
    *,
    news_source: str,
    news_id: int,
    published_at_utc: datetime,
    title: str,
    summary: str | None,
    alias_dict: dict[str, str],
) -> list[dict[str, object]]:
    blob = f"{title}\n{summary or ''}"
    matches = match_stocks_in_text(blob, alias_dict=alias_dict)
    return [
        {
            "news_source": news_source,
            "news_id": news_id,
            "ts_code": m.ts_code,
            "confidence": m.confidence,
            "matched_by": m.matched_by,
            "event_type": m.event_type,
            "published_at_utc": published_at_utc,
        }
        for m in matches
    ]


def upsert_news_stock_links(rows: Iterable[dict[str, object]]) -> int:
    batch = list(rows)
    if not batch:
        return 0
    with SessionLocal() as session:
        stmt = insert(NewsStockLink).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                NewsStockLink.news_source,
                NewsStockLink.news_id,
                NewsStockLink.ts_code,
            ],
            set_={
                "confidence": stmt.excluded.confidence,
                "matched_by": stmt.excluded.matched_by,
                "event_type": stmt.excluded.event_type,
                "published_at_utc": stmt.excluded.published_at_utc,
            },
        )
        res = session.execute(stmt)
        session.commit()
        return res.rowcount or 0


def prune_media_false_links() -> int:
    """删除「来源标注」误链到媒体股（东方财富、新华网等）的记录。"""
    if not _MEDIA_STOCK_CODES:
        return 0
    init_db()
    codes_sql = ", ".join(f"'{c}'" for c in sorted(_MEDIA_STOCK_CODES))
    with SessionLocal() as session:
        res = session.execute(
            text(
                f"""
                DELETE FROM news_stock_link
                WHERE matched_by = 'name_dict'
                  AND ts_code IN ({codes_sql})
                """
            ),
        )
        session.commit()
        return int(res.rowcount or 0)


def link_recent_news(
    *,
    hours: int = 168,
    limit_per_table: int = 2000,
    relink: bool = False,
) -> dict[str, int]:
    """为近期新闻补链；relink=True 时重扫已有关联条目（应用新规则）。"""
    init_db()
    prune_media_false_links()
    alias_dict = load_stock_alias_dict()
    since = datetime.utcnow() - timedelta(hours=hours)
    stats = {"financial_alerts": 0, "global_news": 0, "links_upserted": 0, "relink": int(relink)}

    with SessionLocal() as session:
        fa_q = (
            select(
                FinancialAlert.id,
                FinancialAlert.published_at_utc,
                FinancialAlert.title,
                FinancialAlert.summary,
            )
            .where(FinancialAlert.published_at_utc >= since)
            .order_by(FinancialAlert.published_at_utc.desc())
            .limit(limit_per_table)
        )
        if not relink:
            fa_q = fa_q.where(
                ~FinancialAlert.id.in_(
                    select(NewsStockLink.news_id).where(
                        NewsStockLink.news_source == "financial_alerts"
                    )
                )
            )
        fa_rows = session.execute(fa_q).all()

        gn_q = (
            select(
                GlobalNews.id,
                GlobalNews.published_at_utc,
                GlobalNews.title,
                GlobalNews.description,
            )
            .where(GlobalNews.published_at_utc >= since)
            .order_by(GlobalNews.published_at_utc.desc())
            .limit(limit_per_table)
        )
        if not relink:
            gn_q = gn_q.where(
                ~GlobalNews.id.in_(
                    select(NewsStockLink.news_id).where(
                        NewsStockLink.news_source == "global_news"
                    )
                )
            )
        gn_rows = session.execute(gn_q).all()

    link_rows: list[dict[str, object]] = []
    for nid, pub, title, summary in fa_rows:
        link_rows.extend(
            _link_rows_for_news(
                news_source="financial_alerts",
                news_id=int(nid),
                published_at_utc=pub,
                title=str(title or ""),
                summary=str(summary) if summary else None,
                alias_dict=alias_dict,
            )
        )
        stats["financial_alerts"] += 1

    for nid, pub, title, desc in gn_rows:
        link_rows.extend(
            _link_rows_for_news(
                news_source="global_news",
                news_id=int(nid),
                published_at_utc=pub,
                title=str(title or ""),
                summary=str(desc) if desc else None,
                alias_dict=alias_dict,
            )
        )
        stats["global_news"] += 1

    stats["links_upserted"] = upsert_news_stock_links(link_rows)
    logger.info("[INFO] news_stock_link 补链: %s", stats)
    return stats


def link_unlinked_news(*, hours: int = 168, limit_per_table: int = 500) -> dict[str, int]:
    """为近期尚未关联的新闻补链（兼容旧调用）。"""
    return link_recent_news(hours=hours, limit_per_table=limit_per_table, relink=False)


def link_news_ids(
    *,
    financial_alert_ids: list[int] | None = None,
    global_news_ids: list[int] | None = None,
) -> int:
    """入库后即时关联指定 id。"""
    init_db()
    alias_dict = load_stock_alias_dict()
    link_rows: list[dict[str, object]] = []

    with SessionLocal() as session:
        if financial_alert_ids:
            for row in session.execute(
                select(
                    FinancialAlert.id,
                    FinancialAlert.published_at_utc,
                    FinancialAlert.title,
                    FinancialAlert.summary,
                ).where(FinancialAlert.id.in_(financial_alert_ids))
            ):
                link_rows.extend(
                    _link_rows_for_news(
                        news_source="financial_alerts",
                        news_id=int(row[0]),
                        published_at_utc=row[1],
                        title=str(row[2] or ""),
                        summary=str(row[3]) if row[3] else None,
                        alias_dict=alias_dict,
                    )
                )
        if global_news_ids:
            for row in session.execute(
                select(
                    GlobalNews.id,
                    GlobalNews.published_at_utc,
                    GlobalNews.title,
                    GlobalNews.description,
                ).where(GlobalNews.id.in_(global_news_ids))
            ):
                link_rows.extend(
                    _link_rows_for_news(
                        news_source="global_news",
                        news_id=int(row[0]),
                        published_at_utc=row[1],
                        title=str(row[2] or ""),
                        summary=str(row[3]) if row[3] else None,
                        alias_dict=alias_dict,
                    )
                )
    return upsert_news_stock_links(link_rows)


def _title_like_news_items(
    code: str,
    name: str | None,
    *,
    since: datetime,
    limit: int,
) -> list[dict[str, object]]:
    """无 link 或 link 过少时：标题/摘要 LIKE 回退（含 global_news）。"""
    patterns = [f"%{code}%"]
    if name:
        compact = str(name).replace(" ", "").strip()
        if compact:
            patterns.append(f"%{compact}%")
    clauses = []
    params: dict[str, object] = {"since": since, "lim": limit}
    for i, pat in enumerate(patterns):
        key = f"p{i}"
        params[key] = pat
        clauses.append(f"title LIKE :{key} OR COALESCE(summary, '') LIKE :{key}")
    fa_where = " OR ".join(clauses)
    gn_clauses = []
    for i, pat in enumerate(patterns):
        key = f"g{i}"
        params[key] = pat
        gn_clauses.append(f"title LIKE :{key} OR COALESCE(description, '') LIKE :{key}")
    gn_where = " OR ".join(gn_clauses)

    sql = text(
        f"""
        SELECT news_source, news_id, confidence, matched_by, event_type,
               published_at_utc, title, article_url, source_label
        FROM (
          SELECT 'financial_alerts' AS news_source, id AS news_id,
                 0.55 AS confidence, 'title_like' AS matched_by, NULL AS event_type,
                 published_at_utc, title, article_url, source AS source_label
          FROM financial_alerts
          WHERE published_at_utc >= :since AND ({fa_where})
          UNION ALL
          SELECT 'global_news', id, 0.5, 'title_like', NULL,
                 published_at_utc, title, article_url, channel
          FROM global_news
          WHERE published_at_utc >= :since AND ({gn_where})
        ) u
        ORDER BY published_at_utc DESC
        LIMIT :lim
        """
    )
    with SessionLocal() as session:
        rows = session.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def get_linked_news_for_stock(
    ts_code: str,
    *,
    hours: int = 48,
    limit: int = 20,
    title_like_fallback: bool = True,
) -> list[dict[str, object]]:
    """选股 / info_query 共用：news_stock_link 优先，不足时标题 LIKE 补 global/fa。"""
    init_db()
    code = str(ts_code).strip().zfill(6)
    since = datetime.utcnow() - timedelta(hours=hours)
    sql = text(
        """
        SELECT l.news_source, l.news_id, l.confidence, l.matched_by, l.event_type,
               l.published_at_utc,
               CASE l.news_source
                 WHEN 'financial_alerts' THEN fa.title
                 ELSE gn.title
               END AS title,
               CASE l.news_source
                 WHEN 'financial_alerts' THEN fa.article_url
                 ELSE gn.article_url
               END AS article_url,
               CASE l.news_source
                 WHEN 'financial_alerts' THEN fa.source
                 ELSE gn.channel
               END AS source_label
        FROM news_stock_link l
        LEFT JOIN financial_alerts fa
          ON l.news_source = 'financial_alerts' AND l.news_id = fa.id
        LEFT JOIN global_news gn
          ON l.news_source = 'global_news' AND l.news_id = gn.id
        WHERE l.ts_code = :code AND l.published_at_utc >= :since
        ORDER BY l.published_at_utc DESC, l.confidence DESC
        LIMIT :lim
        """
    )
    with SessionLocal() as session:
        rows = session.execute(sql, {"code": code, "since": since, "lim": limit}).mappings().all()
        out = [dict(r) for r in rows]
        name: str | None = None
        if title_like_fallback and len(out) < min(5, limit):
            name_row = session.execute(
                select(StockList.name).where(StockList.ts_code == code)
            ).first()
            name = str(name_row[0]) if name_row and name_row[0] else None

    if title_like_fallback and len(out) < min(5, limit):
        seen = {(r["news_source"], r["news_id"]) for r in out}
        for item in _title_like_news_items(code, name, since=since, limit=limit):
            key = (item["news_source"], item["news_id"])
            if key not in seen:
                out.append(item)
                seen.add(key)
            if len(out) >= limit:
                break
    return out[:limit]


def news_link_coverage_stats(*, hours: int = 48) -> dict[str, object]:
    """数据质量：近期新闻关联覆盖率。"""
    init_db()
    since = datetime.utcnow() - timedelta(hours=hours)
    out: dict[str, object] = {"hours": hours}
    with SessionLocal() as session:
        for table, source in (("financial_alerts", "financial_alerts"), ("global_news", "global_news")):
            total = session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE published_at_utc >= :since"),
                {"since": since},
            ).scalar_one()
            linked = session.execute(
                text(
                    f"SELECT COUNT(DISTINCT news_id) FROM news_stock_link "
                    f"WHERE news_source = :src AND published_at_utc >= :since"
                ),
                {"src": source, "since": since},
            ).scalar_one()
            out[f"{source}_total"] = int(total or 0)
            out[f"{source}_linked"] = int(linked or 0)
            out[f"{source}_coverage_pct"] = round(
                100.0 * int(linked or 0) / max(int(total or 0), 1), 2
            )
        null_ind = session.execute(
            select(func.count()).select_from(StockList).where(StockList.industry.is_(None))
        ).scalar_one()
        out["stock_list_null_industry"] = int(null_ind or 0)
    return out
