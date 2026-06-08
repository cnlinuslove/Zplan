"""/api/v1/market — 行情查询：股票搜索、K线、资讯关联。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import desc, select, func

import logging
from datetime import date
from pathlib import Path

from fastapi.responses import StreamingResponse

from zplan_shared.models import (
    DailyPrice,
    SessionLocal,
    StockConceptMember,
    StockList,
)
from zplan_shared.market import get_bars, resolve_ts_code

logger = logging.getLogger(__name__)
router = APIRouter(tags=["market"])


@router.get("/market/stocks")
async def search_stocks(
    q: str = Query(default="", description="代码或名称关键词"),
    market: str = Query(default="a", description="市场: a/hk"),
    limit: int = Query(default=20, le=100),
):
    """搜索股票（代码或名称）。"""
    db = SessionLocal()
    try:
        stmt = select(StockList).where(StockList.market == market)
        if q:
            stmt = stmt.where(
                (StockList.ts_code.contains(q)) | (StockList.name.contains(q))
            )
        stmt = stmt.limit(limit)
        rows = db.scalars(stmt).all()
        return {
            "ok": True,
            "stocks": [
                {
                    "ts_code": r.ts_code,
                    "name": r.name,
                    "industry": r.industry,
                    "market": r.market,
                    "list_date": r.listing_date.isoformat() if r.listing_date else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.get("/market/stocks/{ts_code}/bars")
async def get_stock_bars(
    ts_code: str,
    days: int = Query(default=120, description="回溯天数", le=500),
    market: str = Query(default="a"),
):
    """获取个股 K 线数据。"""
    try:
        code = resolve_ts_code(ts_code) or ts_code
        df = get_bars(code, lookback=days, market=market)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "ts_code": ts_code}

    if df is None or df.empty:
        return {"ok": True, "ts_code": ts_code, "bars": [], "count": 0}

    # DataFrame → JSON
    bars = []
    for _, row in df.iterrows():
        bars.append({
            "trade_date": str(row.get("trade_date", "")),
            "open": float(row.get("open", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "close": float(row.get("close", 0) or 0),
            "volume": float(row.get("vol", 0) or 0),
            "amount": float(row.get("amount", 0) or 0),
            "pct_chg": float(row.get("pct_chg", 0) or 0) if "pct_chg" in row else None,
        })
    return {"ok": True, "ts_code": ts_code, "bars": bars, "count": len(bars)}


@router.get("/market/stocks/{ts_code}/news")
async def get_stock_news(
    ts_code: str,
    limit: int = Query(default=20, le=100),
    market: str = Query(default="a"),
):
    """获取个股关联资讯（news_stock_link → global_news / financial_alerts）。"""
    from sqlalchemy import text

    db = SessionLocal()
    try:
        # 通过 news_stock_link 查关联的 global_news
        rows = db.execute(
            text(
                """SELECT gn.title, gn.description, gn.source_name, gn.article_url,
                          gn.published_at_utc, nsl.confidence, nsl.event_type
                   FROM news_stock_link nsl
                   JOIN global_news gn ON nsl.news_id = gn.id AND nsl.news_source = 'global_news'
                   WHERE nsl.ts_code = :code AND nsl.market = :market
                   ORDER BY gn.published_at_utc DESC
                   LIMIT :limit"""
            ),
            {"code": ts_code, "market": market, "limit": limit},
        ).fetchall()
        return {
            "ok": True,
            "ts_code": ts_code,
            "news": [
                {
                    "title": r[0],
                    "description": r[1],
                    "source": r[2],
                    "url": r[3],
                    "published_at": str(r[4]) if r[4] else None,
                    "confidence": r[5],
                    "event_type": r[6],
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.get("/market/stocks/{ts_code}/detail")
async def get_stock_detail(ts_code: str, market: str = Query(default="a")):
    """个股综合详情：基本信息 + 所属概念 + 最新行情。"""
    db = SessionLocal()
    try:
        # 基本信息
        stock = db.scalar(
            select(StockList).where(
                StockList.ts_code == ts_code, StockList.market == market
            )
        )
        if not stock:
            return {"ok": False, "error": "stock not found"}

        # 所属概念
        concepts = db.scalars(
            select(StockConceptMember.concept_name).where(
                StockConceptMember.ts_code == ts_code,
                StockConceptMember.market == market,
            )
        ).all()

        # 最新日线
        latest_bar = db.execute(
            select(DailyPrice)
            .where(DailyPrice.ts_code == ts_code, DailyPrice.market == market)
            .order_by(desc(DailyPrice.trade_date))
            .limit(1)
        ).scalar()

        return {
            "ok": True,
            "stock": {
                "ts_code": stock.ts_code,
                "name": stock.name,
                "industry": stock.industry,
                "market": stock.market,
                "list_date": stock.listing_date.isoformat() if stock.listing_date else None,
                "concepts": list(concepts) if concepts else [],
                "latest": {
                    "trade_date": str(latest_bar.trade_date) if latest_bar else None,
                    "close": latest_bar.close if latest_bar else None,
                    "pct_chg": latest_bar.pct_chg if latest_bar else None,
                    "volume": latest_bar.volume if latest_bar else None,
                    "turnover_rate": latest_bar.turnover_rate if latest_bar else None,
                }
                if latest_bar
                else None,
            },
        }
    finally:
        db.close()


@router.get("/market/stocks/{ts_code}/chart")
async def get_stock_chart(
    ts_code: str,
    lookback: int = 120,
    market: str = "a",
):
    """生成个股 K 线 + MACD 趋势图（PNG，生成后缓存复用）。"""
    try:
        from zplan_shared.chart_viz import plot_stock_chart
        import time as _time

        code = resolve_ts_code(ts_code) or ts_code
        output_dir = Path("/tmp/zplan-charts")
        output_dir.mkdir(parents=True, exist_ok=True)

        # 缓存：当天已生成过就直接返回（文件名格式匹配 chart_viz.py）
        today = date.today().strftime("%Y%m%d")
        cache_path = output_dir / f"{code}_{today}_kline.png"
        if cache_path.exists():
            return StreamingResponse(open(cache_path, "rb"), media_type="image/png")

        _t0 = _time.monotonic()
        path = plot_stock_chart(code, lookback=lookback, output_dir=str(output_dir))
        logger.info("Chart generated for %s in %.1fs", code, _time.monotonic() - _t0)
        return StreamingResponse(open(path, "rb"), media_type="image/png")
    except Exception as exc:
        logger.exception("Chart generation failed for %s", ts_code)
        return {"ok": False, "error": str(exc)}


@router.get("/market/concepts")
async def list_concepts(
    q: str = Query(default="", description="概念名称关键词"),
    limit: int = Query(default=50, le=200),
):
    """搜索/列出概念板块（q 为空时返回热门概念，按成份股数量排序）。"""
    db = SessionLocal()
    try:
        if q:
            stmt = (
                select(func.distinct(StockConceptMember.concept_name))
                .where(StockConceptMember.concept_name.contains(q))
                .limit(limit)
            )
        else:
            # 无搜索词时返回热门概念（成份股最多的前 N 个）
            stmt = (
                select(
                    StockConceptMember.concept_name,
                    func.count(StockConceptMember.ts_code).label("cnt"),
                )
                .group_by(StockConceptMember.concept_name)
                .order_by(func.count(StockConceptMember.ts_code).desc())
                .limit(limit)
            )
            rows = db.execute(stmt).all()
            return {
                "ok": True,
                "concepts": [{"name": r[0], "stock_count": r[1]} for r in rows if r[0]],
            }

        names = db.scalars(stmt).all()
        return {"ok": True, "concepts": [{"name": n} for n in names if n]}
    finally:
        db.close()


@router.get("/market/concepts/{concept_name}/stocks")
async def get_concept_stocks(concept_name: str, market: str = Query(default="a")):
    """获取概念板块成份股。"""
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(StockConceptMember).where(
                StockConceptMember.concept_name == concept_name,
                StockConceptMember.market == market,
            )
        ).all()
        return {
            "ok": True,
            "concept": concept_name,
            "stocks": [
                {"ts_code": r.ts_code, "name": r.name} for r in rows
            ],
        }
    finally:
        db.close()
