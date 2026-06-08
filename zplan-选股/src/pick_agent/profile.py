"""公司档案只读（``company_profiles`` 表，资讯侧 enrichment 写入）。

列映射说明：
  - company_profiles.main_business / business_scope → core_products_json
  - company_profiles.industry_csrc / main_business → positioning
  - company_profiles.profile_json（AkShare 原始响应）→ team_json（尽力提取）
  - company_profiles.website → website
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import inspect, text

from zplan_shared.market import resolve_ts_code
from zplan_shared.models import SessionLocal, engine, init_db

logger = logging.getLogger(__name__)


def get_company_profile(ts_code: str) -> dict[str, Any] | None:
    """从 ``company_profiles`` 读取公司档案，返回 report.py 兼容的 dict。

    键：
      - positioning: 公司定位（行业 + 主营业务摘要）
      - core_products_json: 核心产品/经营范围
      - team_json: 创始团队（从 profile_json 提取或标注待扩展）
      - website: 官网 URL
      - name: 公司全称
      - industry_csrc: 证监会行业
    """
    init_db()
    if not inspect(engine).has_table("company_profiles"):
        logger.debug("company_profiles 表不存在")
        return None

    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        row = session.execute(
            text(
                "SELECT ts_code, full_name, short_name, website, "
                "main_business, business_scope, profile_json, "
                "industry_csrc, industry_sw, list_date, fetched_at "
                "FROM company_profiles WHERE ts_code = :c"
            ),
            {"c": code},
        ).mappings().first()

    if not row:
        return None

    out = dict(row)

    # ── 提取 profile_json 中的额外字段 ──
    raw_profile = {}
    if isinstance(out.get("profile_json"), str) and out["profile_json"]:
        try:
            raw_profile = json.loads(out["profile_json"])
        except json.JSONDecodeError:
            pass

    # ── 构建兼容 report.py 的键 ──
    main_biz = out.get("main_business") or ""
    biz_scope = out.get("business_scope") or ""
    industry_csrc = out.get("industry_csrc") or ""
    industry_sw = out.get("industry_sw") or ""

    # positioning: 行业 + 主营业务摘要
    positioning_parts = []
    if industry_csrc:
        positioning_parts.append(f"行业(CSRC): {industry_csrc}")
    if industry_sw and industry_sw != industry_csrc:
        positioning_parts.append(f"行业(申万): {industry_sw}")
    if main_biz:
        positioning_parts.append(f"主营: {main_biz[:200]}")
    positioning = "；".join(positioning_parts) if positioning_parts else None

    # core_products_json: 主营业务 + 经营范围
    products = {}
    if main_biz:
        products["主营业务"] = main_biz[:500]
    if biz_scope:
        products["经营范围"] = biz_scope[:500]
    # 尝试从 profile_json 提取更多产品信息
    for key_hint in ("主营构成", "产品", "业务构成", "核心产品"):
        val = raw_profile.get(key_hint)
        if val and str(val) not in ("nan", "None", ""):
            products[key_hint] = str(val)[:300]

    # team_json: 法人代表 + 注册资本 + 成立日期
    team = {}
    for src_key, target_label in (
        ("legal_rep", "法人代表"),
        ("registered_capital", "注册资本"),
        ("establish_date", "成立日期"),
    ):
        if out.get(src_key):
            team[target_label] = str(out[src_key])
    # 尝试从 profile_json 提取
    for key_hint in ("法人代表", "总经理", "董事长", "法定代表人", "高管", "董事会"):
        val = raw_profile.get(key_hint)
        if val and str(val) not in ("nan", "None", ""):
            team[key_hint] = str(val)[:200]

    return {
        "name": out.get("full_name") or out.get("short_name") or code,
        "website": out.get("website"),
        "positioning": positioning or "待资讯域补充",
        "core_products_json": json.dumps(products, ensure_ascii=False) if products else None,
        "team_json": json.dumps(team, ensure_ascii=False) if team else None,
        "sources_json": json.dumps({
            "source": "company_profiles (enrich_company.py)",
            "fetched_at": str(out.get("fetched_at") or ""),
            "industry_csrc": industry_csrc,
            "industry_sw": industry_sw,
            "list_date": str(out.get("list_date") or ""),
        }, ensure_ascii=False),
    }
