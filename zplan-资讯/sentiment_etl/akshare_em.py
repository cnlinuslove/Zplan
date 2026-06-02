from __future__ import annotations

import logging
import os
import time
from datetime import date, time as dtime
from typing import Iterable

import akshare as ak
import pandas as pd

from config import (
    AKSHARE_RATE_LIMIT_SECONDS,
    SENTIMENT_INDEX_HIST_DAYS,
    SENTIMENT_INDEX_SYMBOLS,
    SENTIMENT_NORTHBOUND_INTRADAY,
)
from sentiment_etl.timeutil import (
    combine_cn_date_time_to_utc_naive,
    parse_published_to_utc_naive,
    to_json_text,
    trade_date_to_utc_midnight_trade_bucket,
)

logger = logging.getLogger(__name__)

# 仅允许使用 AkShare 中东方财富（East Money）数据源接口（函数名含 _em 或东财文档中的 index_zh_a_hist）
_AK_EM_FLASH = "stock_info_global_em"
_AK_NORTHBOUND_HIST = "stock_hsgt_hist_em"
_AK_NORTHBOUND_MIN = "stock_hsgt_fund_min_em"
_AK_MARGIN_ACCOUNT = "stock_margin_account_info"
_AK_INDEX_HIST = "index_zh_a_hist"
_AK_INDEX_TX = "stock_zh_index_daily_tx"

# 东财 index_zh_a_hist 需 push2 映射表；不可用时走腾讯日线
_INDEX_TX_SYMBOL: dict[str, str] = {
    "000001": "sh000001",
    "399001": "sz399001",
    "399006": "sz399006",
}


def _sleep_rate() -> None:
    if AKSHARE_RATE_LIMIT_SECONDS > 0:
        time.sleep(AKSHARE_RATE_LIMIT_SECONDS)


def fetch_em_financial_flash_df() -> pd.DataFrame:
    """
    东方财富全球财经快讯（实时/近期）。
    标准列: published_at_utc, title, summary, article_url
    """
    raw = getattr(ak, _AK_EM_FLASH)()
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["published_at_utc", "title", "summary", "article_url"])

    out_rows = []
    for _, row in raw.iterrows():
        title = str(row.get("标题", "") or "").strip()
        summary = str(row.get("摘要", "") or "").strip() or None
        url = str(row.get("链接", "") or "").strip()
        pub = parse_published_to_utc_naive(row.get("发布时间"))
        if not title or not url:
            continue
        out_rows.append(
            {
                "published_at_utc": pub,
                "title": title,
                "summary": summary,
                "article_url": url,
            }
        )
    _sleep_rate()
    return pd.DataFrame(out_rows)


def fetch_em_northbound_daily_factors_df(max_rows: int = 120) -> pd.DataFrame:
    """
    北向资金日频（`stock_hsgt_hist_em`，单位见东财文档，数值原样入库）。
    长表: factor_kind, as_of_utc, subject, metric_name, metric_value, extra_json
    """
    raw = getattr(ak, _AK_NORTHBOUND_HIST)(symbol="北向资金")
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=[
                "factor_kind",
                "as_of_utc",
                "subject",
                "metric_name",
                "metric_value",
                "extra_json",
            ]
        )
    raw = raw.tail(int(max_rows))
    numeric_cols = [
        c
        for c in raw.columns
        if c != "日期" and pd.api.types.is_numeric_dtype(raw[c])  # type: ignore[attr-defined]
    ]
    rows: list[dict] = []
    for _, r in raw.iterrows():
        d_raw = r.get("日期")
        trade_d = pd.to_datetime(d_raw, errors="coerce")
        if pd.isna(trade_d):
            continue
        td = trade_d.date()
        as_of = trade_date_to_utc_midnight_trade_bucket(td)
        extras = {k: r.get(k) for k in raw.columns if k != "日期"}
        for col in numeric_cols:
            val = r.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "factor_kind": "northbound_daily",
                    "as_of_utc": as_of,
                    "subject": "northbound",
                    "metric_name": str(col),
                    "metric_value": fv,
                    "extra_json": to_json_text(extras),
                }
            )
    _sleep_rate()
    return pd.DataFrame(rows)


def fetch_em_northbound_intraday_factors_df(max_rows: int = 240) -> pd.DataFrame:
    """北向分时（万元，东财 `stock_hsgt_fund_min_em`）。"""
    if not SENTIMENT_NORTHBOUND_INTRADAY:
        return pd.DataFrame(
            columns=[
                "factor_kind",
                "as_of_utc",
                "subject",
                "metric_name",
                "metric_value",
                "extra_json",
            ]
        )
    raw = getattr(ak, _AK_NORTHBOUND_MIN)(symbol="北向资金")
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=[
                "factor_kind",
                "as_of_utc",
                "subject",
                "metric_name",
                "metric_value",
                "extra_json",
            ]
        )
    raw = raw.tail(int(max_rows))
    rows: list[dict] = []
    for _, r in raw.iterrows():
        day = pd.to_datetime(r.get("日期"), errors="coerce")
        if pd.isna(day):
            continue
        tstr = str(r.get("时间", "15:00") or "15:00")
        parts = tstr.split(":")
        hh = int(parts[0]) if parts and parts[0].isdigit() else 15
        mm = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 0
        ss = int(parts[2]) if len(parts) > 2 and str(parts[2]).isdigit() else 0
        as_of = combine_cn_date_time_to_utc_naive(day.date(), dtime(hh, mm, ss))
        for col in ("沪股通", "深股通", "北向资金"):
            if col not in raw.columns:
                continue
            val = r.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "factor_kind": "northbound_intraday",
                    "as_of_utc": as_of,
                    "subject": "northbound",
                    "metric_name": col,
                    "metric_value": fv,
                    "extra_json": None,
                }
            )
    _sleep_rate()
    return pd.DataFrame(rows)


