"""概念 / 题材 / 行业筛选（东财概念板块成份 + 本地缓存）。"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.http_client import configure_akshare_http, throttle
from zplan_shared.market import get_panel, latest_trade_date, resolve_ts_code
from zplan_shared.models import SessionLocal, StockConceptMember, StockList, init_db

logger = logging.getLogger(__name__)


def _normalize_ts(code: str) -> str:
    raw = str(code).strip().split(".")[0]
    if raw.isdigit() and len(raw) <= 6:
        return resolve_ts_code(raw.zfill(6))
    return resolve_ts_code(raw)


def list_cached_concepts(*, keyword: str | None = None, limit: int = 50) -> list[str]:
    """已缓存的概念板名称列表。"""
    init_db()
    with SessionLocal() as session:
        stmt = select(StockConceptMember.concept_name).distinct()
        if keyword:
            key = keyword.strip()
            stmt = stmt.where(StockConceptMember.concept_name.contains(key))
        rows = session.execute(stmt.order_by(StockConceptMember.concept_name).limit(limit)).all()
    return [r[0] for r in rows]


def fetch_concept_board_names(*, keyword: str | None = None) -> list[str]:
    """从东财拉取概念板块名称（需网络）。"""
    import akshare as ak

    configure_akshare_http()
    df = ak.stock_board_concept_name_em()
    col = "板块名称" if "板块名称" in df.columns else df.columns[1]
    names = [str(x).strip() for x in df[col].tolist() if str(x).strip()]
    if keyword:
        key = keyword.strip()
        names = [n for n in names if key in n]
    return names


def resolve_concept_names(query: str, *, from_cache_only: bool = False) -> list[str]:
    """
    解析用户输入的题材关键词 → 匹配的概念板名称列表。
    优先精确/包含匹配；多个命中时全部返回。
    """
    key = query.strip()
    cached = list_cached_concepts(keyword=key, limit=200)
    if cached:
        return cached
    if from_cache_only:
        return []

    try:
        online = fetch_concept_board_names(keyword=key)
    except Exception as exc:
        logger.warning("东财概念列表不可用: %s", exc)
        return []

    if not online:
        return []
    # 模糊：用户输入「脑机接口」可命中「脑机接口」「脑机概念」等
    return [n for n in online if key in n or n in key]


def sync_concept_members(concept_name: str) -> dict[str, Any]:
    """拉取并缓存单个概念板块成份股。"""
    import akshare as ak

    configure_akshare_http()
    cons = ak.stock_board_concept_cons_em(symbol=concept_name)
    code_col = "代码" if "代码" in cons.columns else cons.columns[0]
    name_col = "名称" if "名称" in cons.columns else cons.columns[1]

    now = datetime.utcnow()
    rows: list[dict[str, Any]] = []
    for _, r in cons.iterrows():
        try:
            code = _normalize_ts(str(r[code_col]))
        except Exception:
            continue
        rows.append(
            {
                "concept_name": concept_name,
                "ts_code": code,
                "name": str(r[name_col]).strip() if pd.notna(r.get(name_col)) else None,
                "synced_at_utc": now,
            }
        )

    init_db()
    with SessionLocal() as session:
        session.execute(
            delete(StockConceptMember).where(StockConceptMember.concept_name == concept_name)
        )
        for i in range(0, len(rows), 200):
            chunk = rows[i : i + 200]
            if not chunk:
                continue
            stmt = insert(StockConceptMember).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["concept_name", "ts_code"],
                set_={
                    "name": stmt.excluded.name,
                    "synced_at_utc": now,
                },
            )
            session.execute(stmt)
        session.commit()

    return {"concept_name": concept_name, "count": len(rows), "synced_at": now.isoformat() + "Z"}


def ensure_concept_cached(query: str, *, max_age_hours: int = 168) -> list[str]:
    """确保题材已缓存；返回匹配到的概念板名称。"""
    names = resolve_concept_names(query, from_cache_only=True)
    if names:
        return names

    online_names = resolve_concept_names(query, from_cache_only=False)
    if not online_names:
        raise LookupError(f"未找到匹配「{query}」的概念板块（可检查网络或换关键词）")

    for nm in online_names[:3]:
        sync_concept_members(nm)
        throttle(1.0)
    return online_names


def get_concept_members(
    concept_query: str,
    *,
    refresh: bool = False,
) -> pd.DataFrame:
    """题材成份股 DataFrame：ts_code, name, concept_name。"""
    if refresh:
        for nm in resolve_concept_names(concept_query, from_cache_only=False)[:3]:
            sync_concept_members(nm)
            throttle(1.0)

    concept_names = ensure_concept_cached(concept_query)
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(
                StockConceptMember.ts_code,
                StockConceptMember.name,
                StockConceptMember.concept_name,
            ).where(StockConceptMember.concept_name.in_(concept_names))
        ).all()

    if not rows:
        raise LookupError(f"「{concept_query}」无成份股缓存")

    df = pd.DataFrame(rows, columns=["ts_code", "name", "concept_name"])
    return df.drop_duplicates(subset=["ts_code"])


def screen_universe(
    *,
    concept: str | None = None,
    industry: str | None = None,
    name_like: str | None = None,
    ts_codes: list[str] | None = None,
    refresh_concept: bool = False,
    attach_panel: bool = True,
) -> pd.DataFrame:
    """
    固定条件筛选。``concept`` 为题材关键词（如 脑机接口）；
    ``industry`` / ``name_like`` 走 stock_list。
    """
    init_db()
    universe: pd.DataFrame | None = None

    if concept:
        universe = get_concept_members(concept, refresh=refresh_concept)
        universe = universe.rename(columns={"name": "name_concept"})

    if ts_codes:
        codes = [_normalize_ts(c) for c in ts_codes]
        base = pd.DataFrame({"ts_code": codes})
        universe = base if universe is None else universe[universe["ts_code"].isin(codes)]

    with SessionLocal() as session:
        stmt = select(StockList.ts_code, StockList.name, StockList.industry)
        if universe is not None and not universe.empty:
            stmt = stmt.where(StockList.ts_code.in_(universe["ts_code"].tolist()))
        if industry:
            stmt = stmt.where(StockList.industry.contains(industry.strip()))
        if name_like:
            key = name_like.strip()
            stmt = stmt.where(
                or_(StockList.name.contains(key), StockList.ts_code.contains(key))
            )
        meta_rows = session.execute(stmt).all()

    if not meta_rows:
        return pd.DataFrame()

    df = pd.DataFrame(meta_rows, columns=["ts_code", "name", "industry"])
    if universe is not None and "concept_name" in universe.columns:
        df = df.merge(
            universe[["ts_code", "concept_name"]].drop_duplicates("ts_code"),
            on="ts_code",
            how="left",
        )

    if attach_panel:
        td = latest_trade_date()
        if td:
            panel = get_panel(td, fields=["close", "pct_chg", "turnover_rate"])
            if not panel.empty:
                panel = panel.rename(
                    columns={
                        "close": "close_price",
                        "pct_chg": "pct_chg_today",
                        "turnover_rate": "turnover_rate_today",
                    }
                )
                df = df.merge(panel, on="ts_code", how="left")

    return df.sort_values("ts_code").reset_index(drop=True)
