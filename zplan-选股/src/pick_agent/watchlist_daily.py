"""持仓订阅：同步行情 + 每日 LLM 简报。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.llm.gemini import gemini_available
from zplan_shared.news_linker import link_unlinked_news
from zplan_shared.pick_watchlist import list_watch, touch_brief, touch_sync, watch_codes
from zplan_shared.symbol_sync import sync_symbols_market_data

from pick_agent.llm_research import brief_review_scan_picks
from pick_agent.pick_store_ext import save_watchlist_daily_run
from pick_agent.scanner import _load_stock_meta
from pick_agent.strategy import PickStrategy, load_strategy
from pick_agent.technical import analyze_technical

logger = logging.getLogger(__name__)


def _build_pick_rows(codes: list[str], strat: PickStrategy) -> list[dict[str, Any]]:
    meta = _load_stock_meta()
    name_map = dict(zip(meta["ts_code"], meta["name"].fillna("")))
    picks: list[dict[str, Any]] = []
    for code in codes:
        tech = analyze_technical(code, min_bars=strat.min_bars)
        if tech.bars < strat.min_bars:
            picks.append(
                {
                    "ts_code": code,
                    "name": name_map.get(code),
                    "tech_score": 0,
                    "verdict": "数据不足",
                    "signals": tech.signals,
                    "composite_score": 0,
                }
            )
            continue
        picks.append(
            {
                "ts_code": code,
                "name": name_map.get(code),
                "close": tech.close,
                "tech_score": tech.score,
                "verdict": tech.verdict,
                "signals": tech.signals,
                "kdj_k": tech.features.get("kdj_k"),
                "kdj_d": tech.features.get("kdj_d"),
                "ret_20d": tech.features.get("ret_20d"),
                "composite_score": tech.score,
                "rule_composite_score": tech.score,
            }
        )
    return picks


def _format_digest_md(
    trade_date: str | None,
    picks: list[dict[str, Any]],
    *,
    sync_summary: dict[str, Any],
    news_links: dict[str, int],
    run_id: int | None,
) -> str:
    today = date.today().isoformat()
    lines = [
        f"# 持仓每日简报 {today}",
        "",
        f"> 行情截止：{trade_date or '—'} | run_id={run_id or '—'} | "
        f"同步成功 {sync_summary.get('ok', 0)}/{sync_summary.get('ok', 0) + sync_summary.get('fail', 0)}",
        "",
    ]
    if news_links:
        lines.append(f"资讯补链：{news_links.get('links_upserted', 0)} 条关联")
        lines.append("")

    for i, p in enumerate(picks, 1):
        brief = p.get("llm_brief") or {}
        lines.extend(
            [
                f"## {i}. {p.get('name') or '—'}（{p['ts_code']}）",
                "",
                f"- **综合分**：{p.get('composite_score', '—')} "
                f"（技术 {p.get('tech_score', '—')}）",
                f"- **操作建议**：{brief.get('recommendation') or p.get('verdict') or '—'}",
                f"- **收盘**：{p.get('close', '—')}",
                "",
                brief.get("trend") or "（无走势简评）",
                "",
            ]
        )
        if p.get("signals"):
            lines.append("**规则信号**：" + "；".join(p["signals"][:5]))
            lines.append("")

    lines.append("---")
    lines.append(f"生成时间 UTC：{datetime.utcnow().isoformat()}Z")
    return "\n".join(lines)


def run_watchlist_daily(
    *,
    strategy: PickStrategy | None = None,
    skip_sync: bool = False,
    skip_news_link: bool = False,
    use_llm: bool = True,
    include_intraday: bool = True,
    persist: bool = True,
    write_digest_file: bool = True,
) -> dict[str, Any]:
    """持仓订阅每日任务：更新行情 → 补资讯链 → 规则+LLM 简报 → 入库。"""
    strat = strategy or load_strategy()
    items = list_watch(enabled_only=True)
    codes = [w["ts_code"] for w in items]
    if not codes:
        return {"ok": False, "message": "持仓订阅为空，请先：main.py watch add 股票名"}

    sync_summary: dict[str, Any] = {"ok": 0, "fail": 0, "skipped": skip_sync}
    if not skip_sync:
        sync_summary = sync_symbols_market_data(codes, include_intraday=include_intraday)
        touch_sync(codes)

    news_stats: dict[str, int] = {}
    if not skip_news_link:
        try:
            news_stats = link_unlinked_news(hours=48, limit_per_table=300)
        except Exception as exc:
            logger.warning("资讯补链失败: %s", exc)
            news_stats = {"error": str(exc)}

    picks = _build_pick_rows(codes, strat)
    llm_usage = None
    if use_llm and gemini_available() and strat.llm_enabled:
        picks, llm_usage = brief_review_scan_picks(
            picks,
            as_of=None,
            model=strat.llm_model,
        )
    elif use_llm and not gemini_available():
        logger.warning("DEEPSEEK_API_KEY 未配置，仅输出规则分")

    from zplan_shared.market import latest_trade_date

    as_of = latest_trade_date()
    run_id: int | None = None
    digest_md = _format_digest_md(
        str(as_of) if as_of else None,
        picks,
        sync_summary=sync_summary,
        news_links=news_stats,
        run_id=None,
    )

    if persist:
        run_id = save_watchlist_daily_run(
            picks,
            as_of=str(as_of) if as_of else None,
            rule_version=strat.rule_version,
            llm_enabled=use_llm and gemini_available(),
            llm_model=strat.llm_model,
            sync_summary=sync_summary,
            news_stats=news_stats,
            llm_usage=llm_usage,
            markdown=digest_md,
        )
        touch_brief(codes)
        digest_md = _format_digest_md(
            str(as_of) if as_of else None,
            picks,
            sync_summary=sync_summary,
            news_links=news_stats,
            run_id=run_id,
        )

    digest_path: str | None = None
    if write_digest_file:
        out_dir = Path(ZPLAN_ROOT) / "pick_digest"
        out_dir.mkdir(parents=True, exist_ok=True)
        digest_path = str(out_dir / f"{date.today().isoformat()}.md")
        Path(digest_path).write_text(digest_md, encoding="utf-8")

    return {
        "ok": True,
        "watchlist_count": len(codes),
        "as_of": str(as_of) if as_of else None,
        "run_id": run_id,
        "digest_path": digest_path,
        "markdown": digest_md,
        "sync": sync_summary,
        "news": news_stats,
        "llm_usage": llm_usage,
        "picks": picks,
    }
