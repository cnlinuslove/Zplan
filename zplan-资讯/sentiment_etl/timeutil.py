from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

CN_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cn_naive_to_utc_naive(dt: datetime) -> datetime:
    """无时区 naive datetime 视为上海本地，转为 UTC naive。"""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(tzinfo=CN_TZ).astimezone(UTC).replace(tzinfo=None)


def parse_published_to_utc_naive(value: Any, assume_tz: str = "Asia/Shanghai") -> datetime:
    """解析字符串 / Timestamp；无时区则按 assume_tz 本地化后转 UTC naive。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return utc_now_naive()
    if isinstance(value, datetime):
        dt = value
    else:
        ts = pd.to_datetime(value, errors="coerce", utc=False)
        if pd.isna(ts):
            return utc_now_naive()
        dt = ts.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(assume_tz))
    return dt.astimezone(UTC).replace(tzinfo=None)


def trade_date_to_utc_midnight_trade_bucket(d: date) -> datetime:
    """日频因子：用该日 00:00 UTC 作为排序主键（业务含义为交易日标签）。"""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).replace(tzinfo=None)


def combine_cn_date_time_to_utc_naive(day: date, t: time) -> datetime:
    dt = datetime.combine(day, t, tzinfo=CN_TZ)
    return dt.astimezone(UTC).replace(tzinfo=None)


def to_json_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
