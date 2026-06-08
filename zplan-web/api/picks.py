"""/api/v1/picks — 选股运行、榜单、触发扫描。"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel
from sqlalchemy import desc, select, func

import math

from zplan_shared.models import (
    PickEntry,
    PickRun,
    PickWatchlist,
    SessionLocal,
    StockList,
)


def _safe_float(v: float | None) -> float | None:
    """将 NaN/Inf 转换为 None，避免 JSON 序列化报错。"""
    if v is None:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _safe_entry(e: PickEntry) -> dict:
    """将 PickEntry 转为安全 dict（NaN → None）。"""
    return {
        "id": e.id,
        "ts_code": e.ts_code,
        "name": e.name,
        "rank": e.rank_in_run,
        "close_price": _safe_float(e.close_price),
        "rule_composite_score": _safe_float(e.rule_composite_score),
        "llm_composite_score": _safe_float(e.llm_composite_score),
        "final_composite_score": _safe_float(e.final_composite_score),
        "recommendation": e.recommendation,
        "verdict": e.verdict,
        "predicted_buy_price": _safe_float(e.predicted_buy_price),
        "predicted_target_price": _safe_float(e.predicted_target_price),
        "predicted_stop_loss": _safe_float(e.predicted_stop_loss),
        "report_json": json.loads(e.report_json) if e.report_json else None,
        "markdown_report": e.markdown,
        "analysis_json": json.loads(e.analysis_process_json) if e.analysis_process_json else None,
    }

logger = logging.getLogger(__name__)
router = APIRouter(tags=["picks"])

# 内存中的后台任务状态
_active_tasks: dict[str, dict[str, Any]] = {}


class ScanRequest(BaseModel):
    """触发全市场扫描。"""
    top_n: int = 300
    market: str = "a"


class AnalyzeRequest(BaseModel):
    """对单股做深度研报。"""
    ts_code: str
    market: str = "a"


# ── 选股运行列表 ──


@router.get("/picks/runs")
async def list_pick_runs(
    run_kind: str | None = Query(default=None, description="scan|llm_top300|report"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
):
    """列出历次选股运行。"""
    db = SessionLocal()
    try:
        stmt = select(PickRun).order_by(desc(PickRun.created_at_utc))
        if run_kind:
            stmt = stmt.where(PickRun.run_kind == run_kind)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = db.scalars(stmt.offset(offset).limit(limit)).all()
        return {
            "ok": True,
            "total": total,
            "runs": [
                {
                    "id": r.id,
                    "run_kind": r.run_kind,
                    "trade_date": r.trade_date_as_of.isoformat() if r.trade_date_as_of else None,
                    "rule_version": r.rule_version,
                    "llm_enabled": r.llm_enabled,
                    "llm_model": r.llm_model,
                    "summary_json": json.loads(r.summary_json) if r.summary_json else None,
                    "created_at": r.created_at_utc.isoformat() if r.created_at_utc else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.get("/picks/runs/{run_id}")
async def get_pick_run(run_id: int):
    """获取单次运行及其所有 entries（榜单）。"""
    db = SessionLocal()
    try:
        run = db.scalar(select(PickRun).where(PickRun.id == run_id))
        if not run:
            return {"ok": False, "error": "run not found"}

        entries = db.scalars(
            select(PickEntry)
            .where(PickEntry.run_id == run_id)
            .order_by(PickEntry.rank_in_run)
        ).all()

        # 计算 entry count
        entry_count = len(entries)

        return {
            "ok": True,
            "run": {
                "id": run.id,
                "run_kind": run.run_kind,
                "trade_date": run.trade_date_as_of.isoformat() if run.trade_date_as_of else None,
                "rule_version": run.rule_version,
                "llm_enabled": run.llm_enabled,
                "entry_count": entry_count,
                "created_at": run.created_at_utc.isoformat() if run.created_at_utc else None,
            },
            "entries": [_safe_entry(e) for e in entries],
        }
    finally:
        db.close()


@router.get("/picks/entries/{entry_id}")
async def get_pick_entry(entry_id: int):
    """获取单个选股条目详情（含完整分析 JSON）。"""
    db = SessionLocal()
    try:
        e = db.scalar(select(PickEntry).where(PickEntry.id == entry_id))
        if not e:
            return {"ok": False, "error": "entry not found"}

        # 获取回测结果
        from zplan_shared.models import PickPredictionOutcome

        outcomes = db.scalars(
            select(PickPredictionOutcome).where(
                PickPredictionOutcome.entry_id == entry_id
            )
        ).all()

        return {
            "ok": True,
            "entry": {
                "id": e.id,
                "ts_code": e.ts_code,
                "name": e.name,
                "rank": e.rank_in_run,
                "close_price": e.close_price,
                "rule_composite_score": e.rule_composite_score,
                "llm_composite_score": e.llm_composite_score,
                "final_composite_score": e.final_composite_score,
                "recommendation": e.recommendation,
                "verdict": e.verdict,
                "predicted_buy_price": e.predicted_buy_price,
                "predicted_target_price": e.predicted_target_price,
                "predicted_stop_loss": e.predicted_stop_loss,
                "report_json": json.loads(e.report_json)
                if e.report_json
                else None,
                "markdown_report": e.markdown,
            },
            "backtest_outcomes": [
                {
                    "horizon_days": o.horizon_days,
                    "return_pct": o.return_pct,
                    "hit_buy": o.hit_buy,
                    "hit_target": o.hit_target,
                    "hit_stop": o.hit_stop,
                }
                for o in outcomes
            ],
        }
    finally:
        db.close()


@router.get("/picks/latest")
async def get_latest_picks(
    run_kind: str = Query(default="scan", description="scan|llm_top300"),
    top_n: int = Query(default=30, le=300),
):
    """获取最新选股的 Top N 榜单（优先取条目数足够的最近 run）。"""
    db = SessionLocal()
    try:
        # 找最近几个 run，选条目数 >= top_n 的
        candidates = db.scalars(
            select(PickRun)
            .where(PickRun.run_kind == run_kind)
            .order_by(desc(PickRun.created_at_utc))
            .limit(20)
        ).all()

        run = None
        for r in candidates:
            cnt = db.scalar(
                select(func.count()).select_from(PickEntry).where(PickEntry.run_id == r.id)
            )
            if cnt >= top_n:
                run = r
                break
        # fallback: 用条目最多的 run
        if not run and candidates:
            best = None
            best_cnt = 0
            for r in candidates:
                cnt = db.scalar(
                    select(func.count()).select_from(PickEntry).where(PickEntry.run_id == r.id)
                )
                if cnt > best_cnt:
                    best_cnt = cnt
                    best = r
            run = best

        if not run:
            return {"ok": False, "error": "no pick run found"}

        entries = db.scalars(
            select(PickEntry)
            .where(PickEntry.run_id == run.id)
            .order_by(PickEntry.rank_in_run)
            .limit(top_n)
        ).all()

        return {
            "ok": True,
            "run_id": run.id,
            "trade_date": run.trade_date_as_of.isoformat() if run.trade_date_as_of else None,
            "rule_version": run.rule_version,
            "entries": [
                {
                    "id": e.id,
                    "ts_code": e.ts_code,
                    "name": e.name,
                    "rank": e.rank_in_run,
                    "close_price": e.close_price,
                    "rule_composite_score": e.rule_composite_score,
                    "llm_composite_score": e.llm_composite_score,
                    "final_composite_score": e.final_composite_score,
                    "recommendation": e.recommendation,
                    "verdict": e.verdict,
                    "predicted_buy_price": e.predicted_buy_price,
                    "predicted_target_price": e.predicted_target_price,
                }
                for e in entries
            ],
        }
    finally:
        db.close()


# ── 后台任务：触发扫描 ──


@router.post("/picks/scan")
async def trigger_scan(req: ScanRequest, bg: BackgroundTasks):
    """触发全市场扫描（后台执行）。"""
    import uuid

    task_id = str(uuid.uuid4())[:8]
    _active_tasks[task_id] = {"status": "queued", "progress": 0, "message": "等待执行..."}

    bg.add_task(_run_scan, task_id, req.top_n, req.market)

    return {"ok": True, "task_id": task_id, "status": "queued"}


def _run_scan(task_id: str, top_n: int, market: str) -> None:
    """后台执行选股扫描。"""
    import time
    import sys
    from pathlib import Path

    _active_tasks[task_id] = {"status": "running", "progress": 10, "message": "扫描中..."}
    t0 = time.monotonic()

    try:
        # 直接调 pick_agent 的扫描函数
        pick_src = str(Path(__file__).resolve().parents[2] / "zplan-选股" / "src")
        if pick_src not in sys.path:
            sys.path.insert(0, pick_src)

        from pick_agent.scanner import scan_universe
        from pick_agent.strategy import load_strategy

        strategy = load_strategy()
        entries = scan_universe(strategy=strategy, market=market)

        _active_tasks[task_id] = {
            "status": "running",
            "progress": 80,
            "message": f"扫描完成 {len(entries)} 只，写入数据库...",
        }

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _active_tasks[task_id] = {
            "status": "completed",
            "progress": 100,
            "message": f"完成：{len(entries)} 只标的",
            "result": {"count": len(entries), "elapsed_ms": elapsed_ms},
        }
    except Exception as exc:
        logger.exception("扫描失败 %s", task_id)
        _active_tasks[task_id] = {
            "status": "failed",
            "progress": 0,
            "message": str(exc),
        }


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """查询后台任务状态。"""
    task = _active_tasks.get(task_id)
    if not task:
        return {"ok": False, "error": "task not found"}
    return {"ok": True, "task_id": task_id, **task}
