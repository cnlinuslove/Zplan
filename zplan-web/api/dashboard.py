"""/api/v1/dashboard — 统计仪表盘。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select, text

from zplan_shared.models import PickEntry, PickRun, SessionLocal

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/stats")
async def get_stats():
    """聚合统计数据。"""
    db = SessionLocal()
    try:
        total_runs = db.scalar(select(func.count(PickRun.id)))
        total_entries = db.scalar(select(func.count(PickEntry.id)))

        # 最近一次运行
        latest_run = db.scalar(
            select(PickRun).order_by(desc(PickRun.created_at_utc)).limit(1)
        )

        # LLM 总成本（从 web_chat_messages 累加）
        try:
            cost_row = db.execute(
                text("SELECT COALESCE(SUM(cost_usd), 0) FROM web_chat_messages")
            ).scalar()
        except Exception:
            cost_row = 0.0

        return {
            "ok": True,
            "stats": {
                "total_runs": total_runs,
                "total_entries": total_entries,
                "total_llm_cost_usd": round(float(cost_row or 0), 4),
                "latest_run": {
                    "id": latest_run.id,
                    "run_kind": latest_run.run_kind,
                    "trade_date": latest_run.trade_date_as_of.isoformat()
                    if latest_run.trade_date_as_of
                    else None,
                    "created_at": latest_run.created_at_utc.isoformat()
                    if latest_run.created_at_utc
                    else None,
                }
                if latest_run
                else None,
            },
        }
    finally:
        db.close()


@router.get("/dashboard/llm-costs")
async def get_llm_costs(days: int = Query(default=30, le=365)):
    """LLM 成本趋势（按日聚合）。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """SELECT DATE(created_at_utc) as dt,
                          COUNT(*) as reqs,
                          COALESCE(SUM(prompt_tokens), 0) as prompt_tok,
                          COALESCE(SUM(output_tokens), 0) as output_tok,
                          COALESCE(SUM(cost_usd), 0) as cost
                   FROM web_chat_messages
                   WHERE created_at_utc >= DATE('now', :days)
                   GROUP BY dt
                   ORDER BY dt DESC"""
            ),
            {"days": f"-{days} days"},
        ).fetchall()

        return {
            "ok": True,
            "daily": [
                {
                    "date": str(r[0]),
                    "requests": r[1],
                    "prompt_tokens": r[2],
                    "output_tokens": r[3],
                    "cost_usd": round(r[4], 4),
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.get("/dashboard/pipeline")
async def get_pipeline_status():
    """数据管道状态：最新行情日期、ETL 执行情况。"""
    db = SessionLocal()
    try:
        # 最新行情日期
        latest_price = db.execute(
            text("SELECT MAX(trade_date) FROM daily_prices WHERE market='a'")
        ).scalar()

        # 最新快照日期
        latest_snapshot = db.execute(
            text("SELECT MAX(trade_date) FROM daily_snapshot WHERE market='a'")
        ).scalar()

        # 最新选股运行
        latest_run = db.scalar(
            select(PickRun.run_kind, PickRun.created_at_utc).order_by(
                desc(PickRun.created_at_utc)
            ).limit(1)
        )

        return {
            "ok": True,
            "pipeline": {
                "latest_price_date": str(latest_price) if latest_price else None,
                "latest_snapshot_date": str(latest_snapshot) if latest_snapshot else None,
                "latest_pick_run": {
                    "kind": latest_run[0] if latest_run else None,
                    "at": latest_run[1].isoformat() if latest_run and latest_run[1] else None,
                },
            },
        }
    finally:
        db.close()
