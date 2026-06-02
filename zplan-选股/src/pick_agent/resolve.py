"""代码 / 简称 / 名称解析。"""
from __future__ import annotations

import re

from sqlalchemy import or_, select

from zplan_shared.market import resolve_ts_code
from zplan_shared.models import SessionLocal, StockList, init_db


class SymbolNotFoundError(LookupError):
    pass


class SymbolAmbiguousError(LookupError):
    def __init__(self, message: str, matches: list[dict[str, str]]):
        super().__init__(message)
        self.matches = matches


def resolve_symbol(query: str) -> str:
    """
  解析用户输入：
  - 6 位代码（可带 .SH/.SZ）
  - 简称子串（stock_list.name LIKE）
  """
    raw = query.strip()
    if not raw:
        raise SymbolNotFoundError("股票查询为空")

    if re.fullmatch(r"\d{6}", raw):
        return resolve_ts_code(raw)

    code_like = resolve_ts_code(raw)
    if re.fullmatch(r"\d{6}", code_like):
        init_db()
        with SessionLocal() as session:
            hit = session.execute(
                select(StockList.ts_code).where(StockList.ts_code == code_like)
            ).scalar_one_or_none()
        if hit:
            return hit

    init_db()
    key = raw.replace(" ", "")
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(StockList.ts_code, StockList.name)
                .where(
                    or_(
                        StockList.name.contains(key),
                        StockList.name.contains(raw),
                    )
                )
                .limit(10)
            )
            .all()
        )

    if not rows:
        raise SymbolNotFoundError(f"未找到匹配「{query}」的股票，请使用 6 位代码")

    matches = [{"ts_code": r[0], "name": r[1]} for r in rows]
    if len(matches) == 1:
        return matches[0]["ts_code"]

    exact = [m for m in matches if m["name"] == raw or m["name"] == key]
    if len(exact) == 1:
        return exact[0]["ts_code"]

    raise SymbolAmbiguousError(
        f"「{query}」匹配多只股票，请指定代码：" + "、".join(f"{m['name']}({m['ts_code']})" for m in matches),
        matches,
    )
