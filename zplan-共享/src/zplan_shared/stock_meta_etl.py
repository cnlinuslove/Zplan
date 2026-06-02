"""回填 stock_list.industry / listing_date（东财行业板块 + 沪深京上市日）。"""
from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import func, select, update
from tenacity import retry, stop_after_attempt, wait_exponential

from zplan_shared.http_client import configure_akshare_http, throttle, _make_session
from zplan_shared.models import SessionLocal, StockList, init_db

logger = logging.getLogger(__name__)

_EM_SPOT_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_EM_SPOT_HOSTS = (
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://17.push2.eastmoney.com",
)


def _parse_listing_date(val: object) -> date | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        ts = pd.Timestamp(val)
        if pd.isna(ts):
            return None
        return ts.date()
    except (ValueError, TypeError):
        return None


def _meta_http_session() -> Any:
    """元数据拉取用：无 urllib3 自动重试，避免单页卡数分钟。"""
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/",
        }
    )
    adapter = HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.trust_env = False
    return session


def _fetch_spot_page(session: Any, page: int, page_size: int) -> tuple[list[dict], int]:
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": _EM_SPOT_FS,
        "fields": "f12,f100",
    }
    last_exc: Exception | None = None
    for host in _EM_SPOT_HOSTS:
        try:
            resp = session.get(f"{host}/api/qt/clist/get", params=params, timeout=45)
            resp.raise_for_status()
            data = (resp.json().get("data") or {})
            return list(data.get("diff") or []), int(data.get("total") or 0)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            throttle(0.5)
    if last_exc:
        raise last_exc
    return [], 0


def _fetch_industry_map_em_spot() -> dict[str, str]:
    """东财 A 股现货列表直连（f12=代码, f100=行业），分页失败可重试。"""
    from typing import Any

    session = _meta_http_session()
    out: dict[str, str] = {}
    page_size = 100
    total = 0
    page = 1
    max_pages = int(os.getenv("STOCK_META_MAX_PAGES", "80") or "80")
    failed_streak = 0
    while page <= max_pages and (page == 1 or (total and (page - 1) * page_size < total)):
        diff: list[dict] = []
        for attempt in range(2):
            try:
                diff, total = _fetch_spot_page(session, page, page_size)
                failed_streak = 0
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= 1:
                    logger.warning("东财现货第 %s 页失败: %s", page, exc)
                    failed_streak += 1
                else:
                    throttle(0.8)
        if failed_streak >= 5:
            logger.warning("东财现货连续 %s 页失败，保留已拉 %s 条行业后结束", failed_streak, len(out))
            break
        if not diff:
            page += 1
            continue
        for row in diff:
            code = str(row.get("f12", "")).strip().zfill(6)
            ind = row.get("f100")
            if len(code) == 6 and code.isdigit() and ind and str(ind).strip() not in ("", "-"):
                out[code] = str(ind).strip()[:64]
        if total and page * page_size >= total:
            break
        page += 1
        throttle(0.15)
    logger.info("[INFO] 东财现货列表行业 %s 只（total≈%s）", len(out), total)
    return out


def _secid_for_code(code: str) -> str:
    c = code.zfill(6)
    if c.startswith(("5", "6", "9")):
        return f"1.{c}"
    return f"0.{c}"


def _fetch_industry_one_em_direct(session: Any, code: str) -> str | None:
    params = {
        "secid": _secid_for_code(code),
        "fields": "f100,f57,f58",
        "fltt": "2",
        "invt": "2",
    }
    for host in _EM_SPOT_HOSTS:
        try:
            resp = session.get(f"{host}/api/qt/stock/get", params=params, timeout=20)
            if resp.status_code >= 500:
                continue
            resp.raise_for_status()
            data = (resp.json().get("data") or {})
            ind = data.get("f100")
            if ind and str(ind).strip() not in ("", "-"):
                return str(ind).strip()[:64]
            return None
        except Exception:  # noqa: BLE001
            throttle(0.3)
    return None


