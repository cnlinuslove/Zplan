"""持仓 / 关注订阅 CRUD。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import re

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.market import resolve_ts_code
from zplan_shared.models import PickWatchlist, SessionLocal, StockList, init_db


def _resolve_code_and_name(query: str) -> tuple[str, str | None]:
    raw = query.strip()
    if re.fullmatch(r"\d{6}", raw):
        code = resolve_ts_code(raw)
        with SessionLocal() as session:
            name = session.execute(
                select(StockList.name).where(StockList.ts_code == code)
            ).scalar_one_or_none()
        return code, name

    key = raw.replace(" ", "")
    with SessionLocal() as session:
        rows = session.execute(
            select(StockList.ts_code, StockList.name)
            .where(or_(StockList.name.contains(key), StockList.name.contains(raw)))
            .limit(10)
        ).all()
    if not rows:
        raise LookupError(f"未找到匹配「{query}」的股票")
    if len(rows) == 1:
        return rows[0][0], rows[0][1]
    exact = [r for r in rows if r[1] == raw or r[1] == key]
    if len(exact) == 1:
        return exact[0][0], exact[0][1]
    raise LookupError(
        "匹配多只：" + "、".join(f"{r[1]}({r[0]})" for r in rows)
    )


def add_watch(
    query: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    init_db()
    code, name = _resolve_code_and_name(query)
    with SessionLocal() as session:

        stmt = insert(PickWatchlist).values(
            ts_code=code,
            name=name,
            note=note,
            enabled=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ts_code"],
            set_={
                "name": stmt.excluded.name,
                "note": stmt.excluded.note,
                "enabled": True,
                "updated_at_utc": datetime.utcnow(),
            },
        )
        session.execute(stmt)
        session.commit()
    return {"ts_code": code, "name": name, "note": note, "enabled": True}


def add_watch_resolved(ts_code: str, name: str | None = None, *, note: str | None = None) -> dict[str, Any]:
    """已解析代码时直接写入（供 pick_agent.resolve 后调用）。"""
    init_db()
    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        stmt = insert(PickWatchlist).values(
            ts_code=code, name=name, note=note, enabled=True
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ts_code"],
            set_={
                "name": stmt.excluded.name,
                "note": stmt.excluded.note,
                "enabled": True,
                "updated_at_utc": datetime.utcnow(),
            },
        )
        session.execute(stmt)
        session.commit()
    return {"ts_code": code, "name": name, "note": note, "enabled": True}


def remove_watch(ts_code: str) -> bool:
    init_db()
    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        row = session.execute(
            select(PickWatchlist).where(PickWatchlist.ts_code == code)
        ).scalar_one_or_none()
        if not row:
            return False
        row.enabled = False
        row.updated_at_utc = datetime.utcnow()
        session.commit()
        return True


def delete_watch(ts_code: str) -> bool:
    init_db()
    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        row = session.execute(
            select(PickWatchlist).where(PickWatchlist.ts_code == code)
        ).scalar_one_or_none()
        if not row:
            return False
        session.delete(row)
        session.commit()
        return True


def list_watch(*, enabled_only: bool = True) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as session:
        stmt = (
            select(PickWatchlist, StockList.name.label("stock_name"))
            .outerjoin(StockList, PickWatchlist.ts_code == StockList.ts_code)
            .order_by(PickWatchlist.created_at_utc)
        )
        if enabled_only:
            stmt = stmt.where(PickWatchlist.enabled.is_(True))
        rows = session.execute(stmt).all()

    result: list[dict[str, Any]] = []
    needs_update: list[tuple[int, str]] = []
    for r, stock_name in rows:
        name = r.name or stock_name
        if not r.name and stock_name:
            needs_update.append((r.id, stock_name))
        result.append(
            {
                "id": r.id,
                "ts_code": r.ts_code,
                "name": name,
                "note": r.note,
                "enabled": r.enabled,
                "last_sync_at_utc": r.last_sync_at_utc.isoformat() + "Z" if r.last_sync_at_utc else None,
                "last_brief_at_utc": r.last_brief_at_utc.isoformat() + "Z" if r.last_brief_at_utc else None,
                "created_at_utc": r.created_at_utc.isoformat() + "Z",
            }
        )

    if needs_update:
        with SessionLocal() as session:
            for wid, sname in needs_update:
                session.execute(
                    update(PickWatchlist)
                    .where(PickWatchlist.id == wid)
                    .values(name=sname, updated_at_utc=datetime.utcnow())
                )
            session.commit()

    return result


def watch_codes(*, enabled_only: bool = True) -> list[str]:
    return [w["ts_code"] for w in list_watch(enabled_only=enabled_only)]


def touch_sync(codes: list[str]) -> None:
    init_db()
    now = datetime.utcnow()
    with SessionLocal() as session:
        session.execute(
            update(PickWatchlist)
            .where(PickWatchlist.ts_code.in_(codes))
            .values(last_sync_at_utc=now, updated_at_utc=now)
        )
        session.commit()


def touch_brief(codes: list[str]) -> None:
    init_db()
    now = datetime.utcnow()
    with SessionLocal() as session:
        session.execute(
            update(PickWatchlist)
            .where(PickWatchlist.ts_code.in_(codes))
            .values(last_brief_at_utc=now, updated_at_utc=now)
        )
        session.commit()
