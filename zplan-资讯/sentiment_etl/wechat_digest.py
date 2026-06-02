from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config import (
    LLM_SUMMARY_ENABLED,
    NEWSAPI_KEY,
    SENTIMENT_WECHAT_DIGEST_LLM,
    SENTIMENT_WECHAT_SAMPLE_PER_SOURCE,
    SENTIMENT_WECHAT_PUSH,
    SENTIMENT_WECHAT_STYLE,
    WECHAT_PUSH_WEBHOOK,
)
from sentiment_etl.timeutil import CN_TZ
from wechat_limits import WECHAT_MARKDOWN_MAX_BYTES, WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8

logger = logging.getLogger(__name__)

_INDEX_NAMES = {"000001": "上证", "399001": "深证成指", "399006": "创业板指"}

# 企微 markdown 可点击来源
_EM_FLASH_HUB = "https://finance.eastmoney.com/a/cjjdd.html"
_EM_HSGT = "https://data.eastmoney.com/hsgtc/index.html"
_EM_RZRQ = "https://data.eastmoney.com/rzrq/"
_EM_QUOTE = "https://quote.eastmoney.com/"

_SOURCE_LABELS: dict[str, str] = {
    "em_financial_flash": "东财快讯",
    "em_northbound_daily": "北向资金",
    "em_northbound_intraday": "北向分时",
    "em_margin_account": "融资融券",
    "em_index_turnover": "主要指数",
    "newsapi": "NewsAPI",
    "google_rss": "Google RSS",
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


def _fmt_trade_date(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "-"
    ts = pd.Timestamp(val)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    return ts.tz_convert(CN_TZ).strftime("%Y-%m-%d")


def _fmt_num(val: Any, *, unit: str = "", digits: int = 2) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "-"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    if abs(f) >= 10000:
        return f"{f:,.0f}{unit}"
    return f"{f:.{digits}f}{unit}".rstrip("0").rstrip(".") + unit


def _fmt_amount_yi(val: Any) -> str:
    """腾讯指数 amount：原值多为百万元量级，>1e10 时按元÷1e8。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "-"
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    yi = f / 1e8 if f >= 1e10 else f / 1e6
    return f"{yi:.0f} 亿元" if yi >= 100 else f"{yi:.1f} 亿元"


def _clip(text: str, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _md_link(label: str, url: str) -> str:
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return str(label)
    lab = str(label).replace("[", "").replace("]", "")
    return f"[{lab}]({u})"


def _flash_records_from_df(df: pd.DataFrame, n: int) -> list[dict[str, str]]:
    sub = df.sort_values("published_at_utc", ascending=False).head(n)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, r in sub.iterrows():
        title = " ".join(str(r.get("title") or "").split())
        if not title or title in seen:
            continue
        seen.add(title)
        url = str(r.get("article_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            url = _EM_FLASH_HUB
        records.append(
            {
                "title": title,
                "summary": " ".join(str(r.get("summary") or "").split()),
                "url": url,
                "time": _fmt_utc_as_cn(r.get("published_at_utc")),
            }
        )
    return records


def _rule_flash_overview(records: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    """Gemini 不可用时的短规则归纳（仍带 source_index 供拼链接）。"""
    if not records:
        return "暂无可用快讯。", []
    themes = "、".join(_clip(r["title"], 22) for r in records[:3])
    overview = (
        f"近时段东财全球快讯共 {len(records)} 条，"
        f"主要涉及 {themes} 等方向，详见下方要点与原文链接。"
    )
    highlights: list[dict[str, Any]] = []
    for i, rec in enumerate(records[:5], start=1):
        takeaway = _clip(rec.get("summary") or rec["title"], 88)
        highlights.append({"takeaway": takeaway, "source_index": i})
    return overview, highlights


def _section_brief_news(
    df: pd.DataFrame | None,
    inserted: dict[str, Any],
    n: int,
) -> str | None:
    if df is None or df.empty:
        return None

    records = _flash_records_from_df(df, n)
    if not records:
        return None

    overview = ""
    highlights: list[dict[str, Any]] = []
    use_llm = LLM_SUMMARY_ENABLED and SENTIMENT_WECHAT_DIGEST_LLM

    if use_llm:
        try:
            from llm.gemini_client import gemini_available, summarize_flash_digest_with_gemini

            if gemini_available():
                llm_items = [{"title": r["title"], "summary": r["summary"]} for r in records]
                parsed = summarize_flash_digest_with_gemini(llm_items, max_items=n)
                overview = str(parsed.get("overview") or "").strip()
                highlights = list(parsed.get("highlights") or [])
        except Exception as exc:
            logger.warning("digest Gemini 总结失败，降级规则归纳: %s", exc)

    if not overview:
        overview, highlights = _rule_flash_overview(records)

    lines = ["### 要闻总结", overview, ""]
    if highlights:
        lines.append("**要点**")
        used_urls: set[str] = set()
        for h in highlights[:5]:
            takeaway = str(h.get("takeaway") or "").strip()
            if not takeaway:
                continue
            try:
                idx = int(h.get("source_index", 0)) - 1
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(records):
                rec = records[idx]
                lines.append(f"· {takeaway} {_md_link('阅读原文', rec['url'])}")
                used_urls.add(rec["url"])
            else:
                lines.append(f"· {takeaway}")

        extra_links = [r for r in records if r["url"] not in used_urls][:2]
        if extra_links:
            lines.append("")
            lines.append("**更多报道**")
            for rec in extra_links:
                lines.append(f"· {_md_link(_clip(rec['title'], 36), rec['url'])}")

    ins = inserted.get("em_financial_flash")
    from_db = inserted.get("__from_db__")
    if from_db and ins is not None:
        stat = f"库内累计 {ins} 条"
    elif isinstance(ins, int):
        stat = f"本轮新增 {ins} 条" if ins else "本轮无新增"
    else:
        stat = ""
    footer = f"> 快讯来源 {_md_link('东方财富全球快讯', _EM_FLASH_HUB)}"
    if stat:
        footer = f"{footer} · {stat}"
    lines.extend(["", footer])
    return "\n".join(lines)


def _northbound_fact(df: pd.DataFrame | None) -> str | None:
    if df is None or df.empty:
        return None
    primary = None
    for name in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
        row = _latest_metric_row(df, name)
        if row is not None:
            primary = (name, row)
            break
    if primary is None:
        return None
    pname, last = primary
    d = _fmt_trade_date(last.get("as_of_utc"))
    val = _fmt_num(last.get("metric_value"), digits=2) + " 亿元"
    return f"北向资金（数据日 {d}）{pname} {val}"


def _margin_fact(df: pd.DataFrame | None) -> str | None:
    if df is None or df.empty:
        return None
    parts: list[str] = []
    last_d = "-"
    for name in ("融资余额", "融券余额", "融资买入额"):
        sub = df[df["metric_name"] == name].sort_values("as_of_utc")
        if not sub.empty:
            r = sub.iloc[-1]
            parts.append(f"{name} {_fmt_num(r['metric_value'])} 亿")
            last_d = _fmt_trade_date(r["as_of_utc"])
    if not parts:
        return None
    return f"融资融券（{last_d}）" + "，".join(parts)


def _index_fact(df: pd.DataFrame | None) -> str | None:
    if df is None or df.empty:
        return None
    rows: list[str] = []
    for sym in df["subject"].drop_duplicates():
        name = _INDEX_NAMES.get(str(sym), f"指数{sym}")
        for metric in ("换手率", "成交额"):
            sub = df[(df["subject"] == sym) & (df["metric_name"] == metric)].sort_values("as_of_utc")
            if sub.empty:
                continue
            r = sub.iloc[-1]
            d = _fmt_trade_date(r.get("as_of_utc"))
            if metric == "换手率":
                rows.append(f"{name}（{d}）换手率 {_fmt_num(r['metric_value'])}%")
            else:
                rows.append(f"{name}（{d}）成交额 {_fmt_amount_yi(r['metric_value'])}")
            break
    return "主要指数：" + "；".join(rows) if rows else None


def _section_market_brief(fetched: dict[str, Any]) -> str | None:
    sentences: list[str] = []
    nb = _northbound_fact(fetched.get("em_northbound_daily"))
    if nb:
        sentences.append(nb)
    mg = _margin_fact(fetched.get("em_margin_account"))
    if mg:
        sentences.append(mg)
    ix = _index_fact(fetched.get("em_index_turnover"))
    if ix:
        sentences.append(ix)
    if not sentences:
        return None
    narrative = "。".join(sentences) + "。"
    links = " · ".join(
        [
            _md_link("北向资金", _EM_HSGT),
            _md_link("融资融券", _EM_RZRQ),
            _md_link("行情中心", _EM_QUOTE),
        ]
    )
    return f"### 市场概况\n{narrative}\n\n> 数据来源 {links}"


def _latest_metric_row(df: pd.DataFrame, metric_name: str) -> pd.Series | None:
    sub = df[df["metric_name"] == metric_name].dropna(subset=["metric_value"]).sort_values("as_of_utc")
    return sub.iloc[-1] if not sub.empty else None


def _flash_line(title: object, summary: object, *, max_title: int = 72) -> str:
    t = _clip(title, max_title)
    s = " ".join(str(summary or "").split())
    if not s or s == t:
        return t
    if s.startswith(f"【{t}】"):
        s = s[len(t) + 2 :].lstrip("】").strip()
    if s.startswith(t):
        rest = s[len(t) :].lstrip("】").strip()
        return _clip(f"{t} — {rest}" if rest else t, max_title + 40)
    return _clip(f"{t} — {_clip(s, 60)}", max_title + 40)


def _inserted_line(label: str, inserted: dict[str, Any], *, factor: bool = False) -> str:
    v = inserted.get(label)
    from_db = inserted.get("__from_db__")
    if isinstance(v, dict) and "error" in v:
        return "状态: 入库失败"
    if v is None:
        return "状态: 未执行"
    if from_db:
        return f"库内累计 {v} 条"
    if factor:
        return f"本轮同步 {v} 条指标"
    n = int(v or 0)
    return f"本轮新增 {n} 条" if n else "本轮无新增（均为重复）"


def _section_divider() -> str:
    return "────────────────"


def _section_flash(df: pd.DataFrame | None, inserted: dict[str, Any], n: int, *, brief: bool) -> str | None:
    if df is None or df.empty:
        return None if brief else "\n".join(
            [
                _section_divider(),
                f"【{_SOURCE_LABELS['em_financial_flash']}】",
                _inserted_line("em_financial_flash", inserted),
                "（无抓取数据）",
            ]
        )
    sub = df.sort_values("published_at_utc", ascending=False).head(n)
    lines = [
        _section_divider() if not brief else "",
        f"### 快讯" if brief else f"【来源】{_SOURCE_LABELS['em_financial_flash']}",
        _inserted_line("em_financial_flash", inserted) if not brief else "",
    ]
    lines = [x for x in lines if x]
    for i, (_, r) in enumerate(sub.iterrows(), 1):
        ts = _fmt_utc_as_cn(r.get("published_at_utc"))
        body = _flash_line(r.get("title"), r.get("summary"))
        prefix = f"{i}. [{ts}] " if not brief else f"- **[{ts}]** "
        lines.append(f"{prefix}{body}")
    return "\n".join(lines)


def _section_northbound_daily(df: pd.DataFrame | None, inserted: dict[str, Any], *, brief: bool) -> str | None:
    if df is None or df.empty:
        return None if brief else "\n".join(
            [
                _section_divider(),
                f"【{_SOURCE_LABELS['em_northbound_daily']}】",
                _inserted_line("em_northbound_daily", inserted, factor=True),
                "（无数据）",
            ]
        )
    primary = None
    for name in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
        row = _latest_metric_row(df, name)
        if row is not None:
            primary = (name, row)
            break
    if primary is None:
        return None if brief else "北向: 近期指标均为空"
    pname, last = primary
    d = _fmt_trade_date(last.get("as_of_utc"))
    val = _fmt_num(last.get("metric_value"), digits=2) + " 亿元"
    lines: list[str] = []
    if not brief:
        lines = [
            _section_divider(),
            f"【{_SOURCE_LABELS['em_northbound_daily']}】",
            _inserted_line("em_northbound_daily", inserted, factor=True),
            f"数据日 {d} | {pname} {val}（东财）",
        ]
    else:
        lines = [f"- **北向**（{d}）{pname} **{val}**"]
    extras = []
    for name in ("当日成交净买额", "当日资金流入", "买入成交额", "卖出成交额"):
        if name == pname:
            continue
        row = df[(df["as_of_utc"] == last["as_of_utc"]) & (df["metric_name"] == name)]
        if not row.empty and row.iloc[0]["metric_value"] is not None:
            extras.append(f"{name} {_fmt_num(row.iloc[0]['metric_value'])}")
    if extras and not brief:
        lines.append("  " + " | ".join(extras))
    return "\n".join(lines)


def _section_margin(df: pd.DataFrame | None, inserted: dict[str, Any], *, brief: bool) -> str | None:
    if df is None or df.empty:
        return None
    parts: list[str] = []
    for name in ("融资余额", "融券余额", "融资买入额"):
        sub = df[df["metric_name"] == name].sort_values("as_of_utc")
        if not sub.empty:
            r = sub.iloc[-1]
            parts.append(f"{name} {_fmt_num(r['metric_value'])} 亿")
    if not parts:
        return None
    d = _fmt_trade_date(sub.iloc[-1]["as_of_utc"])
    if brief:
        return f"- **两融**（{d}）" + " | ".join(parts)
    return "\n".join(
        [
            _section_divider(),
            f"【{_SOURCE_LABELS['em_margin_account']}】",
            _inserted_line("em_margin_account", inserted, factor=True),
            f"数据日 {d} | " + " | ".join(parts),
        ]
    )


def _section_turnover(df: pd.DataFrame | None, inserted: dict[str, Any], *, brief: bool) -> str | None:
    if df is None or df.empty:
        return None if brief else "\n".join(
            [
                _section_divider(),
                f"【{_SOURCE_LABELS['em_index_turnover']}】",
                _inserted_line("em_index_turnover", inserted, factor=True),
                "（未抓到）",
            ]
        )
    rows: list[str] = []
    for sym in df["subject"].drop_duplicates():
        name = _INDEX_NAMES.get(str(sym), f"指数{sym}")
        for metric in ("换手率", "成交额"):
            sub = df[(df["subject"] == sym) & (df["metric_name"] == metric)].sort_values("as_of_utc")
            if sub.empty:
                continue
            r = sub.iloc[-1]
            d = _fmt_trade_date(r.get("as_of_utc"))
            if metric == "换手率":
                rows.append(f"{name}（{d}）换手率 {_fmt_num(r['metric_value'])}%")
            else:
                rows.append(f"{name}（{d}）成交额 {_fmt_amount_yi(r['metric_value'])}")
            break
    if not rows:
        return None
    if brief:
        return "- **指数** " + "；".join(rows)
    return "\n".join(
        [
            _section_divider(),
            f"【{_SOURCE_LABELS['em_index_turnover']}】",
            _inserted_line("em_index_turnover", inserted, factor=True),
            *rows,
        ]
    )


def _section_news_optional(
    label_key: str,
    df: pd.DataFrame | None,
    inserted: dict[str, Any],
    n: int,
    *,
    brief: bool,
    skip_reason: str | None = None,
) -> str | None:
    if brief and (df is None or df.empty):
        return None
    lines = [
        _section_divider(),
        f"【{_SOURCE_LABELS[label_key]}】",
        _inserted_line(label_key, inserted),
    ]
    if skip_reason:
        lines.append(skip_reason)
        return "\n".join(lines)
    if df is None or df.empty:
        lines.append("（无抓取数据）")
        return "\n".join(lines)
    sub = df.sort_values("published_at_utc", ascending=False).head(n)
    for i, (_, r) in enumerate(sub.iterrows(), 1):
        lines.append(
            f"{i}. [{_fmt_utc_as_cn(r.get('published_at_utc'))}] {_clip(r.get('title'), 75)}"
        )
    return "\n".join(lines)


def _alerts_banner(stats: dict[str, Any]) -> str:
    alerts = stats.get("alerts") or []
    if not alerts:
        return ""
    short = [str(a).split("（")[0][:48] for a in alerts[:3]]
    return "> ⚠️ " + "；".join(short)


def _pack_messages(parts: list[str], *, header: str, brief: bool) -> list[str]:
    """按 UTF-8 字节上限分包，避免段 mid-sentence 截断。"""
    max_bytes = WECHAT_MARKDOWN_MAX_BYTES if brief else WECHAT_TEXT_MAX_BYTES
    chunks: list[str] = []
    current = header
    for part in parts:
        if not part or not str(part).strip():
            continue
        candidate = f"{current}\n{part}" if current.strip() else part
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
            continue
        if current.strip():
            chunks.append(truncate_wechat_utf8(current, max_bytes))
        current = part
        if len(current.encode("utf-8")) > max_bytes:
            chunks.append(truncate_wechat_utf8(current, max_bytes))
            current = ""
    if current.strip():
        chunks.append(truncate_wechat_utf8(current, max_bytes))
    if len(chunks) > 1:
        chunks = [f"({i}/{len(chunks)})\n{b}" for i, b in enumerate(chunks, 1)]
    return chunks


def build_etl_digest_messages(stats: dict[str, Any]) -> list[str]:
    style = (stats.get("style") or SENTIMENT_WECHAT_STYLE or "brief").lower()
    if style == "debug":
        return _build_debug_messages(stats)
    return _build_brief_messages(stats)


def _build_brief_messages(stats: dict[str, Any]) -> list[str]:
    inserted = dict(stats.get("inserted") or {})
    if stats.get("from_db"):
        inserted["__from_db__"] = True
    fetched: dict[str, Any] = stats.get("fetched") or {}
    n = max(1, min(SENTIMENT_WECHAT_SAMPLE_PER_SOURCE, 5))

    header = f"### Z-Plan 资讯简报\n> 北京时间 {_now_cn_str()}"
    alert = _alerts_banner(stats)
    if alert:
        header = f"{header}\n{alert}"

    parts: list[str] = []
    news = _section_brief_news(fetched.get("em_financial_flash"), inserted, n)
    if news:
        parts.append(news)

    market = _section_market_brief(fetched)
    if market:
        parts.append(market)

    parts.append("> 群内 @机器人 可问答；发送「帮助」看指令。")
    return _pack_messages(parts, header=header, brief=True)


def _build_debug_messages(stats: dict[str, Any]) -> list[str]:
    inserted = dict(stats.get("inserted") or {})
    fetched: dict[str, Any] = stats.get("fetched") or {}
    if stats.get("from_db"):
        inserted["__from_db__"] = True
    n = max(1, SENTIMENT_WECHAT_SAMPLE_PER_SOURCE)

    header = (
        f"【Z-Plan 多源 ETL 调试】\n"
        f"推送时间: {_now_cn_str()}\n"
        f"说明: 全源样例（SENTIMENT_WECHAT_STYLE=debug）"
    )
    alert = _alerts_banner(stats).replace("> ", "")
    if alert:
        header += f"\n{alert}"

    sections: list[str] = []
    for block in (
        _section_flash(fetched.get("em_financial_flash"), inserted, n, brief=False),
        _section_northbound_daily(fetched.get("em_northbound_daily"), inserted, brief=False),
        _section_margin(fetched.get("em_margin_account"), inserted, brief=False),
        _section_turnover(fetched.get("em_index_turnover"), inserted, brief=False),
        _section_news_optional(
            "newsapi",
            fetched.get("newsapi"),
            inserted,
            n,
            brief=False,
            skip_reason=None if NEWSAPI_KEY else "NEWSAPI_KEY 未配置",
        ),
        _section_news_optional("google_rss", fetched.get("google_rss"), inserted, n, brief=False),
    ):
        if block:
            sections.append(block)
    return _pack_messages(sections, header=header, brief=False)


def _stats_from_db() -> dict[str, Any]:
    """从 SQLite 读取各源最新样例，用于仅推送（无需重跑 ETL）。"""
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

    from agents.news_agent import push_to_wechat

    stats = dict(stats)
    stats.setdefault("style", SENTIMENT_WECHAT_STYLE)
    messages = build_etl_digest_messages(stats)
    ok_count = 0
    for msg in messages:
        if push_to_wechat(msg):
            ok_count += 1
    result = {
        "pushed": ok_count > 0,
        "parts": len(messages),
        "parts_ok": ok_count,
        "style": stats.get("style"),
    }
    logger.info("ETL 微信 digest: %s", result)
    return result


def push_digest_from_db() -> dict[str, Any]:
    """仅根据库内最新数据组稿并推送（跳过重抓）。"""
    stats = _stats_from_db()
    stats["from_db"] = True
    return push_etl_digest_to_wechat(stats)
