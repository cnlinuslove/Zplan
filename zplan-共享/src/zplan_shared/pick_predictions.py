"""选股预测价提取与事后验证（回测 Agent 主调，选股只读校准结果）。"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import desc, select

from zplan_shared.features import suggested_price_levels
from zplan_shared.market import get_bars
from zplan_shared.models import (
    PickEntry,
    PickPredictionOutcome,
    PickRun,
    SessionLocal,
    init_db,
)


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def price_levels_from_pick(p: dict[str, Any]) -> dict[str, float | None]:
    """从扫描/简报 pick 字典取或推算建议价。"""
    for key in ("predicted_buy_price", "suggested_buy", "buy_price"):
        if p.get(key) is not None:
            buy = float(p[key])
            return {
                "predicted_buy_price": buy,
                "predicted_target_price": _f(p.get("predicted_target_price") or p.get("target_price")),
                "predicted_stop_loss": _f(p.get("predicted_stop_loss") or p.get("stop_loss")),
                "price_source": p.get("price_source") or "stored",
            }
    code = p.get("ts_code")
    if code:
        bars = get_bars(code)
        if not bars.empty:
            lv = suggested_price_levels(bars)
            return {
                "predicted_buy_price": lv.get("suggested_buy"),
                "predicted_target_price": lv.get("target_price"),
                "predicted_stop_loss": lv.get("stop_loss"),
                "price_source": "rule_recomputed",
            }
    return {}


def price_levels_from_report(report: dict[str, Any]) -> dict[str, float | None]:
    advice = report.get("投资建议") or {}
    llm = report.get("llm") or {}
    buy = advice.get("建议买入价")
    target = advice.get("目标价")
    stop = advice.get("止损参考")
    source = "rule"
    if llm.get("buy_price") is not None and buy == llm.get("buy_price"):
        source = "llm"
    elif llm.get("buy_price") is not None:
        source = "llm"
    if buy is None and llm.get("buy_price") is not None:
        buy = llm.get("buy_price")
        target = llm.get("target_price", target)
        stop = llm.get("stop_loss", stop)
        source = "llm"
    return {
        "predicted_buy_price": _f(buy),
        "predicted_target_price": _f(target),
        "predicted_stop_loss": _f(stop),
        "price_source": source,
    }


def price_levels_from_entry(entry: PickEntry) -> dict[str, float | None]:
    if entry.predicted_buy_price is not None:
        return {
            "predicted_buy_price": entry.predicted_buy_price,
            "predicted_target_price": entry.predicted_target_price,
            "predicted_stop_loss": entry.predicted_stop_loss,
            "price_source": entry.price_source or "stored",
        }
    report = _loads(entry.report_json)
    if report:
        return price_levels_from_report(report)
    proc = _loads(entry.analysis_process_json) or {}
    brief = proc.get("llm_brief") or {}
    if brief.get("buy_price") is not None:
        return {
            "predicted_buy_price": _f(brief.get("buy_price")),
            "predicted_target_price": _f(brief.get("target_price")),
            "predicted_stop_loss": _f(brief.get("stop_loss")),
            "price_source": "llm_brief",
        }
    if entry.ts_code:
        bars = get_bars(entry.ts_code, end=entry.created_at_utc.date() if entry.created_at_utc else None)
        if not bars.empty:
            lv = suggested_price_levels(bars)
            return {
                "predicted_buy_price": lv.get("suggested_buy"),
                "predicted_target_price": lv.get("target_price"),
                "predicted_stop_loss": lv.get("stop_loss"),
                "price_source": "rule_recomputed",
            }
    return {}


def backfill_entry_predictions(entry: PickEntry, *, session: Any) -> tuple[bool, bool]:
    """若 entry 缺预测价则从 report/规则补写。返回 (已有买入价, 本次新写入)。"""
    if entry.predicted_buy_price is not None:
        return True, False
    levels = price_levels_from_entry(entry)
    if not levels.get("predicted_buy_price"):
        return False, False
    entry.predicted_buy_price = levels["predicted_buy_price"]
    entry.predicted_target_price = levels.get("predicted_target_price")
    entry.predicted_stop_loss = levels.get("predicted_stop_loss")
    entry.price_source = levels.get("price_source")
    session.add(entry)
    return True, True


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def evaluate_outcome(
    entry: PickEntry,
    run: PickRun,
    *,
    horizon_days: int = 10,
) -> dict[str, Any]:
    """单条 pick 在 as_of 之后 horizon 个交易日的实际表现。"""
    levels = price_levels_from_entry(entry)
    buy = levels.get("predicted_buy_price")
    target = levels.get("predicted_target_price")
    stop = levels.get("predicted_stop_loss")

    as_of = run.trade_date_as_of
    if as_of is None:
        as_of = entry.created_at_utc.date() if entry.created_at_utc else None

    base: dict[str, Any] = {
        "entry_id": entry.id,
        "horizon_days": horizon_days,
        "as_of_date": str(as_of) if as_of else None,
        "predicted_buy_price": buy,
        "predicted_target_price": target,
        "predicted_stop_loss": stop,
        "status": "no_prediction",
    }
    if not buy or not as_of or not entry.ts_code:
        return base

    bars = get_bars(entry.ts_code)
    if bars.empty:
        base["status"] = "no_bars"
        return base

    idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
    bars = bars.copy()
    bars.index = idx
    as_ts = pd.Timestamp(as_of)
    on_or_before = bars[bars.index <= as_ts]
    if on_or_before.empty:
        base["status"] = "no_bars"
        return base

    close_at_as_of = float(on_or_before["close"].iloc[-1])
    forward = bars[bars.index > as_ts].head(horizon_days)

    if forward.empty:
        return {
            **base,
            "status": "pending",
            "close_at_as_of": close_at_as_of,
        }

    min_low = float(forward["low"].min())
    max_high = float(forward["high"].max())
    close_end = float(forward["close"].iloc[-1])
    next_open = float(forward["open"].iloc[0]) if "open" in forward.columns else None

    buy_touched = min_low <= buy
    target_hit = target is not None and max_high >= target
    stop_hit = stop is not None and min_low <= stop
    buy_gap_pct = round((min_low - buy) / buy * 100, 4)
    ret_buy = round((close_end - buy) / buy * 100, 4) if buy_touched else None
    ret_close = round((close_end - close_at_as_of) / close_at_as_of * 100, 4)

    status = "complete" if len(forward) >= horizon_days else "partial"

    return {
        **base,
        "status": status,
        "close_at_as_of": close_at_as_of,
        "next_open": next_open,
        "min_low": round(min_low, 4),
        "max_high": round(max_high, 4),
        "close_at_horizon": round(close_end, 4),
        "buy_touched": buy_touched,
        "target_hit": target_hit,
        "stop_hit": stop_hit,
        "buy_gap_pct": buy_gap_pct,
        "return_from_buy_pct": ret_buy,
        "return_from_close_pct": ret_close,
        "horizon_start": str(forward.index[0].date()),
        "horizon_end": str(forward.index[-1].date()),
        "bars_in_horizon": len(forward),
    }


def upsert_outcome(session: Any, payload: dict[str, Any]) -> PickPredictionOutcome:
    entry_id = int(payload["entry_id"])
    horizon = int(payload["horizon_days"])
    existing = session.execute(
        select(PickPredictionOutcome).where(
            PickPredictionOutcome.entry_id == entry_id,
            PickPredictionOutcome.horizon_days == horizon,
        )
    ).scalar_one_or_none()

    fields = {
        "as_of_date": _parse_date(payload.get("as_of_date")),
        "status": str(payload.get("status") or "pending"),
        "predicted_buy_price": _f(payload.get("predicted_buy_price")),
        "predicted_target_price": _f(payload.get("predicted_target_price")),
        "predicted_stop_loss": _f(payload.get("predicted_stop_loss")),
        "close_at_as_of": _f(payload.get("close_at_as_of")),
        "next_open": _f(payload.get("next_open")),
        "min_low": _f(payload.get("min_low")),
        "max_high": _f(payload.get("max_high")),
        "close_at_horizon": _f(payload.get("close_at_horizon")),
        "buy_touched": payload.get("buy_touched"),
        "target_hit": payload.get("target_hit"),
        "stop_hit": payload.get("stop_hit"),
        "buy_gap_pct": _f(payload.get("buy_gap_pct")),
        "return_from_buy_pct": _f(payload.get("return_from_buy_pct")),
        "return_from_close_pct": _f(payload.get("return_from_close_pct")),
        "horizon_start": _parse_date(payload.get("horizon_start")),
        "horizon_end": _parse_date(payload.get("horizon_end")),
        "evaluated_at_utc": datetime.utcnow(),
    }

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        row = existing
    else:
        row = PickPredictionOutcome(entry_id=entry_id, horizon_days=horizon, **fields)
        session.add(row)
    return row


def validate_entries(
    *,
    run_id: int | None = None,
    entry_id: int | None = None,
    horizons: list[int] | None = None,
    limit: int = 500,
    backfill_prices: bool = True,
) -> dict[str, Any]:
    """批量验证并落库。返回统计摘要。"""
    init_db()
    horizons = horizons or [5, 10, 20]
    stats = {"evaluated": 0, "pending": 0, "no_prediction": 0, "backfilled": 0, "horizons": horizons}

    with SessionLocal() as session:
        stmt = (
            select(PickEntry, PickRun)
            .join(PickRun, PickEntry.run_id == PickRun.id)
            .order_by(desc(PickEntry.created_at_utc))
        )
        if entry_id is not None:
            stmt = stmt.where(PickEntry.id == entry_id)
        elif run_id is not None:
            stmt = stmt.where(PickEntry.run_id == run_id)
        stmt = stmt.limit(limit)
        rows = session.execute(stmt).all()

        for entry, run in rows:
            if backfill_prices:
                has_buy, wrote = backfill_entry_predictions(entry, session=session)
                if wrote:
                    stats["backfilled"] += 1
                if not has_buy:
                    stats["no_prediction"] += 1
                    continue
            elif not entry.predicted_buy_price:
                stats["no_prediction"] += 1
                continue
            for h in horizons:
                payload = evaluate_outcome(entry, run, horizon_days=h)
                upsert_outcome(session, payload)
                stats["evaluated"] += 1
                if payload.get("status") in ("pending", "partial"):
                    stats["pending"] += 1
        session.commit()

    return stats


def list_outcomes(
    *,
    limit: int = 100,
    status: str | None = None,
    horizon_days: int | None = None,
) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as session:
        stmt = (
            select(PickPredictionOutcome, PickEntry, PickRun)
            .join(PickEntry, PickPredictionOutcome.entry_id == PickEntry.id)
            .join(PickRun, PickEntry.run_id == PickRun.id)
            .order_by(desc(PickPredictionOutcome.evaluated_at_utc))
            .limit(limit)
        )
        if status:
            stmt = stmt.where(PickPredictionOutcome.status == status)
        if horizon_days is not None:
            stmt = stmt.where(PickPredictionOutcome.horizon_days == horizon_days)
        rows = session.execute(stmt).all()

    out: list[dict[str, Any]] = []
    for oc, entry, run in rows:
        out.append(
            {
                "outcome_id": oc.id,
                "entry_id": entry.id,
                "run_id": run.id,
                "run_kind": run.run_kind,
                "rule_version": run.rule_version,
                "ts_code": entry.ts_code,
                "name": entry.name,
                "horizon_days": oc.horizon_days,
                "status": oc.status,
                "as_of_date": str(oc.as_of_date) if oc.as_of_date else None,
                "predicted_buy_price": oc.predicted_buy_price,
                "min_low": oc.min_low,
                "buy_touched": oc.buy_touched,
                "buy_gap_pct": oc.buy_gap_pct,
                "return_from_buy_pct": oc.return_from_buy_pct,
                "return_from_close_pct": oc.return_from_close_pct,
                "target_hit": oc.target_hit,
                "stop_hit": oc.stop_hit,
                "price_source": entry.price_source,
            }
        )
    return out


def calibration_summary(*, horizon_days: int = 10) -> dict[str, Any]:
    """聚合预测偏差，供选股策略调参参考。"""
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(PickPredictionOutcome, PickEntry, PickRun)
            .join(PickEntry, PickPredictionOutcome.entry_id == PickEntry.id)
            .join(PickRun, PickEntry.run_id == PickRun.id)
            .where(
                PickPredictionOutcome.horizon_days == horizon_days,
                PickPredictionOutcome.status.in_(("complete", "partial")),
                PickPredictionOutcome.predicted_buy_price.isnot(None),
            )
        ).all()

    if not rows:
        return {"horizon_days": horizon_days, "count": 0, "message": "尚无已完成验证记录，请先运行 validate"}

    records = []
    for oc, entry, run in rows:
        records.append(
            {
                "buy_gap_pct": oc.buy_gap_pct,
                "buy_touched": oc.buy_touched,
                "return_from_buy_pct": oc.return_from_buy_pct,
                "return_from_close_pct": oc.return_from_close_pct,
                "target_hit": oc.target_hit,
                "stop_hit": oc.stop_hit,
                "price_source": entry.price_source,
                "run_kind": run.run_kind,
                "rule_version": run.rule_version,
            }
        )

    df = pd.DataFrame(records)
    n = len(df)
    touched = df["buy_touched"].fillna(False).astype(bool)
    gaps = df["buy_gap_pct"].dropna()

    hints: list[str] = []
    if len(gaps) >= 5:
        mean_gap = float(gaps.mean())
        if mean_gap > 1.5:
            hints.append(
                f"建议买入价偏保守（均价差 {mean_gap:.2f}%：期内最低价多高于预测买价），"
                "可考虑提高 suggested_buy（如减小 MA20 折扣）"
            )
        elif mean_gap < -2.0:
            hints.append(
                f"建议买入价偏激进（均价差 {mean_gap:.2f}%：期内常跌破预测买价），"
                "可考虑下调 suggested_buy 或放宽买入容忍"
            )

    touch_rate = float(touched.mean()) if n else 0.0
    if n >= 5 and touch_rate < 0.4:
        hints.append(f"{horizon_days} 日内触及建议买价比例仅 {touch_rate:.0%}，预测买入区可能过高")

    by_source = (
        df.groupby("price_source", dropna=False)
        .agg(
            n=("buy_gap_pct", "count"),
            touch_rate=("buy_touched", lambda s: float(s.fillna(False).mean())),
            mean_gap_pct=("buy_gap_pct", "mean"),
        )
        .reset_index()
        .to_dict(orient="records")
    )

    return {
        "horizon_days": horizon_days,
        "count": n,
        "touch_rate": round(touch_rate, 4),
        "mean_buy_gap_pct": round(float(gaps.mean()), 4) if len(gaps) else None,
        "median_buy_gap_pct": round(float(gaps.median()), 4) if len(gaps) else None,
        "mean_return_from_buy_pct": round(float(df["return_from_buy_pct"].dropna().mean()), 4)
        if df["return_from_buy_pct"].notna().any()
        else None,
        "target_hit_rate": round(float(df["target_hit"].fillna(False).mean()), 4),
        "stop_hit_rate": round(float(df["stop_hit"].fillna(False).mean()), 4),
        "by_price_source": by_source,
        "optimization_hints": hints,
    }
