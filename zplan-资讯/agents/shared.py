"""共享工具函数 — 股票检测、名称加载。

chat_engine.py 和 wechat_interact.py 共用，避免重复定义。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from models import SessionLocal, init_db
from zplan_shared.models import StockList

# 6 位股票代码（0/3/6/8 开头）
_CODE_RE = re.compile(r"(?<![0-9])([0368]\d{5})(?:\.(?:SH|SZ|BJ))?(?![0-9])")

# 简称匹配黑名单（媒体/平台，容易误匹配）
_NAME_BLOCKLIST = frozenset({
    "东方财富", "新华网", "同花顺", "财联社", "证券时报",
    "第一财经", "证券之星", "南方财经", "新浪财经", "雪球",
    "金融界", "和讯", "财新", "澎湃", "每经", "中证网",
    "机器人",
})

_stock_names_cache: dict[str, str] | None = None


def load_stock_names() -> dict[str, str]:
    """加载 stock_list 简称 → ts_code 字典（内存缓存）。"""
    global _stock_names_cache
    if _stock_names_cache is not None:
        return _stock_names_cache
    init_db()
    d: dict[str, str] = {}
    with SessionLocal() as session:
        for code, name in session.execute(
            select(StockList.ts_code, StockList.name)
        ):
            nm = str(name or "").strip()
            if len(nm) < 2:
                continue
            if nm in _NAME_BLOCKLIST:
                continue
            d[nm] = str(code).zfill(6)
    _stock_names_cache = dict(sorted(d.items(), key=lambda kv: -len(kv[0])))
    return _stock_names_cache


def find_stocks_in_text(text: str) -> list[tuple[str, str]]:
    """从用户消息中检测所有股票引用（三层匹配）。返回 [(code, name), ...] 不重复。

    Tier 1: 6 位代码
    Tier 2: 全名精确匹配（长名优先）
    Tier 3: 2 字 CJK 滑动窗口 → 子串匹配长名称（如"爱普"→"爱普股份"）
    """
    if not text:
        return []
    found: dict[str, str] = {}  # code → name

    # Tier 1: 6 位股票代码
    for m in _CODE_RE.finditer(text):
        code = m.group(1)
        if code not in found:
            with SessionLocal() as session:
                row = session.execute(
                    select(StockList.name).where(StockList.ts_code == code)
                ).first()
                found[code] = str(row[0]) if row and row[0] else code

    # Tier 2: 名称精确匹配（长名优先）
    names = load_stock_names()
    for nm, code in names.items():
        if nm in text and code not in found:
            found[code] = nm

    # Tier 3: 2 字滑动窗口 → 子串匹配长名称
    if not found:
        cjk_chars = re.findall(r"[一-鿿]", text)
        for i in range(len(cjk_chars) - 1):
            frag = cjk_chars[i] + cjk_chars[i + 1]
            if frag in _NAME_BLOCKLIST:
                continue
            for nm, code in names.items():
                if len(nm) >= 3 and frag in nm and code not in found:
                    found[code] = nm
                    break

    return [(code, name) for code, name in found.items()]
