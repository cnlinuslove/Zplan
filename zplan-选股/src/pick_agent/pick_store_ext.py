"""pick_store 扩展：持仓每日简报入库。"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from zplan_shared.models import PickEntry, PickRun, SessionLocal, init_db
from zplan_shared.market import next_trading_day
from zplan_shared.pick_store import _entry_from_pick


def save_watchlist_daily_run(
    picks: list[dict[str, Any]],
    *,
    as_of: str | None,
    rule_version: str,
    llm_enabled: bool,
    llm_model: str | None,
    sync_summary: dict[str, Any],
    news_stats: dict[str, int],
    llm_usage: dict[str, Any] | None,
    markdown: str,
) -> int:
    init_db()
    as_of_d = date.fromisoformat(str(as_of)[:10]) if as_of else None
    summary = {
        "as_of": as_of,
        "watchlist_count": len(picks),
        "sync": sync_summary,
        "news": news_stats,
        "llm_usage": llm_usage,
        "digest_markdown": markdown,
    }
    with SessionLocal() as session:
        run = PickRun(
            run_kind="watchlist_daily",
            trade_date_as_of=as_of_d,
            trade_date=next_trading_day(as_of_d) if as_of_d else None,
            rule_version=rule_version,
            llm_enabled=llm_enabled,
            llm_model=llm_model,
            symbol_query="watchlist",
            params_json=json.dumps({"source": "pick_watchlist"}, ensure_ascii=False),
            summary_json=json.dumps(summary, ensure_ascii=False, default=str),
        )
        session.add(run)
        session.flush()
        for i, p in enumerate(picks, start=1):
            row = _entry_from_pick(p, rank=i)
            session.add(PickEntry(run_id=run.id, markdown=None, report_json=None, **row))
        session.commit()
        return int(run.id)
