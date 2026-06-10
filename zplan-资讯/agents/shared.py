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
_QUESTION_RE = re.compile(r"(怎么|如何|为什么|为何|什么|哪|吗|？|\?|最近|会不会|能不能|多少|是否|展望|影响)")

# 简称匹配黑名单（媒体/平台，容易误匹配）
_NAME_BLOCKLIST = frozenset({
    "东方财富", "新华网", "同花顺", "财联社", "证券时报",
    "第一财经", "证券之星", "南方财经", "新浪财经", "雪球",
    "金融界", "和讯", "财新", "澎湃", "每经", "中证网",
    "机器人",
})

# 股票名通用后缀/片段（2字组合），避免误匹配
# 这些二字组合出现在大量股票名中，不具备辨识度
_GENERIC_BIGRAMS = frozenset({
    "股份", "科技", "集团", "有限", "控股", "实业",
    "电子", "医药", "能源", "材料", "生物", "电气",
    "智能", "医疗", "汽车", "信息", "通讯", "环境",
    "资源", "机械", "食品", "传媒", "建设", "地产",
    "交通", "化工", "电力", "设备", "技术", "工程",
    "软件", "网络", "数据", "银行", "证券", "保险",
    "物流", "航空", "钢铁", "有色", "建材", "家电",
    "纺织", "服装", "农业", "旅游", "酒店", "餐饮",
    "环保", "水务", "燃气", "港口", "高速", "铁路",
    "矿业", "石油", "石化", "玻璃", "陶瓷", "造纸",
    "包装", "印刷", "塑料", "橡胶", "化纤", "仪器",
    "仪表", "光电", "光学", "半导体", "集成", "电路",
    "通信", "计算机", "互联网", "新材", "重工", "装备",
    "制造", "服务", "发展", "投资", "开发", "经营",
    "管理", "咨询", "设计", "检测", "认证", "租赁",
    "医药", "制药", "医疗", "健康", "生物", "基因",
    "芯片", "半导体", "新能源", "光伏", "锂电", "储能",
    "机器人", "人工", "智能", "大数据", "云计算",
    "消费", "零售", "餐饮", "教育", "娱乐", "传媒",
    "地产", "物业", "基建", "水泥", "钢铁", "煤炭",
    "航运", "物流", "快递", "电商", "支付", "金融",
    "基金", "信托", "期货", "外汇", "黄金", "白银",
    "信息", "软件", "硬件", "系统", "平台", "网络",
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

    # Tier 3: 滑动窗口匹配，短输入激进/长句保守
    if not found:
        cjk_chars = re.findall(r"[一-鿿]", text)
        cjk_only = "".join(cjk_chars)
        is_short = len(cjk_only) <= 4 and not _QUESTION_RE.search(text)

        if is_short:
            # 短输入：用户大概率在说股票名 → 2/3字子串匹配（优先开头匹配）
            for i in range(len(cjk_chars) - 1):
                frag2 = cjk_chars[i] + cjk_chars[i + 1]
                if frag2 in _GENERIC_BIGRAMS:
                    continue
                # 先找开头匹配的（更可能是用户意图）
                for nm, code in names.items():
                    if len(nm) >= 3 and nm.startswith(frag2) and code not in found:
                        found[code] = nm
                # 没找到开头匹配的 → 子串兜底（必须精确唯一匹配）
                if not found:
                    sub_matches = [
                        (code, nm) for nm, code in names.items()
                        if len(nm) >= 4 and frag2 in nm
                    ]
                    if len(sub_matches) == 1:
                        found[sub_matches[0][0]] = sub_matches[0][1]
            # 也试 3 字
            for i in range(len(cjk_chars) - 2):
                frag3 = cjk_chars[i] + cjk_chars[i + 1] + cjk_chars[i + 2]
                for nm, code in names.items():
                    if len(nm) >= 4 and nm.startswith(frag3) and code not in found:
                        found[code] = nm
                        break
        else:
            # 长句：仅 3 字片段 + 必须匹配开头（高精度）
            for i in range(len(cjk_chars) - 2):
                frag3 = cjk_chars[i] + cjk_chars[i + 1] + cjk_chars[i + 2]
                for nm, code in names.items():
                    if len(nm) >= 4 and nm.startswith(frag3) and code not in found:
                        found[code] = nm
                        break

    return [(code, name) for code, name in found.items()]
