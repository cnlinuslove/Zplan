"""概念股产品摘要缓存：用 LLM 批量获取股票在某概念下的核心产品和市场地位。

表 ``concept_product_cache`` 按需创建，缓存 LLM 结果，后续同一概念筛选秒出。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from zplan_shared.models import SessionLocal, init_db

logger = logging.getLogger(__name__)

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS concept_product_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code VARCHAR(16) NOT NULL,
    concept_name VARCHAR(128) NOT NULL,
    product_summary TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ts_code, concept_name)
)
"""


def _ensure_table() -> None:
    init_db()
    with SessionLocal() as s:
        s.execute(text(_TABLE_DDL))
        s.commit()


def get_product_summaries(
    ts_codes: list[str],
    concept_name: str,
) -> dict[str, str]:
    """从缓存获取产品摘要。返回 {ts_code: summary}，未缓存的 code 不在 key 中。"""
    _ensure_table()
    if not ts_codes:
        return {}
    with SessionLocal() as s:
        placeholders = ",".join(f":c{i}" for i in range(len(ts_codes)))
        rows = s.execute(
            text(
                f"SELECT ts_code, product_summary FROM concept_product_cache "
                f"WHERE ts_code IN ({placeholders}) AND concept_name = :concept"
            ),
            {**{f"c{i}": c for i, c in enumerate(ts_codes)}, "concept": concept_name},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def generate_product_summaries(
    ts_codes: list[str],
    concept_name: str,
    *,
    names: dict[str, str] | None = None,
    force_refresh: bool = False,
) -> dict[str, str]:
    """用 LLM 批量生成股票在指定概念下的产品摘要。

    Args:
        ts_codes: 股票代码列表
        concept_name: 概念名称
        names: {ts_code: name} 映射，帮助 LLM 识别
        force_refresh: 是否强制刷新缓存

    Returns:
        {ts_code: product_summary} 完整映射（含缓存命中和新生成的）
    """
    _ensure_table()

    # 先查缓存
    cached = get_product_summaries(ts_codes, concept_name)
    if not force_refresh:
        missing = [c for c in ts_codes if c not in cached]
    else:
        missing = ts_codes
        cached = {}

    if not missing:
        return cached

    # 用 LLM 批量生成
    try:
        from zplan_shared.llm.gemini import generate_json, llm_available

        if not llm_available():
            logger.warning("LLM 不可用，跳过产品摘要生成")
            return cached
    except Exception:
        return cached

    name_map = names or {}
    stock_list = "\n".join(
        f"- {c} {name_map.get(c, '')}"
        for c in missing
    )

    prompt = (
        f"以下是 A 股「{concept_name}」概念板块的部分成分股。\n"
        f"对每只股票，用一句话概括其与「{concept_name}」相关的核心产品和市场地位。\n"
        f"要求：具体到产品名称，提及市场份额或行业排名（如有），15-30 字。\n\n"
        f"{stock_list}\n\n"
        f'返回 JSON 对象：{{"items": [{{"ts_code": "000001", "product": "核心产品描述"}}]}}'
    )

    try:
        result = generate_json(
            prompt=prompt,
            response_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ts_code": {"type": "string"},
                                "product": {"type": "string"},
                            },
                            "required": ["ts_code", "product"],
                        },
                    }
                },
                "required": ["items"],
            },
        )
    except Exception as exc:
        logger.warning("LLM 产品摘要生成失败: %s", exc)
        return cached

    items = result.get("items", []) if isinstance(result, dict) else []
    if not isinstance(items, list):
        return cached

    # 写入缓存
    new_entries: dict[str, str] = {}
    now = datetime.utcnow().isoformat()
    with SessionLocal() as s:
        for item in items:
            code = item.get("ts_code", "")
            product = item.get("product", "")
            if code and product and code in missing:
                new_entries[code] = product
                s.execute(
                    text(
                        "INSERT OR REPLACE INTO concept_product_cache "
                        "(ts_code, concept_name, product_summary, updated_at) "
                        "VALUES (:c, :concept, :summary, :ts)"
                    ),
                    {"c": code, "concept": concept_name, "summary": product, "ts": now},
                )
        s.commit()

    logger.info(
        "产品摘要已生成: concept=%s, total=%d, new=%d",
        concept_name,
        len(missing),
        len(new_entries),
    )
    return {**cached, **new_entries}
