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
