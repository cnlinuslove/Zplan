"""公司档案只读（``company_profile`` 表，资讯侧写入）。"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import inspect, text

from zplan_shared.market import resolve_ts_code
from zplan_shared.models import SessionLocal, engine, init_db


def get_company_profile(ts_code: str) -> dict[str, Any] | None:
    init_db()
    if not inspect(engine).has_table("company_profile"):
        return None
    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        row = session.execute(
            text(
                "SELECT ts_code, name, website, positioning, core_products_json, "
                "team_json, sources_json, updated_at FROM company_profile WHERE ts_code = :c"
            ),
            {"c": code},
        ).mappings().first()
    if not row:
        return None
    out = dict(row)
    for key in ("core_products_json", "team_json", "sources_json"):
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return out
