"""个股概念/题材 ETL（东财 F10 核心题材 → ``stock_concept_members``）。"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import requests
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from zplan_shared.market import resolve_ts_code
from zplan_shared.models import SessionLocal, StockConceptMember, StockList, init_db
from zplan_shared.stock_meta_etl import _meta_http_session

logger = logging.getLogger(__name__)

_EM_F10_CONCEPTION = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
)

# 指数/资金/规模类标签，默认不算「概念题材」
_TAG_BLOCKLIST = (
    "MSCI",
    "富时",
    "标普",
    "股通",
    "融资融券",
    "机构重仓",
    "证金持股",
    "HS300",
    "上证",
    "深证",
    "深成",
    "创业板综",
    "央视",
    "AH股",
    "成份",
    "成份股",
    "热股",
    "权重股",
    "大盘股",
    "大盘价值",
    "大盘成长",
    "价值股",
    "破净",
    "百元股",
    "茅指数",
    "宁组合",
    "龙头",
    "周期股",
    "标准普尔",
    "深股通",
    "沪股通",
)


def em_f10_market_code(ts_code: str) -> str:
    """6 位代码 → 东财 F10 ``code`` 参数（SZ/SH/BJ）。"""
    code = resolve_ts_code(ts_code).zfill(6)
    if code.startswith(("92", "4", "8")):
        return f"BJ{code}"
    if code.startswith(("5", "6")):
        return f"SH{code}"
    return f"SZ{code}"


def classify_board_kind(board_name: str) -> str:
    """粗分：concept / industry / region / tag / theme。"""
    name = (board_name or "").strip()
    if not name:
        return "theme"
    if any(k in name for k in _TAG_BLOCKLIST):
        return "tag"
    if name.endswith("板块"):
        return "region"
    if "Ⅱ" in name or "Ⅲ" in name:
        return "industry"
    if "概念" in name:
        return "concept"
    return "theme"


def fetch_stock_concepts_em(
    ts_code: str,
    *,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """拉取单票东财 F10 所属板块（``ssbk``）。"""
    http = session or _meta_http_session()
    params = {"code": em_f10_market_code(ts_code)}
    resp = http.get(
        _EM_F10_CONCEPTION,
        params=params,
        timeout=float(os.getenv("STOCK_CONCEPT_HTTP_TIMEOUT", "25")),
        headers={"Referer": "https://emweb.securities.eastmoney.com/"},
    )
    resp.raise_for_status()
    payload = resp.json()
    code = resolve_ts_code(ts_code)
    rows: list[dict[str, Any]] = []
    for item in payload.get("ssbk") or []:
        name = str(item.get("BOARD_NAME") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "ts_code": code,
                "concept_name": name[:128],
                "board_kind": classify_board_kind(name),
            }
        )
    return rows


def upsert_stock_concept_members(
    rows: list[dict[str, Any]],
    *,
    stock_name: str | None = None,
) -> int:
    if not rows:
        return 0
    now = datetime.utcnow()
    with SessionLocal() as db:
        for row in rows:
            stmt = insert(StockConceptMember).values(
                {
                    "concept_name": row["concept_name"],
                    "ts_code": row["ts_code"],
                    "name": stock_name or row.get("name"),
                    "synced_at_utc": now,
                }
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["concept_name", "ts_code"],
                set_={
                    "name": stmt.excluded.name,
                    "synced_at_utc": now,
                },
            )
            db.execute(stmt)
        db.commit()
    return len(rows)


def backfill_stock_concepts(
    *,
    limit: int | None = None,
    only_missing: bool = True,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, int | str]:
    """
    全市场回填：按股票拉 F10，写入 ``stock_concept_members``（反向：股 → 所属概念）。

    ``kinds`` 默认 ``concept`` + ``theme``；设 ``STOCK_CONCEPT_ALL_KINDS=true`` 写入全部 ssbk。
    """
    init_db()
    if kinds is None:
        if os.getenv("STOCK_CONCEPT_ALL_KINDS", "").lower() in ("1", "true", "yes"):
            kinds = ("concept", "theme", "industry", "region", "tag")
        else:
            kinds = ("concept", "theme")

    with SessionLocal() as db:
        listed = db.execute(
            select(StockList.ts_code, StockList.name).order_by(StockList.ts_code)
        ).all()
    name_map = {str(c).zfill(6): n for c, n in listed}
    codes = list(name_map.keys())

    if only_missing:
        with SessionLocal() as db:
            have = {
                r[0]
                for r in db.execute(
                    select(StockConceptMember.ts_code).distinct()
                )
            }
        codes = [c for c in codes if c not in have]
    if limit:
        codes = codes[:limit]

    sleep_s = float(os.getenv("STOCK_CONCEPT_SLEEP", "0.35") or "0.35")
    http = _meta_http_session()
    ok, fail, rows_written = 0, 0, 0

    logger.info("[INFO] 概念题材回填：待处理 %s 只，写入类型 %s", len(codes), kinds)

    for idx, code in enumerate(codes, 1):
        if idx > 1 and sleep_s > 0:
            time.sleep(sleep_s)
        try:
            raw = fetch_stock_concepts_em(code, session=http)
            filtered = [r for r in raw if r["board_kind"] in kinds]
            with SessionLocal() as db:
                db.execute(
                    delete(StockConceptMember).where(StockConceptMember.ts_code == code)
                )
                db.commit()
            if filtered:
                rows_written += upsert_stock_concept_members(
                    filtered, stock_name=name_map.get(code)
                )
            ok += 1
        except Exception as exc:
            fail += 1
            logger.warning("%s 概念拉取失败: %s", code, exc)
        if idx % 100 == 0 or idx == len(codes):
            logger.info(
                "[INFO] 进度 %s/%s ok=%s fail=%s rows=%s",
                idx,
                len(codes),
                ok,
                fail,
                rows_written,
            )

    stats = {
        "queued": len(codes),
        "ok": ok,
        "fail": fail,
        "rows": rows_written,
        "kinds": ",".join(kinds),
        "source": "em_f10",
    }
    logger.info("[INFO] 概念题材回填完成: %s", stats)
    return stats