def _fetch_industry_map_em_individual(codes: list[str]) -> dict[str, str]:
    """东财个股 f100 行业（push2 列表不可用时的上交所补源）。"""
    session = _meta_http_session()
    out: dict[str, str] = {}
    sleep_s = float(os.getenv("STOCK_META_INDIVIDUAL_SLEEP", "0.6") or "0.6")
    max_n = int(os.getenv("STOCK_META_INDIVIDUAL_MAX", "0") or "0")
    fail_streak = 0
    todo = codes[:max_n] if max_n > 0 else codes
    for idx, code in enumerate(todo):
        if idx and sleep_s > 0:
            time.sleep(sleep_s)
        ind = _fetch_industry_one_em_direct(session, code)
        if ind:
            out[code] = ind
            fail_streak = 0
            if len(out) % 50 == 0:
                backfill_stock_list_meta_incremental(out, {}, only_missing=True)
        else:
            fail_streak += 1
            if fail_streak >= 8:
                logger.warning("东财个股行业连续失败 %s 次，中止（已得 %s 条）", fail_streak, len(out))
                break
    logger.info("[INFO] 东财个股行业 %s/%s 只", len(out), len(todo))
    return out


def _codes_missing_industry() -> list[str]:
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockList.ts_code).where(StockList.industry.is_(None)).order_by(StockList.ts_code)
        ).all()
    return [str(r[0]).zfill(6) for r in rows]


def backfill_stock_list_meta_incremental(
    industry_map: dict[str, str],
    listing_map: dict[str, date],
    *,
    only_missing: bool = True,
) -> dict[str, int]:
    """将已拉取的 meta 写入库（支持分页中途落库）。"""
    init_db()
    updated_industry = 0
    updated_listing = 0
    with SessionLocal() as session:
        rows = session.execute(select(StockList.ts_code, StockList.industry, StockList.listing_date)).all()
        for ts_code, cur_ind, cur_list in rows:
            code = str(ts_code).zfill(6)
            sets: dict[str, object] = {}
            new_ind = industry_map.get(code)
            new_list = listing_map.get(code)
            if new_ind and (not only_missing or cur_ind is None):
                sets["industry"] = new_ind[:64]
            if new_list and (not only_missing or cur_list is None):
                sets["listing_date"] = new_list
            if sets:
                session.execute(update(StockList).where(StockList.ts_code == code).values(**sets))
                if "industry" in sets:
                    updated_industry += 1
                if "listing_date" in sets:
                    updated_listing += 1
        session.commit()
    return {"updated_industry": updated_industry, "updated_listing_date": updated_listing}


@retry(stop=stop_after_attempt(3))
def _fetch_industry_map_em_boards() -> dict[str, str]:
    """东财行业板块成份 → {ts_code: 行业名}。"""
    configure_akshare_http()
    boards = ak.stock_board_industry_name_em()
    name_col = "板块名称" if "板块名称" in boards.columns else boards.columns[1]
    industries = [str(x).strip() for x in boards[name_col].tolist() if str(x).strip()]
    out: dict[str, str] = {}
    sleep_s = float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "2") or "2")
    for idx, ind_name in enumerate(industries):
        if idx:
            time.sleep(sleep_s)
        try:
            cons = ak.stock_board_industry_cons_em(symbol=ind_name)
            code_col = "代码" if "代码" in cons.columns else cons.columns[12]
            for code in cons[code_col].astype(str):
                c = code.strip().zfill(6)
                if len(c) == 6 and c.isdigit():
                    out[c] = ind_name
        except Exception as exc:  # noqa: BLE001
            logger.warning("行业成份 %s 失败: %s", ind_name, exc)
    logger.info("[INFO] 东财行业映射 %s 只", len(out))
    return out


_SZSE_A_SHARE_XLSX = (
    "http://www.szse.cn/api/report/ShowReport"
    "?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1"
)


