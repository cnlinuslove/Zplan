"""概念股产品摘要缓存：用 LLM 批量获取股票在某概念下的核心产品和市场地位。

表 ``concept_product_cache`` 按需创建，缓存 LLM 结果，后续同一概念筛选秒出。

v3：引入官方主营业务（巨潮资讯）作为 LLM 最高优先级 grounding，
   杜绝强行关联，诚实标注"无关"。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
    search_results TEXT,
    search_source VARCHAR(32),
    search_queried_at DATETIME,
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


def _derive_relevance(product_text: str) -> int:
    """从产品描述文本自动推导概念相关度（0-100），不依赖 LLM 打分。

    避免让 LLM 同时生成描述和打分 —— 打分任务会诱导 LLM 编造关联来证明高分。
    改为：从诚实的产品描述中提取信号。
    """
    text = (product_text or "").strip()
    if not text:
        return 0

    # "无关" → 0-10
    if text.startswith("无关") or text.startswith("无关："):
        return 5

    score = 30  # 基础分：有关联但未说明程度

    # 加分项（信号越强分越高）
    if any(kw in text for kw in ("龙头", "第一", "最大", "领先", "核心", "独角兽")):
        score += 30
    if any(kw in text for kw in ("子公司", "布局", "研发", "产品", "应用于")):
        score += 15
    if any(kw in text for kw in ("市占率", "市场份额", "产能", "国内")):
        score += 15
    if any(kw in text for kw in ("可能", "可应用于", "涉及", "布局")):
        score -= 15  # 不确定语言 → 降分

    return max(0, min(100, score))


def get_concept_relevance_scores(
    ts_codes: list[str],
    concept_name: str,
) -> dict[str, int]:
    """从缓存获取概念相关度评分（LLM 打分，0-100）。

    未缓存的 code 不在 key 中。0=完全无关，100=核心龙头。
    """
    _ensure_table()
    if not ts_codes:
        return {}
    with SessionLocal() as s:
        placeholders = ",".join(f":c{i}" for i in range(len(ts_codes)))
        rows = s.execute(
            text(
                f"SELECT ts_code, relevance_score FROM concept_product_cache "
                f"WHERE ts_code IN ({placeholders}) AND concept_name = :concept "
                f"AND relevance_score IS NOT NULL"
            ),
            {**{f"c{i}": c for i, c in enumerate(ts_codes)}, "concept": concept_name},
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows if r[1] is not None}


def _fetch_main_businesses(ts_codes: list[str]) -> dict[str, str]:
    """从 company_profiles 获取官方主营业务（巨潮资讯）。"""
    if not ts_codes:
        return {}
    try:
        with SessionLocal() as s:
            placeholders = ",".join(f":c{i}" for i in range(len(ts_codes)))
            rows = s.execute(
                text(
                    f"SELECT ts_code, main_business, short_name FROM company_profiles "
                    f"WHERE ts_code IN ({placeholders})"
                ),
                {f"c{i}": c for i, c in enumerate(ts_codes)},
            ).fetchall()
        result: dict[str, str] = {}
        for r in rows:
            biz = (r[1] or "").strip()
            if biz:
                result[r[0]] = biz
            else:
                # fallback to stock_list name
                name = (r[2] or "").strip()
                if name:
                    result[r[0]] = f"（暂无主营业务数据）{name}"
        return result
    except Exception as exc:
        logger.warning("主营业务查询失败: %s", exc)
        return {}


def _collect_web_search_context(
    ts_codes: list[str],
    concept_name: str,
    names: dict[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    """为每只股票搜索 Web 信息。"""
    try:
        from zplan_shared.web_search import (
            search_company_concept,
            format_search_results,
        )
    except ImportError:
        logger.warning("web_search 模块不可用")
        return {}, {}

    search_texts: dict[str, str] = {}
    search_meta: dict[str, Any] = {}

    for code in ts_codes:
        name = names.get(code, "")
        if not name:
            continue
        try:
            results = search_company_concept(name, concept_name)
        except Exception as exc:
            logger.warning("Web 搜索失败 %s %s: %s", code, name, exc)
            continue
        if results:
            search_texts[code] = format_search_results(results)
            search_meta[code] = {
                "results_json": json.dumps(results, ensure_ascii=False),
                "source": results[0].get("source", "unknown") if results else "unknown",
            }

    return search_texts, search_meta


def _build_llm_prompt(
    ts_codes: list[str],
    concept_name: str,
    names: dict[str, str],
    main_biz: dict[str, str],
    search_contexts: dict[str, str],
) -> str:
    """构建 LLM prompt，以官方主营业务为最高优先级 grounding。

    三层信息源（优先级从高到低）：
    1. 官方主营业务（巨潮资讯）— 必须优先采信
    2. 网络搜索结果 — 辅助参考
    3. LLM 自身知识 — 兜底
    """
    name_map = names or {}
    stock_list = "\n".join(
        f"- {c} {name_map.get(c, '')}"
        for c in ts_codes
    )

    prompt_parts = [
        f"以下是 A 股「{concept_name}」概念板块的部分成分股。",
        f"注意：概念板块成分股可能包含与概念无关的公司（纯蹭概念），请严格基于事实判断。",
        f"",
        f"【官方主营业务 — 来自巨潮资讯，必须优先采信】",
    ]

    for code in ts_codes:
        biz = main_biz.get(code, "")
        name = name_map.get(code, "")
        if biz:
            prompt_parts.append(f"  {code} {name}：{biz}")
        else:
            prompt_parts.append(f"  {code} {name}：（暂无官方数据）")

    prompt_parts.append("")

    # Web search（次要参考）
    if search_contexts:
        prompt_parts.append("【网络搜索结果 — 仅供参考，真实性自行判断】")
        for code in ts_codes:
            ctx = search_contexts.get(code)
            name = name_map.get(code, "")
            if ctx:
                prompt_parts.append(f"\n▸ {code} {name}：")
                prompt_parts.append(ctx)
    else:
        prompt_parts.append("【网络搜索结果】（暂无）")

    prompt_parts.append("")
    prompt_parts.append("【任务】")
    prompt_parts.append(
        f"对每只股票，判断其与「{concept_name}」的真实关联度。"
        f"「官方主营业务」是巨潮资讯披露的权威数据，必须作为基础判断依据，"
        f"但描述可能笼统（如\"传感器业务\"未细分品类）。"
    )
    prompt_parts.append("")
    prompt_parts.append(
        "判断规则："
    )
    prompt_parts.append(
        "1. 先看主营业务，再看你对这家公司的了解。两者结合做出判断。"
    )
    prompt_parts.append(
        f"2. 若该公司确实有与「{concept_name}」相关的产品/技术/业务 → "
        "product 填一句话概括（15-30字），具体到产品名称和核心地位。"
        "即使主营业务描述笼统（如\"传感器业务\"），只要确有其事就要写出来。"
    )
    prompt_parts.append(
        f"3. 若该公司完全与「{concept_name}」无关（如做汽车轴承的与脑科学无关）→ "
        'product 填 "无关：主营业务为XXX"'
    )
    prompt_parts.append(
        "4. 严禁编造产品。但严禁把\"主营业务描述笼统\"等同于\"无关\"。"
        "例如汉威科技主营只写\"传感器\"，但其子公司确实做脑机接口传感器，这就必须写出来。"
    )

    prompt_parts.append("")
    prompt_parts.append("【成分股列表】")
    prompt_parts.append(stock_list)
    prompt_parts.append("")
    prompt_parts.append(
        '返回 JSON 对象：{"items": [{"ts_code": "002396", "product": "无关：主营业务为网络通讯设备"}'
        ', {"ts_code": "300007", "product": "脑机接口柔性传感器，国内领先"}]}'
    )

    return "\n".join(prompt_parts)


def generate_product_summaries(
    ts_codes: list[str],
    concept_name: str,
    *,
    names: dict[str, str] | None = None,
    force_refresh: bool = False,
    use_web_search: bool = True,
) -> dict[str, str]:
    """用 LLM 批量生成股票在指定概念下的产品摘要。

    Args:
        ts_codes: 股票代码列表
        concept_name: 概念名称
        names: {ts_code: name} 映射
        force_refresh: 强制刷新缓存
        use_web_search: 是否使用 Web Search 增强

    Returns:
        {ts_code: product_summary} 完整映射（含缓存命中和新生成的）
    """
    _ensure_table()

    cached = get_product_summaries(ts_codes, concept_name)
    if not force_refresh:
        missing = [c for c in ts_codes if c not in cached]
    else:
        missing = ts_codes
        cached = {}

    if not missing:
        return cached

    # ── ground-truth：官方主营业务 ──
    name_map = names or {}
    main_biz = _fetch_main_businesses(missing)

    # ── 名称映射（优先用 profile short_name，回退到传入 names）──
    for code in missing:
        if code not in name_map and code in main_biz:
            # name_map 不需要 main_business 内容，只是股票名
            pass  # names 需要外部传入，这里只是确保 code 都有 name

    # ── Web Search（辅助参考）──
    search_contexts: dict[str, str] = {}
    search_meta: dict[str, Any] = {}

    if use_web_search:
        try:
            search_contexts, search_meta = _collect_web_search_context(
                missing, concept_name, name_map
            )
        except Exception as exc:
            logger.warning("Web 搜索阶段失败（不影响后续流程）: %s", exc)

    # ── LLM ──
    try:
        from zplan_shared.llm.gemini import generate_json, llm_available

        if not llm_available():
            logger.warning("LLM 不可用，跳过产品摘要生成")
            return cached
    except Exception:
        return cached

    prompt = _build_llm_prompt(missing, concept_name, name_map, main_biz, search_contexts)

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

    # ── 写入缓存 ──
    new_entries: dict[str, str] = {}
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as s:
        for item in items:
            code = item.get("ts_code", "")
            product = item.get("product", "")
            if code and product and code in missing:
                new_entries[code] = product
                relevance = _derive_relevance(product)
                meta = search_meta.get(code, {})
                s.execute(
                    text(
                        "INSERT OR REPLACE INTO concept_product_cache "
                        "(ts_code, concept_name, product_summary, "
                        " search_results, search_source, search_queried_at, "
                        " relevance_score, updated_at) "
                        "VALUES (:c, :concept, :summary, :sr, :ss, :sq, :rel, :ts)"
                    ),
                    {
                        "c": code,
                        "concept": concept_name,
                        "summary": product,
                        "sr": meta.get("results_json"),
                        "ss": meta.get("source"),
                        "sq": now if meta else None,
                        "rel": relevance,
                        "ts": now,
                    },
                )
        s.commit()

    logger.info(
        "产品摘要已生成: concept=%s, total=%d, new=%d, web_searched=%d",
        concept_name,
        len(missing),
        len(new_entries),
        len(search_contexts),
    )
    return {**cached, **new_entries}
