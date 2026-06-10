"""/api/v1/forecast — 大盘预测查询。"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Query
from sqlalchemy import desc, select, text

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.models import MarketForecast, SessionLocal

router = APIRouter(tags=["forecast"])


def _chart_path_to_url(path: str | None) -> str | None:
    """将绝对文件路径转为 /charts/ 相对 URL。"""
    if not path:
        return None
    charts_dir = Path(ZPLAN_ROOT) / "charts"
    try:
        rel = Path(path).relative_to(charts_dir)
        return f"/charts/{rel.as_posix()}"
    except ValueError:
        return None


@router.get("/forecast/latest")
async def get_latest_forecast():
    """获取最新大盘预测（含证据链 + 验证状态）。"""
    db = SessionLocal()
    try:
        mf = db.execute(
            select(MarketForecast)
            .order_by(desc(MarketForecast.as_of_date))
            .limit(1)
        ).scalars().first()

        if not mf:
            return {"ok": True, "forecast": None}

        # 解析 JSON
        try:
            forecast_data = json.loads(mf.forecast_json) if isinstance(mf.forecast_json, str) else mf.forecast_json
        except (json.JSONDecodeError, TypeError):
            forecast_data = None

        # 解析图表路径并转为 Web URL
        try:
            raw_charts = json.loads(mf.index_charts_json) if mf.index_charts_json else None
        except (json.JSONDecodeError, TypeError):
            raw_charts = None

        parsed_charts = None
        if raw_charts:
            parsed_charts = {}
            for code, paths in raw_charts.items():
                if isinstance(paths, dict):
                    parsed_charts[code] = {k: _chart_path_to_url(v) for k, v in paths.items()}
                else:
                    parsed_charts[code] = _chart_path_to_url(paths)

        return {
            "ok": True,
            "forecast": {
                "id": mf.id,
                "as_of_date": str(mf.as_of_date),
                "market_direction": mf.market_direction,
                "direction_confidence": mf.direction_confidence,
                "forecast_data": forecast_data,
                "charts": parsed_charts,
                "push_sent": mf.push_sent,
                "verified": {
                    "at": mf.verified_at.isoformat() if mf.verified_at else None,
                    "actual_direction": mf.actual_direction,
                    "actual_pct_chg": mf.actual_pct_chg,
                    "direction_correct": mf.direction_correct,
                } if mf.verified_at else None,
                "created_at": mf.created_at_utc.isoformat() if mf.created_at_utc else None,
            },
        }
    finally:
        db.close()


@router.get("/forecast/history")
async def get_forecast_history(days: int = Query(default=30, le=365)):
    """最近 N 天的预测历史（含验证结果 + 多维度统计）。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            select(MarketForecast)
            .where(MarketForecast.verified_at.isnot(None))
            .order_by(desc(MarketForecast.as_of_date))
            .limit(days)
        ).scalars().all()

        history = []
        correct_count = 0
        dir_stats: dict[str, dict[str, int]] = {}
        for r in rows:
            is_correct = r.direction_correct
            if is_correct:
                correct_count += 1
            history.append({
                "as_of_date": str(r.as_of_date),
                "predicted": r.market_direction,
                "actual": r.actual_direction,
                "actual_pct": r.actual_pct_chg,
                "correct": is_correct,
                "confidence": r.direction_confidence,
            })
            # 方向分类统计
            d = r.market_direction or "unknown"
            if d not in dir_stats:
                dir_stats[d] = {"count": 0, "correct": 0}
            dir_stats[d]["count"] += 1
            if is_correct:
                dir_stats[d]["correct"] += 1

        total = len(history)
        accuracy = round(correct_count / total * 100, 1) if total > 0 else None

        # 方向对称性检查
        by_direction = {}
        for d, v in dir_stats.items():
            by_direction[f"{d}_count"] = v["count"]
            by_direction[f"{d}_correct"] = v["correct"]
            by_direction[f"{d}_accuracy"] = round(v["correct"] / v["count"] * 100, 1) if v["count"] > 0 else None

        # 置信度分桶校准（基于已验证记录）
        bins = [
            (0, 50, "0-50%"),
            (50, 65, "50-65%"),
            (65, 80, "65-80%"),
            (80, 101, "80-100%"),
        ]
        confidence_calibration = []
        for lo, hi, label in bins:
            in_bin = [r for r in rows if lo <= (r.direction_confidence or 0) < hi]
            acc = sum(1 for r in in_bin if r.direction_correct)
            pct = round(acc / len(in_bin) * 100, 1) if in_bin else None
            confidence_calibration.append({
                "bin": label,
                "count": len(in_bin),
                "accurate": acc,
                "accuracy_pct": pct,
                "bin_expected": (lo + hi) / 2,
            })

        # 各指数准确率（从 forecast_evals 表读取）
        per_index_accuracy: dict[str, dict] = {}
        try:
            from zplan_shared.models import ForecastEval
            evals = db.execute(
                select(ForecastEval)
                .where(ForecastEval.horizon_days == 1)
                .order_by(ForecastEval.forecast_id.desc())
                .limit(days * 7)  # 最多 7 indices × N days
            ).scalars().all()
            for fe in evals:
                if not fe.index_results_json:
                    continue
                try:
                    ir = json.loads(fe.index_results_json)
                    for ix in ir:
                        code = ix["code"]
                        if code not in per_index_accuracy:
                            per_index_accuracy[code] = {"correct": 0, "total": 0}
                        per_index_accuracy[code]["total"] += 1
                        if ix.get("correct"):
                            per_index_accuracy[code]["correct"] += 1
                except (json.JSONDecodeError, TypeError):
                    pass
            for v in per_index_accuracy.values():
                v["pct"] = round(v["correct"] / v["total"] * 100, 1) if v["total"] > 0 else None
        except Exception:
            pass  # forecast_evals 表可能尚不存在

        return {
            "ok": True,
            "history": history,
            "stats": {
                "total_verified": total,
                "correct": correct_count,
                "accuracy_pct": accuracy,
                "by_direction": by_direction,
                "confidence_calibration": confidence_calibration,
                "per_index_accuracy": per_index_accuracy if per_index_accuracy else None,
            },
        }
    finally:
        db.close()


@router.get("/forecast/index-chart/{index_code}")
async def get_index_chart_path(index_code: str):
    """获取某个指数的最新图表路径（供前端渲染）。"""
    db = SessionLocal()
    try:
        mf = db.execute(
            select(MarketForecast)
            .order_by(desc(MarketForecast.as_of_date))
            .limit(1)
        ).scalars().first()

        if not mf or not mf.index_charts_json:
            return {"ok": False, "error": "no charts available"}

        try:
            charts = json.loads(mf.index_charts_json)
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "error": "chart json parse error"}

        index_charts = charts.get(index_code)
        if not index_charts:
            return {"ok": False, "error": f"no chart for index {index_code}"}

        # 路径转 URL
        url_charts = {}
        if isinstance(index_charts, dict):
            url_charts = {k: _chart_path_to_url(v) for k, v in index_charts.items()}
        else:
            url_charts = _chart_path_to_url(index_charts)

        return {
            "ok": True,
            "index_code": index_code,
            "charts": url_charts,
        }
    finally:
        db.close()