def _fetch_listing_industry_sz_direct() -> pd.DataFrame:
    """深交所 A 股列表：官网 xlsx（AkShare 常因 Content-Type 解析失败）。"""
    import io

    import requests

    session = _meta_http_session()
    session.headers.update(
        {
            "Referer": "http://www.szse.cn/",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        }
    )
    try:
        resp = session.get(_SZSE_A_SHARE_XLSX, timeout=60)
        resp.raise_for_status()
        df = pd.read_excel(io.BytesIO(resp.content), engine="openpyxl")
    except Exception as exc:  # noqa: BLE001
        logger.warning("深交所 xlsx 直连失败: %s", exc)
        return pd.DataFrame(columns=["ts_code", "listing_date", "industry"])
    if df.empty or "A股代码" not in df.columns:
        return pd.DataFrame(columns=["ts_code", "listing_date", "industry"])
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("A股代码", "")).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        rows.append(
            {
                "ts_code": code,
                "listing_date": _parse_listing_date(r.get("A股上市日期")),
                "industry": str(r.get("所属行业", "") or "").strip() or None,
            }
        )
    logger.info("[INFO] 深交所 A 股列表 %s 只（直连 xlsx）", len(rows))
    return pd.DataFrame(rows)


def _fetch_listing_industry_sz() -> pd.DataFrame:
    direct = _fetch_listing_industry_sz_direct()
    if not direct.empty:
        return direct
    configure_akshare_http()
    try:
        df = ak.stock_info_sz_name_code(symbol="A股列表")
    except Exception as exc:  # noqa: BLE001
        logger.warning("深交所列表失败（常因 403/非 xlsx）: %s", exc)
        return pd.DataFrame(columns=["ts_code", "listing_date", "industry"])
    if df.empty or "A股代码" not in df.columns:
        return pd.DataFrame(columns=["ts_code", "listing_date", "industry"])
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("A股代码", "")).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        rows.append(
            {
                "ts_code": code,
                "listing_date": _parse_listing_date(r.get("A股上市日期")),
                "industry": str(r.get("所属行业", "") or "").strip() or None,
            }
        )
    return pd.DataFrame(rows)


def _fetch_listing_sh() -> pd.DataFrame:
    configure_akshare_http()
    parts: list[pd.DataFrame] = []
    for board in ("主板A股", "科创板"):
        try:
            df = ak.stock_info_sh_name_code(symbol=board)
            if not df.empty:
                parts.append(df)
        except Exception as exc:  # noqa: BLE001
            logger.warning("上交所列表 %s 失败: %s", board, exc)
    if not parts:
        return pd.DataFrame(columns=["ts_code", "listing_date"])
    merged = pd.concat(parts, ignore_index=True)
    rows = []
    for _, r in merged.iterrows():
        code = str(r.get("证券代码", "")).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        rows.append(
            {
                "ts_code": code,
                "listing_date": _parse_listing_date(r.get("上市日期")),
            }
        )
    return pd.DataFrame(rows)


def _fetch_listing_industry_bj() -> pd.DataFrame:
    configure_akshare_http()
    try:
        df = ak.stock_info_bj_name_code()
    except Exception as exc:  # noqa: BLE001
        logger.warning("北交所列表失败: %s", exc)
        return pd.DataFrame(columns=["ts_code", "listing_date", "industry"])
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("证券代码", "")).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        rows.append(
            {
                "ts_code": code,
                "listing_date": _parse_listing_date(r.get("上市日期")),
                "industry": str(r.get("所属行业", "") or "").strip() or None,
            }
        )
    return pd.DataFrame(rows)


