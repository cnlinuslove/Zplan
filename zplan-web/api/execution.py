"""/api/v1/execution — 执行计划看板：今日操作清单、状态快照、价位监控。"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter(tags=["execution"])

BEIJING_TZ = timezone(timedelta(hours=8))
SNAPSHOT_DIR = "/tmp/zplan-execution"


def _snapshot_path(date_str: str) -> Path:
    return Path(SNAPSHOT_DIR) / f"execution_{date_str}.json"


def _load_today_snapshot() -> dict | None:
    """加载今日执行计划快照。"""
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    p = _snapshot_path(today)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


@router.get("/execution/today")
async def get_today_execution():
    """获取今日执行计划（含各阶段快照）。"""
    snapshot = _load_today_snapshot()
    if not snapshot:
        return {"ok": True, "has_data": False, "message": "今日暂无执行计划数据"}

    # 汇总统计
    plans = snapshot if isinstance(snapshot, list) else snapshot.get("plans", [])
    buy_count = sum(1 for p in plans if p.get("open_action") == "BUY_AT_OPEN")
    pullback_count = sum(1 for p in plans if p.get("open_action") == "BUY_ON_PULLBACK")
    wait_count = sum(1 for p in plans if p.get("open_action") == "WAIT_OBSERVE")
    skip_count = sum(1 for p in plans if p.get("open_action") == "SKIP_TODAY")
    hit_target = sum(1 for p in plans if p.get("hit_target"))
    hit_stop = sum(1 for p in plans if p.get("hit_stop"))

    return {
        "ok": True,
        "has_data": True,
        "summary": {
            "total": len(plans),
            "buy_now": buy_count,
            "buy_on_pullback": pullback_count,
            "wait": wait_count,
            "skip": skip_count,
            "hit_target": hit_target,
            "hit_stop": hit_stop,
        },
        "plans": plans,
    }


@router.get("/execution/status")
async def get_execution_status():
    """获取执行层各阶段运行状态。"""
    beijing_now = datetime.now(BEIJING_TZ)
    today_str = beijing_now.strftime("%Y-%m-%d")
    time_str = beijing_now.strftime("%H:%M")
    weekday = beijing_now.weekday()

    snapshot = _load_today_snapshot()
    has_snapshot = snapshot is not None

    # 判断各阶段是否已执行
    stages = [
        {
            "key": "pre_market",
            "label": "盘前检查",
            "time": "8:28",
            "done": any(p.get("overnight_adjustment") is not None or p.get("adjusted_buy") is not None
                      for p in (snapshot or [])),
        },
        {
            "key": "auction",
            "label": "集合竞价",
            "time": "9:25",
            "done": any(p.get("auction_price") is not None for p in (snapshot or [])),
        },
        {
            "key": "opening",
            "label": "开盘决策",
            "time": "9:30",
            "done": any(p.get("open_action") for p in (snapshot or [])),
        },
        {
            "key": "intraday",
            "label": "盘中监控",
            "time": "交易时段",
            "done": any(p.get("current_price") is not None for p in (snapshot or [])),
        },
    ]

    return {
        "ok": True,
        "date": today_str,
        "time": time_str,
        "weekday": weekday,
        "is_weekend": weekday >= 5,
        "has_snapshot": has_snapshot,
        "stages": stages,
    }


@router.get("/execution/calendar")
async def get_execution_calendar(
    days: int = Query(default=5, le=30),
):
    """查看最近 N 天的执行记录（哪些天有快照）。"""
    d = Path(SNAPSHOT_DIR)
    if not d.exists():
        return {"ok": True, "days": []}

    records = []
    for f in sorted(d.glob("execution_*.json"), reverse=True)[:days]:
        date_str = f.stem.replace("execution_", "")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            plans = data if isinstance(data, list) else data.get("plans", [])
            buy_count = sum(1 for p in plans if p.get("open_action") == "BUY_AT_OPEN")
            hit_target = sum(1 for p in plans if p.get("hit_target"))
            records.append({
                "date": date_str,
                "total_picks": len(plans),
                "buy_signals": buy_count,
                "hit_target": hit_target,
            })
        except Exception:
            records.append({"date": date_str, "error": "解析失败"})

    return {"ok": True, "days": records}
