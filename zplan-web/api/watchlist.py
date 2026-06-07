"""/api/v1/watchlist — 自选股管理。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from zplan_shared.models import PickWatchlist, SessionLocal, StockList

router = APIRouter(tags=["watchlist"])


class WatchlistAddRequest(BaseModel):
    ts_code: str
    market: str = "a"
    notes: str | None = None


@router.get("/watchlist")
async def get_watchlist(market: str = Query(default="a")):
    """获取所有自选股。"""
    db = SessionLocal()
    try:
        rows = db.scalars(select(PickWatchlist).where(PickWatchlist.market == market)).all()
        # enrich with latest stock_list info
        codes = [r.ts_code for r in rows]
        stocks = {}
        if codes:
            stock_rows = db.scalars(
                select(StockList).where(
                    StockList.ts_code.in_(codes), StockList.market == market
                )
            ).all()
            stocks = {s.ts_code: s for s in stock_rows}

        return {
            "ok": True,
            "items": [
                {
                    "ts_code": r.ts_code,
                    "name": stocks[r.ts_code].name if r.ts_code in stocks else r.ts_code,
                    "industry": stocks[r.ts_code].industry if r.ts_code in stocks else None,
                    "notes": r.note,
                    "enabled": r.enabled,
                    "created_at": r.created_at_utc.isoformat() if r.created_at_utc else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.post("/watchlist")
async def add_to_watchlist(req: WatchlistAddRequest):
    """添加自选股。"""
    db = SessionLocal()
    try:
        existing = db.scalar(
            select(PickWatchlist).where(
                PickWatchlist.ts_code == req.ts_code,
                PickWatchlist.market == req.market,
            )
        )
        if existing:
            if req.notes:
                existing.note = req.notes
                existing.enabled = True
                db.commit()
            return {"ok": True, "action": "already_exists", "ts_code": req.ts_code}

        db.add(
            PickWatchlist(
                ts_code=req.ts_code,
                market=req.market,
                note=req.notes,
                enabled=True,
            )
        )
        db.commit()
        return {"ok": True, "action": "added", "ts_code": req.ts_code}
    finally:
        db.close()


@router.delete("/watchlist/{ts_code}")
async def remove_from_watchlist(ts_code: str, market: str = Query(default="a")):
    """移除自选股。"""
    db = SessionLocal()
    try:
        item = db.scalar(
            select(PickWatchlist).where(
                PickWatchlist.ts_code == ts_code,
                PickWatchlist.market == market,
            )
        )
        if item:
            db.delete(item)
            db.commit()
            return {"ok": True, "action": "removed", "ts_code": ts_code}
        return {"ok": False, "error": "not found"}
    finally:
        db.close()