def fetch_em_margin_market_factors_df(max_rows: int = 120) -> pd.DataFrame:
    """全市场两融账户统计（`stock_margin_account_info`，东财数据中心）。"""
    raw = getattr(ak, _AK_MARGIN_ACCOUNT)()
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=[
                "factor_kind",
                "as_of_utc",
                "subject",
                "metric_name",
                "metric_value",
                "extra_json",
            ]
        )
    raw = raw.tail(int(max_rows))
    numeric_cols = [
        c
        for c in raw.columns
        if c != "日期" and pd.api.types.is_numeric_dtype(raw[c])  # type: ignore[attr-defined]
    ]
    rows: list[dict] = []
    for _, r in raw.iterrows():
        trade_d = pd.to_datetime(r.get("日期"), errors="coerce")
        if pd.isna(trade_d):
            continue
        as_of = trade_date_to_utc_midnight_trade_bucket(trade_d.date())
        extras = {k: r.get(k) for k in raw.columns if k != "日期"}
        for col in numeric_cols:
            val = r.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "factor_kind": "margin_account",
                    "as_of_utc": as_of,
                    "subject": "all_market",
                    "metric_name": str(col),
                    "metric_value": fv,
                    "extra_json": to_json_text(extras),
                }
            )
    _sleep_rate()
    return pd.DataFrame(rows)


def _index_turnover_rows_from_em(sym: str, start_s: str, end_s: str) -> list[dict]:
    raw = getattr(ak, _AK_INDEX_HIST)(
        symbol=str(sym),
        period="daily",
        start_date=start_s,
        end_date=end_s,
    )
    if raw is None or raw.empty or "换手率" not in raw.columns:
        return []
    out: list[dict] = []
    for _, r in raw.iterrows():
        trade_d = pd.to_datetime(r.get("日期"), errors="coerce")
        if pd.isna(trade_d):
            continue
        val = r.get("换手率")
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "factor_kind": "index_turnover",
                "as_of_utc": trade_date_to_utc_midnight_trade_bucket(trade_d.date()),
                "subject": str(sym),
                "metric_name": "换手率",
                "metric_value": fv,
                "extra_json": None,
            }
        )
    return out


def _index_turnover_rows_from_tx(sym: str, n_days: int) -> list[dict]:
    """东财不可用时：腾讯指数日线成交额作情绪代理（metric=成交额）。"""
    tx_sym = _INDEX_TX_SYMBOL.get(str(sym))
    if not tx_sym:
        return []
    raw = getattr(ak, _AK_INDEX_TX)(symbol=tx_sym)
    if raw is None or raw.empty:
        return []
    tail = raw.tail(n_days + 5)
    out: list[dict] = []
    for _, r in tail.iterrows():
        trade_d = pd.to_datetime(r.get("date"), errors="coerce")
        if pd.isna(trade_d):
            continue
        val = r.get("amount")
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "factor_kind": "index_turnover",
                "as_of_utc": trade_date_to_utc_midnight_trade_bucket(trade_d.date()),
                "subject": str(sym),
                "metric_name": "成交额",
                "metric_value": fv,
                "extra_json": to_json_text({"fallback": "stock_zh_index_daily_tx"}),
            }
        )
    return out


def fetch_em_index_turnover_df(
    symbols: Iterable[str] | None = None,
    days: int | None = None,
) -> pd.DataFrame:
    """
    主要指数日频换手率（东财 `index_zh_a_hist`）；失败时用腾讯成交额代理。
    """
    syms = list(symbols) if symbols is not None else list(SENTIMENT_INDEX_SYMBOLS)
    n_days = int(days or SENTIMENT_INDEX_HIST_DAYS)
    end = date.today()
    start = end - pd.Timedelta(days=n_days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    rows: list[dict] = []
    prefer_tx = os.getenv("AKSHARE_INDEX_PREFER_TX", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    for sym in syms:
        got: list[dict] = []
        if not prefer_tx:
            try:
                got = _index_turnover_rows_from_em(sym, start_s, end_s)
            except Exception as exc:  # noqa: BLE001
                logger.warning("index_zh_a_hist 失败 symbol=%s: %s", sym, exc)
        if not got:
            try:
                got = _index_turnover_rows_from_tx(sym, n_days)
                if got:
                    logger.info("[INFO] 指数 %s 使用腾讯成交额代理 %s 条", sym, len(got))
            except Exception as exc:  # noqa: BLE001
                logger.warning("stock_zh_index_daily_tx 失败 symbol=%s: %s", sym, exc)
        rows.extend(got)
        _sleep_rate()
    return pd.DataFrame(rows)