def build_stock_meta_frames(
    *,
    include_em_industry: bool = True,
    flush_each_phase: bool = False,
    only_missing: bool = True,
) -> tuple[dict[str, str], dict[str, date], dict[str, str]]:
    """合并行业（东财现货列表优先）与上市日（沪深京交易所）。"""
    industry: dict[str, str] = {}
    industry_boards: dict[str, str] = {}
    listing: dict[str, date] = {}

    listing_frames = [_fetch_listing_sh(), _fetch_listing_industry_bj(), _fetch_listing_industry_sz()]
    for frame in listing_frames:
        if frame.empty:
            continue
        for _, r in frame.iterrows():
            code = str(r["ts_code"])
            if r.get("listing_date") is not None:
                listing[code] = r["listing_date"]
            ind = r.get("industry") if "industry" in frame.columns else None
            if ind:
                industry[code] = str(ind)
    if flush_each_phase:
        backfill_stock_list_meta_incremental(industry, listing, only_missing=only_missing)
        logger.info("[INFO] 上市日/交易所行业已落库")

    skip_em = os.getenv("STOCK_META_SKIP_EM", "").strip().lower() in ("1", "true", "yes")
    if include_em_industry and not skip_em:
        try:
            spot_ind = _fetch_industry_map_em_spot()
            industry.update(spot_ind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财现货行业列表失败: %s", exc)
        if flush_each_phase and industry:
            backfill_stock_list_meta_incremental(industry, listing, only_missing=only_missing)
            logger.info("[INFO] 东财现货行业已落库")
        try:
            industry_boards = _fetch_industry_map_em_boards()
            for code, ind in industry_boards.items():
                industry.setdefault(code, ind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财行业板块映射失败（可忽略）: %s", exc)
    elif include_em_industry and skip_em:
        logger.info("[INFO] STOCK_META_SKIP_EM=1，跳过东财行业拉取")

    if include_em_industry and os.getenv("STOCK_META_SH_INDIVIDUAL_EM", "1").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        missing = [c for c in _codes_missing_industry() if c.startswith(("6", "9"))]
        if missing:
            indiv = _fetch_industry_map_em_individual(missing)
            industry.update(indiv)
            if flush_each_phase and indiv:
                backfill_stock_list_meta_incremental(industry, listing, only_missing=only_missing)
                logger.info("[INFO] 上交所个股行业已落库 %s 条", len(indiv))

    return industry, listing, industry_boards


def backfill_stock_list_meta(
    *,
    only_missing: bool = True,
    include_em_industry: bool = True,
) -> dict[str, int]:
    """
    回填 stock_list.industry / listing_date。
    only_missing=True 时仅更新当前为 NULL 的字段。
    include_em_industry=False 时仅用沪深京交易所列表（东财板块失败时的降级）。
    """
    init_db()
    industry_map, listing_map, _ = build_stock_meta_frames(
        include_em_industry=include_em_industry,
        flush_each_phase=os.getenv("STOCK_META_FLUSH_EACH_PHASE", "1").strip().lower()
        in ("1", "true", "yes"),
        only_missing=only_missing,
    )
    updated_industry = 0
    updated_listing = 0

    with SessionLocal() as session:
        rows = session.execute(select(StockList.ts_code, StockList.industry, StockList.listing_date)).all()
        for ts_code, cur_ind, cur_list in rows:
            code = str(ts_code).zfill(6)
            new_ind = industry_map.get(code)
            new_list = listing_map.get(code)
            sets: dict[str, object] = {}
            if new_ind and (not only_missing or cur_ind is None):
                sets["industry"] = new_ind[:64]
            if new_list and (not only_missing or cur_list is None):
                sets["listing_date"] = new_list
            if sets:
                session.execute(update(StockList).where(StockList.ts_code == code).values(**sets))
                if "industry" in sets:
                    updated_industry += 1
                if "listing_date" in sets:
                    updated_listing += 1
        session.commit()

        null_ind = session.execute(
            select(func.count()).select_from(StockList).where(StockList.industry.is_(None))
        ).scalar_one()
        null_list = session.execute(
            select(func.count()).select_from(StockList).where(StockList.listing_date.is_(None))
        ).scalar_one()
        total = session.execute(select(func.count()).select_from(StockList)).scalar_one()

    stats = {
        "updated_industry": updated_industry,
        "updated_listing_date": updated_listing,
        "null_industry_remaining": int(null_ind or 0),
        "null_listing_date_remaining": int(null_list or 0),
        "stock_list_total": int(total or 0),
        "industry_map_size": len(industry_map),
        "listing_map_size": len(listing_map),
    }
    logger.info("[INFO] stock_list 元数据回填: %s", stats)
    return stats
