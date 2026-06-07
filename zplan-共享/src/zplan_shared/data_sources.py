"""AkShare 数据类与固定 ``source`` 标识（同一类数据不混源）。"""
from __future__ import annotations

import os
from typing import Literal

DailyProvider = Literal["em", "tx", "sina"]

# 日线：可配置，全库统一用同一 provider
DAILY_SOURCE_EM = "akshare_em"
DAILY_SOURCE_TX = "akshare_tx"
DAILY_SOURCE_SINA = "akshare_sina"

# 分时：AkShare 仅东财接口，固定
INTRADAY_SOURCE = "akshare_em"

_PROVIDER_TO_TAG: dict[DailyProvider, str] = {
    "em": DAILY_SOURCE_EM,
    "tx": DAILY_SOURCE_TX,
    "sina": DAILY_SOURCE_SINA,
}

_PROVIDER_LABELS: dict[DailyProvider, str] = {
    "em": "东方财富",
    "tx": "腾讯证券",
    "sina": "新浪财经",
}


def daily_provider() -> DailyProvider:
    raw = os.getenv("AKSHARE_DAILY_PROVIDER", "em").strip().lower()
    if raw in ("tx", "tencent", "qq"):
        return "tx"
    if raw in ("sina", "sn"):
        return "sina"
    return "em"


def daily_source_tag() -> str:
    return _PROVIDER_TO_TAG[daily_provider()]


def daily_provider_label() -> str:
    return _PROVIDER_LABELS[daily_provider()]


# ── 港股 (HKEX) 数据源 ──────────────────────────────────────────
HK_DAILY_SOURCE_EM = "akshare_hk_em"
HK_DAILY_SOURCE_SINA = "akshare_hk_sina"

_HK_PROVIDER_TO_TAG: dict[str, str] = {
    "em": HK_DAILY_SOURCE_EM,
    "sina": HK_DAILY_SOURCE_SINA,
}
_HK_PROVIDER_LABELS: dict[str, str] = {
    "em": "东方财富（港股）",
    "sina": "新浪财经（港股）",
}


def hk_daily_provider() -> str:
    """港股日线数据源；默认 ``sina``（境外可访问），可选 ``em``（东财，境内更快）。"""
    raw = os.getenv("HK_DAILY_PROVIDER", "sina").strip().lower()
    if raw in ("em", "eastmoney", "east_money"):
        return "em"
    return "sina"


def hk_daily_source_tag() -> str:
    return _HK_PROVIDER_TO_TAG[hk_daily_provider()]


def hk_daily_provider_label() -> str:
    return _HK_PROVIDER_LABELS[hk_daily_provider()]
