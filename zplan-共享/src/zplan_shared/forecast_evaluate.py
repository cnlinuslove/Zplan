"""大盘预测多周期评估与置信度校准。

提供:
- evaluate_forecast() - 单条预测多 horizon 评估
- save_forecast_evals() - 批量评估并落库
- forecast_calibration_summary() - 聚合置信度校准
- forecast_accuracy_trend() - 滚动准确率趋势
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select

from zplan_shared.market import get_index_bars
from zplan_shared.models import MarketForecast, ForecastEval, SessionLocal, init_db

_A_INDEX_CODES = ["000001", "399001", "399006", "000688", "000300", "000905", "000852"]


def _resolve_direction(pct_chg: float, threshold: float) -> str:
    if pct_chg > threshold:
        return "bullish"
    elif pct_chg < -threshold:
        return "bearish"
    return "range-bound"


def _resolve_direction_cn(pct_chg: float, threshold: float) -> str:
    if pct_chg > threshold:
        return "偏多"
    elif pct_chg < -threshold:
        return "偏空"
    return "震荡"


def _fwd_change_for_code(code: str, horizon_days: int, as_of_date: date) -> float:
    """计算单指数 forward horizon 涨跌幅。返回 pct_chg，不足则 NaN。"""
    import datetime as dt
    bars = get_index_bars(
        code,
        start=as_of_date,
        end=as_of_date + dt.timedelta(days=horizon_days * 3 + 10),
    )
    if bars.empty:
        return float("nan")
    bars = bars.sort_index()
    # Find first row at or after as_of_date
    # Convert index to a uniform type for comparison
    ix_dates = pd.to_datetime(bars.index).date if hasattr(pd.to_datetime(bars.index), 'date') else pd.to_datetime(bars.index)
    mask = [d >= as_of_date for d in ix_dates]
    if not any(mask):
        return float("nan")
    pos = mask.index(True)
    if pos + horizon_days >= len(bars):
        return float("nan")
    start_close = float(bars.iloc[pos]["close"])
    end_close = float(bars.iloc[pos + horizon_days]["close"])
    return round((end_close - start_close) / start_close * 100, 2)


def evaluate_forecast(
    mf: MarketForecast,
    horizon_days: int = 1,
    threshold: float = 0.3,
) -> dict[str, Any] | None:
    """评估单条预测在指定 horizon 上的表现。

    返回 None 表示 forward 数据不足。
    """
    try:
        forecast = json.loads(mf.forecast_json) if isinstance(mf.forecast_json, str) else mf.forecast_json
    except (json.JSONDecodeError, TypeError):
        return None

    # 各指数逐一计算 forward
    fwd: dict[str, float] = {}
    for code in _A_INDEX_CODES:
        fwd[code] = _fwd_change_for_code(code, horizon_days, mf.as_of_date)

    # 上证综指代表大盘
    actual_market_pct = fwd.get("000001", float("nan"))
    if pd.isna(actual_market_pct):
        return None

    actual_dir = _resolve_direction(actual_market_pct, threshold)
    market_dir = forecast.get("market_direction", {})
    predicted_dir = market_dir.get("direction", "?")

    # 各指数对照
    index_forecasts = forecast.get("index_forecasts") or []
    index_results = []
    correct_count = 0
    for ix in index_forecasts:
        code = ix.get("code", "")
        predicted_cn = ix.get("direction", "")
        actual_pct = fwd.get(code, float("nan"))
        if pd.isna(actual_pct):
            continue
        actual_cn = _resolve_direction_cn(actual_pct, threshold)
        matched = (
            (predicted_cn == "偏多" and actual_cn == "偏多")
            or (predicted_cn == "偏空" and actual_cn == "偏空")
            or (predicted_cn == "震荡" and actual_cn == "震荡")
        )
        if matched:
            correct_count += 1
        index_results.append({
            "code": code,
            "name": ix.get("name", code),
            "predicted": predicted_cn,
            "actual_pct": actual_pct,
            "actual_direction": actual_cn,
            "correct": matched,
        })

    return {
        "forecast_id": mf.id,
        "as_of_date": str(mf.as_of_date),
        "horizon_days": horizon_days,
        "predicted_direction": predicted_dir,
        "actual_direction": actual_dir,
        "actual_pct_chg": actual_market_pct,
        "direction_correct": predicted_dir == actual_dir,
        "predicted_confidence": mf.direction_confidence,
        "index_results": index_results,
        "index_correct_count": correct_count,
        "index_total": len(index_results),
    }


def save_forecast_evals(
    since: date | None = None,
    horizons: list[int] | None = None,
    threshold: float = 0.3,
) -> dict[str, Any]:
    """批量评估所有预测并存入 forecast_evals 表。"""
    if horizons is None:
        horizons = [1, 3, 5, 20]

    init_db()
    saved = 0
    errors = []

    with SessionLocal() as session:
        q = select(MarketForecast).order_by(MarketForecast.as_of_date.asc())
        if since:
            q = q.where(MarketForecast.as_of_date >= since)
        mfs = session.execute(q).scalars().all()

        for mf in mfs:
            for h in horizons:
                existing = session.execute(
                    select(ForecastEval).where(
                        ForecastEval.forecast_id == mf.id,
                        ForecastEval.horizon_days == h,
                    )
                ).scalars().first()
                if existing:
                    continue

                try:
                    result = evaluate_forecast(mf, horizon_days=h, threshold=threshold)
                    if result is None:
                        continue

                    fe = ForecastEval(
                        forecast_id=mf.id,
                        horizon_days=h,
                        predicted_direction=result["predicted_direction"],
                        actual_direction=result["actual_direction"],
                        direction_correct=result["direction_correct"],
                        index_results_json=json.dumps(result["index_results"], ensure_ascii=False),
                        predicted_confidence=result["predicted_confidence"] or 0,
                        index_correct_count=result["index_correct_count"],
                        index_total=result["index_total"],
                        evaluated_at_utc=datetime.now(timezone.utc),
                    )
                    session.add(fe)
                    saved += 1
                except Exception as exc:
                    errors.append({"forecast_id": mf.id, "horizon": h, "error": str(exc)})

        session.commit()

    return {"evaluated": saved, "errors": errors}


def forecast_calibration_summary(
    horizon_days: int = 1,
    since: date | None = None,
) -> dict[str, Any]:
    """聚合置信度校准报告。"""
    init_db()
    with SessionLocal() as session:
        q = (
            select(ForecastEval, MarketForecast)
            .join(MarketForecast, ForecastEval.forecast_id == MarketForecast.id)
            .where(ForecastEval.horizon_days == horizon_days)
            .order_by(MarketForecast.as_of_date.asc())
        )
        if since:
            q = q.where(MarketForecast.as_of_date >= since)
        rows = session.execute(q).all()

        if not rows:
            return {"horizon_days": horizon_days, "total": 0, "message": "无评估数据"}

        total = len(rows)
        correct = sum(1 for fe, _ in rows if fe.direction_correct)
        accuracy = round(correct / total * 100, 1) if total > 0 else None

        bins = [
            (0, 50, "0-50%"),
            (50, 65, "50-65%"),
            (65, 80, "65-80%"),
            (80, 101, "80-100%"),
        ]
        confidence_bins = []
        for lo, hi, label in bins:
            in_bin = [(fe, _) for fe, _ in rows if lo <= (fe.predicted_confidence or 0) < hi]
            acc = sum(1 for fe, _ in in_bin if fe.direction_correct)
            pct = round(acc / len(in_bin) * 100, 1) if in_bin else None
            confidence_bins.append({
                "bin": label,
                "count": len(in_bin),
                "accurate": acc,
                "accuracy_pct": pct,
                "bin_expected": (lo + hi) / 2,
            })

        per_index: dict[str, dict[str, int]] = {}
        for fe, _ in rows:
            if not fe.index_results_json:
                continue
            try:
                ir = json.loads(fe.index_results_json)
                for ix in ir:
                    code = ix["code"]
                    if code not in per_index:
                        per_index[code] = {"correct": 0, "total": 0}
                    per_index[code]["total"] += 1
                    if ix.get("correct"):
                        per_index[code]["correct"] += 1
            except (json.JSONDecodeError, TypeError):
                pass

        per_index_pct = {
            code: {**v, "pct": round(v["correct"] / v["total"] * 100, 1) if v["total"] > 0 else None}
            for code, v in per_index.items()
        }

        by_direction: dict[str, int] = {}
        for fe, _ in rows:
            d = fe.predicted_direction or "unknown"
            by_direction[f"{d}_count"] = by_direction.get(f"{d}_count", 0) + 1
            if fe.direction_correct:
                by_direction[f"{d}_correct"] = by_direction.get(f"{d}_correct", 0) + 1

        return {
            "horizon_days": horizon_days,
            "total": total,
            "correct": correct,
            "accuracy_pct": accuracy,
            "confidence_bins": confidence_bins,
            "per_index_accuracy": per_index_pct,
            "by_direction": by_direction,
        }


def evidence_type_correlation(since: date | None = None) -> dict[str, Any]:
    """分析各类 evidence 信号与预测准确率的相关性。

    返回每种 evidence type 的出现次数和命中率。
    """
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(MarketForecast)
            .where(MarketForecast.verified_at.isnot(None))
            .order_by(MarketForecast.as_of_date.asc())
        ).scalars().all()

        if since:
            rows = [r for r in rows if r.as_of_date >= since]

        evidence_stats: dict[str, dict[str, int]] = {}
        for mf in rows:
            try:
                forecast = json.loads(mf.forecast_json) if isinstance(mf.forecast_json, str) else mf.forecast_json
            except (json.JSONDecodeError, TypeError):
                continue
            evidence_list = (forecast.get("market_direction") or {}).get("evidence") or []
            direction_correct = mf.direction_correct
            for ev in evidence_list:
                etype = ev.get("type", "unknown")
                if etype not in evidence_stats:
                    evidence_stats[etype] = {"count": 0, "correct": 0}
                evidence_stats[etype]["count"] += 1
                if direction_correct:
                    evidence_stats[etype]["correct"] += 1

        result = {}
        for etype, stats in sorted(evidence_stats.items(), key=lambda x: -x[1]["count"]):
            result[etype] = {
                "count": stats["count"],
                "correct": stats["correct"],
                "hit_rate_pct": round(stats["correct"] / stats["count"] * 100, 1) if stats["count"] > 0 else None,
            }

        return {"total_forecasts": len(rows), "evidence_types": result}


def forecast_accuracy_trend(days: int = 60) -> list[dict[str, Any]]:
    """滚动准确率趋势。"""
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(ForecastEval, MarketForecast)
            .join(MarketForecast, ForecastEval.forecast_id == MarketForecast.id)
            .where(ForecastEval.horizon_days == 1)
            .order_by(MarketForecast.as_of_date.asc())
        ).all()

        trend = []
        window = []
        for fe, mf in rows:
            window.append(1 if fe.direction_correct else 0)
            if len(window) > 5:
                window.pop(0)
            trend.append({
                "date": str(mf.as_of_date),
                "correct": fe.direction_correct,
                "rolling_accuracy_5": round(sum(window) / len(window) * 100, 1) if window else None,
            })
        return trend
