"""全市场规则分表 ``stock_rule_scores`` 读写。"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.models import SessionLocal, StockRuleScore, init_db


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def upsert_rule_scores(
    rows: list[dict[str, Any]],
    *,
    trade_date_as_of: date,
    rule_version: str,
) -> int:
    """批量写入/更新规则分，返回写入条数。"""
    if not rows:
        return 0
    init_db()
    now = datetime.utcnow()
    with SessionLocal() as session:
        for chunk_start in range(0, len(rows), 500):
            chunk = rows[chunk_start : chunk_start + 500]
            values = []
            for r in chunk:
                signals = r.get("signals")
                features = r.get("features")
                values.append(
                    {
                        "ts_code": r["ts_code"],
                        "name": r.get("name"),
                        "trade_date_as_of": trade_date_as_of,
                        "rule_version": rule_version,
                        "tech_score": r.get("tech_score"),
                        "composite_score": r.get("composite_score"),
                        "verdict": r.get("verdict"),
                        "close_price": r.get("close"),
                        "signals_json": _dumps(signals) if signals else None,
                        "features_json": _dumps(features) if features else None,
                        "updated_at_utc": now,
                    }
                )
            stmt = insert(StockRuleScore).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ts_code", "trade_date_as_of", "rule_version"],
                set_={
                    "name": stmt.excluded.name,
                    "tech_score": stmt.excluded.tech_score,
                    "composite_score": stmt.excluded.composite_score,
                    "verdict": stmt.excluded.verdict,
                    "close_price": stmt.excluded.close_price,
                    "signals_json": stmt.excluded.signals_json,
                    "features_json": stmt.excluded.features_json,
                    "updated_at_utc": now,
                },
            )
            session.execute(stmt)
        session.commit()

    assign_ranks(trade_date_as_of=trade_date_as_of, rule_version=rule_version)
    return len(rows)


def assign_ranks(*, trade_date_as_of: date, rule_version: str) -> None:
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockRuleScore)
            .where(
                StockRuleScore.trade_date_as_of == trade_date_as_of,
                StockRuleScore.rule_version == rule_version,
            )
            .order_by(
                desc(StockRuleScore.composite_score),
                desc(StockRuleScore.tech_score),
            )
        ).scalars().all()
        for i, row in enumerate(rows, start=1):
            row.rank_by_composite = i
        session.commit()


def count_scores(*, trade_date_as_of: date, rule_version: str) -> int:
    init_db()
    with SessionLocal() as session:
        from sqlalchemy import func

        return int(
            session.execute(
                select(func.count())
                .select_from(StockRuleScore)
                .where(
                    StockRuleScore.trade_date_as_of == trade_date_as_of,
                    StockRuleScore.rule_version == rule_version,
                )
            ).scalar_one()
        )


def top_rule_scores(
    *,
    trade_date_as_of: date,
    rule_version: str,
    top_n: int = 300,
) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockRuleScore)
            .where(
                StockRuleScore.trade_date_as_of == trade_date_as_of,
                StockRuleScore.rule_version == rule_version,
            )
            .order_by(StockRuleScore.rank_by_composite)
            .limit(top_n)
        ).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        signals = json.loads(r.signals_json) if r.signals_json else []
        features = json.loads(r.features_json) if r.features_json else {}
        out.append(
            {
                "ts_code": r.ts_code,
                "name": r.name,
                "tech_score": r.tech_score,
                "composite_score": r.composite_score,
                "verdict": r.verdict,
                "close": r.close_price,
                "signals": signals,
                "features": features,
                "features_json": r.features_json,
                "rank_rule": r.rank_by_composite,
            }
        )
    return out


def latest_score_date(*, rule_version: str) -> date | None:
    init_db()
    with SessionLocal() as session:
        from sqlalchemy import func

        d = session.execute(
            select(func.max(StockRuleScore.trade_date_as_of)).where(
                StockRuleScore.rule_version == rule_version
            )
        ).scalar_one_or_none()
    return d if d else None
