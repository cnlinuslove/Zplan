"""公司深度数据丰富：P0 产品/行业 + P1 研报/机构持仓。

数据源：
  P0: stock_profile_cninfo → 公司档案
  P0: 行业对比（基于 financial_indicators 自助计算）
  P1: stock_research_report_em → 机构研报
  P1: stock_main_stock_holder → 十大股东
  P1: stock_hsgt_individual_em → 北向资金
  P1: stock_report_fund_hold → 基金持仓汇总

用法：
  cd zplan-选股 && .venv/bin/python3 -m zplan_shared.enrich_company --symbol 300124
  cd zplan-选股 && .venv/bin/python3 -m zplan_shared.enrich_company --batch --top 500
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as dt_date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[3] / "zplan-资讯" / "zplan.db"


def _get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


# ═══════════════════════════════════════════════════════════════════════
# P0: 公司档案
# ═══════════════════════════════════════════════════════════════════════

def ensure_company_profile_table(db: sqlite3.Connection) -> None:
    db.execute("""
    CREATE TABLE IF NOT EXISTS company_profiles (
        ts_code VARCHAR(16) PRIMARY KEY,
        full_name VARCHAR(256),
        short_name VARCHAR(128),
        en_name VARCHAR(256),
        used_names TEXT,
        indices TEXT,
        market VARCHAR(32),
        industry_csrc VARCHAR(128),
        industry_sw VARCHAR(128),
        legal_rep VARCHAR(64),
        registered_capital VARCHAR(64),
        establish_date DATE,
        list_date DATE,
        website VARCHAR(256),
        email VARCHAR(128),
        phone VARCHAR(64),
        reg_address TEXT,
        office_address TEXT,
        main_business TEXT,
        business_scope TEXT,
        profile_json TEXT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)


def fetch_company_profile(ts_code: str) -> dict[str, Any] | None:
    """从 stock_profile_cninfo 拉取公司档案。"""
    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare 未安装")
        return None

    try:
        df = ak.stock_profile_cninfo(symbol=ts_code)
    except Exception as e:
        logger.warning("stock_profile_cninfo(%s) 失败: %s", ts_code, e)
        return None

    if df is None or df.empty:
        return None

    row: dict = {}
    try:
        row = df.iloc[0].to_dict()
    except Exception:
        logger.warning("stock_profile_cninfo(%s) 无法解析第一行", ts_code)
        return None

    if not row:
        return None

    col_map = {
        "full_name": ["公司名称", "证券简称(NAME)", "NAME"],
        "short_name": ["证券简称", "证券简称SNAME"],
        "en_name": ["英文名称"],
        "used_names": ["曾用简称"],
        "indices": ["入选指数"],
        "market": ["所属市场"],
        "industry_csrc": ["所属行业", "所属行业CSRC"],
        "industry_sw": ["所属行业SW"],
        "legal_rep": ["法人代表", "法定代表人"],
        "registered_capital": ["注册资本", "注册资本(万元)"],
        "establish_date": ["成立日期"],
        "list_date": ["上市日期"],
        "website": ["公司网址", "公司网站"],
        "email": ["电子邮箱", "邮箱"],
        "phone": ["联系电话", "电话"],
        "reg_address": ["注册地址"],
        "office_address": ["办公地址"],
        "main_business": ["主营业务"],
        "business_scope": ["经营范围", "机构简介"],
    }

    profile: dict[str, Any] = {"ts_code": ts_code}
    for key, candidates in col_map.items():
        for c in candidates:
            v = row.get(c)
            if v is not None and str(v) not in ("nan", "None", ""):
                profile[key] = str(v)
                break

    profile["profile_json"] = json.dumps(row, ensure_ascii=False, default=str)
    return profile


def save_company_profile(db: sqlite3.Connection, profile: dict[str, Any]) -> bool:
    try:
        db.execute("""
        INSERT OR REPLACE INTO company_profiles
            (ts_code, full_name, short_name, en_name, used_names, indices,
             market, industry_csrc, industry_sw, legal_rep, registered_capital,
             establish_date, list_date, website, email, phone,
             reg_address, office_address, main_business, business_scope,
             profile_json, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            profile.get("ts_code"),
            profile.get("full_name"), profile.get("short_name"), profile.get("en_name"),
            profile.get("used_names"), profile.get("indices"),
            profile.get("market"), profile.get("industry_csrc"), profile.get("industry_sw"),
            profile.get("legal_rep"), profile.get("registered_capital"),
            profile.get("establish_date"), profile.get("list_date"),
            profile.get("website"), profile.get("email"), profile.get("phone"),
            profile.get("reg_address"), profile.get("office_address"),
            profile.get("main_business"), profile.get("business_scope"),
            profile.get("profile_json"),
        ))
        return True
    except Exception as e:
        logger.error("入库 profile %s 失败: %s", profile.get("ts_code"), e)
        return False


# ═══════════════════════════════════════════════════════════════════════
# P0: 行业对比（自助计算，不依赖外部 API）
# ═══════════════════════════════════════════════════════════════════════

def ensure_industry_peers_table(db: sqlite3.Connection) -> None:
    db.execute("""
    CREATE TABLE IF NOT EXISTS industry_peers (
        ts_code VARCHAR(16) NOT NULL,
        as_of DATE NOT NULL,
        industry_name VARCHAR(128),
        peer_count INTEGER,
        rank_by_revenue INTEGER,
        rank_by_profit INTEGER,
        rank_by_pe_ttm INTEGER,
        rank_by_market_cap INTEGER,
        industry_med_pe FLOAT,
        industry_med_pb FLOAT,
        industry_med_roe FLOAT,
        peer_top3_names TEXT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (ts_code, as_of)
    )
    """)


def compute_industry_peers(db: sqlite3.Connection, ts_code: str, as_of: str = "2026-06-05") -> dict | None:
    """基于 stock_list.industry + financial_indicators/daily_snapshot 计算同业排名。"""
    # 获取行业
    cur = db.execute("SELECT industry FROM stock_list WHERE ts_code = ?", (ts_code,))
    row = cur.fetchone()
    if not row or not row["industry"]:
        return None
    industry = row["industry"]

    peers = db.execute("""
    SELECT sl.ts_code, fi.net_profit, fi.revenue, fi.roe,
           sn.pe_ttm, sn.pb, sn.total_mv
    FROM stock_list sl
    LEFT JOIN financial_indicators fi ON sl.ts_code = fi.ts_code AND fi.report_date = '2026-03-31'
    LEFT JOIN daily_snapshot sn ON sl.ts_code = sn.ts_code AND sn.trade_date = ?
    WHERE sl.industry = ? AND sl.market = 'a'
    """, (as_of, industry)).fetchall()

    if not peers:
        return None

    peer_count = len(peers)

    def _rank(items, key, desc=True):
        vals = [(p["ts_code"], p[key])
                for p in items if p[key] is not None and p[key] > 0]
        vals.sort(key=lambda x: x[1], reverse=desc)
        for i, (code, _) in enumerate(vals, 1):
            if code == ts_code:
                return i
        return -1

    def _median(items, key):
        vals = sorted([p[key] for p in items if p[key] is not None and p[key] > 0])
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    # Top 3 by revenue
    rev_sorted = sorted(
        [p for p in peers if p["revenue"] is not None and p["revenue"] > 0],
        key=lambda x: x["revenue"], reverse=True)
    top3 = [p["ts_code"] for p in rev_sorted[:3]]

    return {
        "ts_code": ts_code,
        "as_of": as_of,
        "industry_name": industry,
        "peer_count": peer_count,
        "rank_by_revenue": _rank(peers, "revenue"),
        "rank_by_profit": _rank(peers, "net_profit"),
        "rank_by_pe_ttm": _rank(peers, "pe_ttm", desc=False),
        "rank_by_market_cap": _rank(peers, "total_mv"),
        "industry_med_pe": _median(peers, "pe_ttm"),
        "industry_med_pb": _median(peers, "pb"),
        "industry_med_roe": _median(peers, "roe"),
        "peer_top3_names": json.dumps(top3, ensure_ascii=False),
    }


def save_industry_peers(db: sqlite3.Connection, data: dict) -> bool:
    try:
        db.execute("""
        INSERT OR REPLACE INTO industry_peers
            (ts_code, as_of, industry_name, peer_count,
             rank_by_revenue, rank_by_profit, rank_by_pe_ttm, rank_by_market_cap,
             industry_med_pe, industry_med_pb, industry_med_roe, peer_top3_names, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            data["ts_code"], data["as_of"], data["industry_name"], data["peer_count"],
            data["rank_by_revenue"], data["rank_by_profit"],
            data["rank_by_pe_ttm"], data["rank_by_market_cap"],
            data["industry_med_pe"], data["industry_med_pb"], data["industry_med_roe"],
            data["peer_top3_names"],
        ))
        return True
    except Exception as e:
        logger.error("入库 peers %s 失败: %s", data.get("ts_code"), e)
        return False


# ═══════════════════════════════════════════════════════════════════════
# P1: 研报摘要
# ═══════════════════════════════════════════════════════════════════════

def ensure_research_reports_table(db: sqlite3.Connection) -> None:
    db.execute("""
    CREATE TABLE IF NOT EXISTS research_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code VARCHAR(16) NOT NULL,
        report_date DATE,
        institution VARCHAR(128),
        rating VARCHAR(32),
        title TEXT,
        eps_2026 FLOAT,
        pe_2026 FLOAT,
        eps_2027 FLOAT,
        pe_2027 FLOAT,
        eps_2028 FLOAT,
        pe_2028 FLOAT,
        report_url TEXT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ts_code, report_date, institution, title)
    )
    """)


def fetch_research_reports(ts_code: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak
    except ImportError:
        return []

    try:
        df = ak.stock_research_report_em(symbol=ts_code)
    except Exception as e:
        logger.warning("research_report(%s) 失败: %s", ts_code, e)
        return []

    if df is None or df.empty:
        return []

    results = []
    for _, row in df.head(10).iterrows():
        results.append({
            "ts_code": ts_code,
            "report_date": str(row.get("日期", row.get("date", ""))),
            "institution": str(row.get("机构", row.get("institution", ""))),
            "rating": str(row.get("东财评级", row.get("rating", ""))),
            "title": str(row.get("报告名称", row.get("title", ""))),
            "eps_2026": _safe_float(row, "2026-盈利预测-收益"),
            "pe_2026": _safe_float(row, "2026-盈利预测-市盈率"),
            "eps_2027": _safe_float(row, "2027-盈利预测-收益"),
            "pe_2027": _safe_float(row, "2027-盈利预测-市盈率"),
            "eps_2028": _safe_float(row, "2028-盈利预测-收益"),
            "pe_2028": _safe_float(row, "2028-盈利预测-市盈率"),
            "report_url": str(row.get("报告PDF链接", row.get("report_url", ""))),
        })
    return results


def save_research_reports(db: sqlite3.Connection, reports: list[dict]) -> int:
    saved = 0
    for r in reports:
        try:
            db.execute("""
            INSERT OR REPLACE INTO research_reports
                (ts_code, report_date, institution, rating, title,
                 eps_2026, pe_2026, eps_2027, pe_2027, eps_2028, pe_2028,
                 report_url, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (r["ts_code"], r["report_date"], r["institution"], r["rating"],
                  r["title"], r["eps_2026"], r["pe_2026"], r["eps_2027"],
                  r["pe_2027"], r["eps_2028"], r["pe_2028"], r["report_url"]))
            saved += 1
        except Exception:
            pass
    return saved


# ═══════════════════════════════════════════════════════════════════════
# P1: 机构持仓
# ═══════════════════════════════════════════════════════════════════════

def ensure_institutional_holdings_table(db: sqlite3.Connection) -> None:
    db.execute("""
    CREATE TABLE IF NOT EXISTS institutional_holdings (
        ts_code VARCHAR(16) NOT NULL,
        as_of DATE NOT NULL,
        top_holders_json TEXT,
        north_bound_shares FLOAT,
        north_bound_pct FLOAT,
        north_bound_mv FLOAT,
        fund_count INTEGER,
        fund_total_shares FLOAT,
        fund_total_mv FLOAT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (ts_code, as_of)
    )
    """)


# 全局缓存：基金持仓全量数据（只拉一次）
_fund_hold_cache: dict[str, dict] | None = None


def _ensure_fund_hold_cache() -> dict[str, dict]:
    """拉取全市场基金持仓并缓存（仅调用一次 akshare，避免每只重复拉）。"""
    global _fund_hold_cache
    if _fund_hold_cache is not None:
        return _fund_hold_cache
    _fund_hold_cache = {}
    try:
        import akshare as ak
        df = ak.stock_report_fund_hold(symbol="基金持仓", date="20250331")
        if df is not None and not df.empty and "股票代码" in df.columns:
            for _, row in df.iterrows():
                code = str(row["股票代码"])
                _fund_hold_cache[code] = {
                    "fund_count": int(row.get("持有基金家数", 0) or 0),
                    "fund_total_shares": _safe_float(row, "持股总数"),
                    "fund_total_mv": _safe_float(row, "持股市值"),
                }
        logger.info("基金持仓缓存: %s 只股票", len(_fund_hold_cache))
    except Exception as e:
        logger.warning("基金持仓缓存失败: %s", e)
    return _fund_hold_cache


def fetch_institutional_holdings(ts_code: str, as_of: str = "2026-06-05", fast: bool = False) -> dict | None:
    try:
        import akshare as ak
    except ImportError:
        return None

    result: dict[str, Any] = {"ts_code": ts_code, "as_of": as_of}

    # 1) 十大股东
    try:
        df = ak.stock_main_stock_holder(stock=ts_code)
        if df is not None and not df.empty:
            holders = []
            for _, row in df.head(10).iterrows():
                holders.append({
                    "name": str(row.get("股东名称", row.get("holder_name", ""))),
                    "shares": _safe_float(row, "持股数"),
                    "pct": _safe_float(row, "持股比例"),
                    "change": str(row.get("变动", row.get("change", ""))),
                })
            result["top_holders_json"] = json.dumps(holders, ensure_ascii=False)
    except Exception as e:
        logger.debug("main_stock_holder(%s): %s", ts_code, e)

    # 2) 北向资金（fast 模式跳过，节省 ~13s/只）
    if not fast:
        try:
            df_nb = ak.stock_hsgt_individual_em(symbol=ts_code)
            if df_nb is not None and not df_nb.empty:
                latest = df_nb.iloc[-1]
                result["north_bound_shares"] = _safe_float(latest, "持股数")
                result["north_bound_pct"] = _safe_float(latest, "占A股%")
                result["north_bound_mv"] = _safe_float(latest, "持股市值")
        except Exception as e:
            logger.debug("hsgt(%s): %s", ts_code, e)

    # 3) 基金持仓（从全局缓存查，不再重复拉全量）
    try:
        fund_cache = _ensure_fund_hold_cache()
        if ts_code in fund_cache:
            f = fund_cache[ts_code]
            result["fund_count"] = f["fund_count"]
            result["fund_total_shares"] = f["fund_total_shares"]
            result["fund_total_mv"] = f["fund_total_mv"]
    except Exception as e:
        logger.debug("fund_hold(%s): %s", ts_code, e)

    return result if len(result) > 2 else None


def save_institutional_holdings(db: sqlite3.Connection, data: dict) -> bool:
    try:
        db.execute("""
        INSERT OR REPLACE INTO institutional_holdings
            (ts_code, as_of, top_holders_json, north_bound_shares,
             north_bound_pct, north_bound_mv, fund_count,
             fund_total_shares, fund_total_mv, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            data["ts_code"], data["as_of"],
            data.get("top_holders_json"),
            data.get("north_bound_shares"),
            data.get("north_bound_pct"),
            data.get("north_bound_mv"),
            data.get("fund_count"),
            data.get("fund_total_shares"),
            data.get("fund_total_mv"),
        ))
        return True
    except Exception as e:
        logger.error("入库 holdings %s 失败: %s", data.get("ts_code"), e)
        return False


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════

def _safe_float(row, key: str) -> float | None:
    """安全地从 Series/Row 取值转 float。"""
    candidates = [key]
    if key.strip():
        candidates.append(key.strip())

    for c in candidates:
        try:
            v = row[c]
            if v is None:
                continue
            v = float(v)
            if v != v:  # NaN check
                continue
            return v
        except (KeyError, ValueError, TypeError):
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════
# 一站式丰富
# ═══════════════════════════════════════════════════════════════════════

def enrich_single(ts_code: str, as_of: str = "2026-06-05", fast: bool = False,
                  include_products: bool = True) -> dict[str, bool]:
    """为单只股票拉取 P0+P1 数据。fast 模式跳过北向资金（节省 ~13s/只）。"""
    # 先做网络 I/O（无锁），再统一写 DB（短锁）
    profile = fetch_company_profile(ts_code)
    peers_raw = None  # needs DB read
    reports = fetch_research_reports(ts_code)
    holdings = fetch_institutional_holdings(ts_code, as_of, fast=fast)
    products = fetch_product_detail_zyjs(ts_code) if include_products else None

    # DB 操作带重试（并发 SQLite 偶尔锁）
    for attempt in range(3):
        try:
            db = _get_db()
            ensure_tables(db)

            status = {"profile": False, "peers": False, "reports": False, "holdings": False, "products": False}

            if profile:
                status["profile"] = save_company_profile(db, profile)

            peers_raw = compute_industry_peers(db, ts_code, as_of)
            if peers_raw:
                status["peers"] = save_industry_peers(db, peers_raw)

            if reports:
                saved = save_research_reports(db, reports)
                status["reports"] = saved > 0

            if holdings:
                status["holdings"] = save_institutional_holdings(db, holdings)

            if products:
                status["products"] = save_product_detail(db, products)

            db.close()
            return status
        except Exception as e:
            try: db.close()
            except: pass
            if "database is locked" in str(e).lower() and attempt < 2:
                time.sleep(0.1 * (attempt + 1))
            else:
                logger.error("enrich_single(%s) DB 失败: %s", ts_code, e)
                return {"profile": False, "peers": False, "reports": False, "holdings": False}


def ensure_tables(db: sqlite3.Connection) -> None:
    ensure_company_profile_table(db)
    ensure_industry_peers_table(db)
    ensure_research_reports_table(db)
    ensure_institutional_holdings_table(db)
    ensure_company_products_table(db)
    db.commit()


# ═══════════════════════════════════════════════════════════════════════
# 格式化为 LLM Prompt 片段
# ═══════════════════════════════════════════════════════════════════════

def build_enrich_prompt_section(ts_code: str, db: sqlite3.Connection | None = None) -> str:
    """从新表中读取数据，拼成可直接注入 deep research prompt 的文本。"""
    should_close = False
    if db is None:
        db = _get_db()
        should_close = True

    parts = []

    # Profile
    cur = db.execute("SELECT * FROM company_profiles WHERE ts_code = ?", (ts_code,))
    p = cur.fetchone()
    if p:
        lines = ["【公司档案】"]
        if p["full_name"]:
            lines.append(f"  全称: {p['full_name']}")
        if p["industry_csrc"]:
            lines.append(f"  行业(CSRC): {p['industry_csrc']}")
        if p["industry_sw"]:
            lines.append(f"  行业(申万): {p['industry_sw']}")
        if p["main_business"]:
            lines.append(f"  主营业务: {p['main_business'][:200]}")
        if p["business_scope"]:
            lines.append(f"  经营范围: {p['business_scope'][:200]}")
        if p["list_date"]:
            lines.append(f"  上市日期: {p['list_date']}")
        if p["website"]:
            lines.append(f"  官网: {p['website']}")
        if p["indices"]:
            lines.append(f"  入选指数: {p['indices']}")
        parts.append("\n".join(lines))

    # Peers
    cur = db.execute("SELECT * FROM industry_peers WHERE ts_code = ? ORDER BY as_of DESC LIMIT 1", (ts_code,))
    peer = cur.fetchone()
    if peer:
        lines = [f"【行业对比】行业: {peer['industry_name']}（共 {peer['peer_count']} 只）"]
        if peer["rank_by_revenue"] > 0:
            lines.append(f"  营收排名: {peer['rank_by_revenue']}/{peer['peer_count']}")
        if peer["rank_by_profit"] > 0:
            lines.append(f"  利润排名: {peer['rank_by_profit']}/{peer['peer_count']}")
        if peer["rank_by_market_cap"] > 0:
            lines.append(f"  市值排名: {peer['rank_by_market_cap']}/{peer['peer_count']}")
        if peer["industry_med_pe"] is not None:
            lines.append(f"  行业中位数PE: {peer['industry_med_pe']:.1f}")
        if peer["industry_med_pb"] is not None:
            lines.append(f"  行业中位数PB: {peer['industry_med_pb']:.2f}")
        if peer["industry_med_roe"] is not None:
            lines.append(f"  行业中位数ROE: {peer['industry_med_roe']:.1f}%")
        parts.append("\n".join(lines))

    # Reports
    cur = db.execute("""
        SELECT * FROM research_reports WHERE ts_code = ?
        ORDER BY report_date DESC LIMIT 5
    """, (ts_code,))
    reports = cur.fetchall()
    if reports:
        lines = ["【机构研报摘要（最新5份）】"]
        for r in reports:
            rating = r["rating"] or "-"
            inst = r["institution"] or "?"
            title = (r["title"] or "")[:80]
            eps_info = ""
            if r["eps_2026"]:
                eps_info += f" 2026E EPS={r['eps_2026']:.2f}"
            if r["eps_2027"]:
                eps_info += f" 2027E EPS={r['eps_2027']:.2f}"
            lines.append(f"  [{rating}] {inst}: {title}{eps_info}")
        parts.append("\n".join(lines))

    # Holdings
    cur = db.execute("""
        SELECT * FROM institutional_holdings WHERE ts_code = ?
        ORDER BY as_of DESC LIMIT 1
    """, (ts_code,))
    h = cur.fetchone()
    if h:
        lines = ["【机构持仓】"]
        if h["fund_count"] is not None:
            lines.append(f"  基金持仓: {h['fund_count']} 只基金")
        if h["north_bound_pct"] is not None:
            lines.append(f"  北向持股: {h['north_bound_pct']:.1f}%")
        if h["north_bound_mv"] is not None:
            lines.append(f"  北向市值: {h['north_bound_mv']/1e8:.1f} 亿")
        if h["top_holders_json"]:
            try:
                holders = json.loads(h["top_holders_json"])
                top3 = holders[:3]
                top3_str = '、'.join(f'{x["name"]}({x.get("pct","?")}%)' for x in top3)
                lines.append(f"  前三大股东: {top3_str}")
            except Exception:
                pass
        parts.append("\n".join(lines))

    # ── P2: 产品深度数据 ──
    try:
        product_section = build_product_prompt_section(ts_code, db)
        if product_section:
            parts.append(product_section)
    except Exception:
        pass

    if should_close:
        db.close()

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# P2: 产品深度数据 — stock_zyjs_ths 结构化产品列表 + LLM 深度调研
# ═══════════════════════════════════════════════════════════════════════

def ensure_company_products_table(db: sqlite3.Connection) -> None:
    """产品深度数据表：产品列表 + LLM 竞争力分析。"""
    db.execute("""
    CREATE TABLE IF NOT EXISTS company_products (
        ts_code VARCHAR(16) PRIMARY KEY,
        -- 来自 stock_zyjs_ths（免费）
        main_business_categories TEXT,
        product_types TEXT,
        product_names TEXT,
        business_scope_zyjs TEXT,
        zyjs_fetched_at DATETIME,
        -- 来自 LLM 深度调研
        competitive_positioning TEXT,
        technology_moat TEXT,
        key_products_json TEXT,
        revenue_drivers_json TEXT,
        key_customers_json TEXT,
        key_competitors_json TEXT,
        supply_chain_position TEXT,
        growth_catalysts TEXT,
        risk_factors TEXT,
        llm_confidence_score FLOAT,
        llm_sources_json TEXT,
        llm_researched_at DATETIME,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    db.execute("""
    CREATE INDEX IF NOT EXISTS ix_company_products_updated
    ON company_products (updated_at)
    """)


def fetch_product_detail_zyjs(ts_code: str) -> dict[str, Any] | None:
    """从 AkShare stock_zyjs_ths 拉取产品级数据（免费，同花顺主营构成）。

    返回字段：主营业务, 产品类型, 产品名称, 经营范围
    """
    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare 未安装")
        return None

    try:
        df = ak.stock_zyjs_ths(symbol=ts_code)
    except Exception as e:
        logger.debug("stock_zyjs_ths(%s) 失败: %s", ts_code, e)
        return None

    if df is None or df.empty:
        return None

    try:
        row = df.iloc[0].to_dict()
    except Exception:
        return None

    col_map = {
        "main_business_categories": ["主营业务"],
        "product_types": ["产品类型"],
        "product_names": ["产品名称"],
        "business_scope_zyjs": ["经营范围"],
    }

    result: dict[str, Any] = {"ts_code": ts_code}
    for key, candidates in col_map.items():
        for c in candidates:
            v = row.get(c)
            if v is not None and str(v) not in ("nan", "None", ""):
                result[key] = str(v)
                break

    if "main_business_categories" not in result and "product_names" not in result:
        return None

    result["zyjs_fetched_at"] = dt_date.today().isoformat()
    return result


def save_product_detail(db: sqlite3.Connection, data: dict[str, Any]) -> bool:
    """入库产品数据（UPSERT）。"""
    try:
        db.execute("""
        INSERT OR REPLACE INTO company_products
            (ts_code, main_business_categories, product_types, product_names,
             business_scope_zyjs, zyjs_fetched_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            data["ts_code"],
            data.get("main_business_categories"),
            data.get("product_types"),
            data.get("product_names"),
            data.get("business_scope_zyjs"),
            data.get("zyjs_fetched_at"),
        ))
        return True
    except Exception as e:
        logger.error("入库 products %s 失败: %s", data.get("ts_code"), e)
        return False


def _load_company_products(db: sqlite3.Connection, ts_code: str) -> dict[str, Any] | None:
    """从 company_products 读取已入库的产品数据（含 LLM 调研结果）。"""
    cur = db.execute(
        "SELECT * FROM company_products WHERE ts_code = ?", (ts_code,)
    )
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    # 反序列化 JSON 字段
    for json_key in ("key_products_json", "revenue_drivers_json",
                     "key_customers_json", "key_competitors_json",
                     "llm_sources_json"):
        raw = out.get(json_key)
        if isinstance(raw, str) and raw:
            try:
                out[json_key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return out


def build_product_prompt_section(ts_code: str, db: sqlite3.Connection | None = None) -> str:
    """从 company_products 读取产品数据，拼成 LLM prompt 注入文本。

    优先使用 LLM 调研结果，回退到 zyjs 结构化数据。
    """
    should_close = False
    if db is None:
        db = _get_db()
        should_close = True

    try:
        data = _load_company_products(db, ts_code)
    except Exception:
        if should_close:
            db.close()
        return ""

    if not data:
        if should_close:
            db.close()
        return ""

    parts = []

    # ── 基础产品数据（zyjs）──
    if data.get("main_business_categories"):
        parts.append(f"【产品线/业务板块】\n  {data['main_business_categories']}")
    if data.get("product_types"):
        parts.append(f"【产品分类】\n  {data['product_types']}")
    if data.get("product_names"):
        products = data["product_names"]
        # 产品名可能很长，截断
        if len(products) > 600:
            products = products[:600] + "…"
        parts.append(f"【具体产品】\n  {products}")

    # ── LLM 深度调研结果 ──
    llm_sections = []
    if data.get("competitive_positioning"):
        llm_sections.append(f"【竞争定位】\n  {data['competitive_positioning']}")
    if data.get("technology_moat"):
        llm_sections.append(f"【技术壁垒/护城河】\n  {data['technology_moat']}")
    if data.get("key_products_json"):
        kp = data["key_products_json"]
        if isinstance(kp, list):
            items = []
            for p in kp[:5]:
                if isinstance(p, dict):
                    items.append(f"  • {p.get('name', '?')} — 营收占比 {p.get('revenue_pct', '?')}%, "
                               f"毛利率 {p.get('gross_margin', '?')}%, "
                               f"地位: {p.get('market_position', '?')}")
            if items:
                llm_sections.append("【核心产品详情】\n" + "\n".join(items))
    if data.get("revenue_drivers_json"):
        rd = data["revenue_drivers_json"]
        if isinstance(rd, list):
            drivers = "\n".join(f"  • {d}" for d in rd[:5] if isinstance(d, str))
            if drivers:
                llm_sections.append(f"【营收驱动因素】\n{drivers}")
    if data.get("key_competitors_json"):
        kc = data["key_competitors_json"]
        if isinstance(kc, list):
            comps = []
            for c in kc[:5]:
                if isinstance(c, dict):
                    comps.append(f"  • {c.get('name', '?')} ({c.get('code', '?')}) — {c.get('comparison', '?')}")
            if comps:
                llm_sections.append("【主要竞争对手】\n" + "\n".join(comps))
    if data.get("growth_catalysts"):
        llm_sections.append(f"【增长催化剂】\n  {data['growth_catalysts']}")
    if data.get("risk_factors"):
        llm_sections.append(f"【产品风险因素】\n  {data['risk_factors']}")
    if data.get("supply_chain_position"):
        llm_sections.append(f"【供应链位置】\n  {data['supply_chain_position']}")

    if llm_sections:
        parts.append("【深度产品调研（LLM）】")
        parts.extend(llm_sections)
        if data.get("llm_confidence_score"):
            parts.append(f"  [置信度: {data['llm_confidence_score']:.0%}]")

    if should_close:
        db.close()

    return "\n\n".join(parts) if parts else ""


def research_products_with_llm(
    ts_code: str,
    *,
    db: sqlite3.Connection | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """用 LLM 对单只股票做产品竞争力深度调研。

    以 stock_zyjs_ths 的产品列表为 grounding，让 LLM 基于训练知识
    分析该公司的产品竞争力、技术壁垒、市场地位等。

    Args:
        ts_code: 股票代码
        db: 数据库连接（可选）
        force: 强制重新调研（忽略已有结果）

    Returns:
        调研结果 dict，写入 company_products 表
    """
    should_close = False
    if db is None:
        db = _get_db()
        should_close = True

    try:
        # 检查是否已有 LLM 调研结果
        if not force:
            existing = _load_company_products(db, ts_code)
            if existing and existing.get("llm_researched_at"):
                logger.debug("产品调研已缓存: %s", ts_code)
                return existing

        # 必须有 zyjs 产品数据作为 grounding
        data = _load_company_products(db, ts_code)
        if not data:
            # 尝试拉取 zyjs
            zyjs = fetch_product_detail_zyjs(ts_code)
            if zyjs:
                save_product_detail(db, zyjs)
                data = _load_company_products(db, ts_code)
            if not data:
                logger.debug("无 zyjs 产品数据，跳过 LLM 调研: %s", ts_code)
                return None

        # ── 从 company_profiles 获取公司名称和行业 ──
        cur = db.execute(
            "SELECT full_name, industry_csrc, industry_sw, main_business "
            "FROM company_profiles WHERE ts_code = ?", (ts_code,)
        )
        profile = cur.fetchone()

        name = profile["full_name"] if profile else ts_code
        industry = (profile["industry_csrc"] or profile["industry_sw"] or "未知") if profile else "未知"
        main_biz = profile["main_business"] if profile else ""

        # ── 构建 LLM prompt ──
        products_raw = data.get("product_names") or ""
        categories = data.get("main_business_categories") or ""
        product_types = data.get("product_types") or ""

        prompt = f"""你是一位资深的行业研究员，正在对一家 A 股上市公司进行产品层面的深度分析。

【公司信息】
- 名称：{name}
- 代码：{ts_code}
- 行业：{industry}
- 主营业务概要：{main_biz[:200] if main_biz else '无'}

【已核验的产品数据（来源：同花顺主营构成）】
- 业务板块：{categories}
- 产品分类：{product_types}
- 具体产品：{products_raw[:800] if products_raw else '无'}

请基于以上产品数据，结合你的行业知识，提供以下分析。注意：
1. 必须严格基于产品列表，不得编造不存在于列表中的产品
2. 对于不确定的信息，标注"推断"并给出置信度
3. 竞争对手必须是真实存在的公司

返回 JSON 对象：

{{
  "competitive_positioning": "一句话概述公司在行业中的竞争地位（≤80字）",
  "technology_moat": "技术壁垒与护城河分析（≤150字），包括：专利/技术门槛、品牌溢价、客户转换成本、规模优势",
  "key_products": [
    {{
      "name": "核心产品名",
      "revenue_pct": "营收占比估计（如 '~25%'）",
      "gross_margin": "毛利率区间估计（如 '30-40%'）",
      "market_position": "市场地位（如 '国内前三'、'全球龙头'、'国产替代先锋'）",
      "growth_stage": "生命周期阶段（导入期/成长期/成熟期/衰退期）"
    }}
  ],
  "revenue_drivers": ["核心增长驱动因素，每条 ≤30字，3-5条"],
  "key_customers": [{{"name": "主要客户/下游行业", "concentration": "集中度（高/中/低）"}}],
  "key_competitors": [
    {{
      "name": "竞争对手名",
      "code": "股票代码（如知道）",
      "comparison": "与 {name} 的对比（≤40字）"
    }}
  ],
  "supply_chain_position": "在产业链中的位置（上游/中游/下游/平台型）及议价能力简述（≤100字）",
  "growth_catalysts": "未来 1-2 年的产品层面增长催化剂（≤100字）",
  "risk_factors": "产品层面风险因素（技术替代、客户集中、原材料依赖等，≤100字）",
  "confidence_score": 0.0-1.0（对该分析的总体置信度）
}}

输出合法 JSON，字段不可缺失。"""

        # ── 调用 LLM ──
        try:
            from zplan_shared.llm.gemini import generate_json, llm_available, pop_usage
        except ImportError:
            logger.warning("LLM 模块不可用")
            if should_close:
                db.close()
            return None

        if not llm_available():
            logger.warning("LLM 未配置，跳过产品调研: %s", ts_code)
            if should_close:
                db.close()
            return None

        try:
            result = generate_json(
                prompt=prompt,
                response_schema={
                    "type": "object",
                    "properties": {
                        "competitive_positioning": {"type": "string"},
                        "technology_moat": {"type": "string"},
                        "key_products": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "revenue_pct": {"type": "string"},
                                    "gross_margin": {"type": "string"},
                                    "market_position": {"type": "string"},
                                    "growth_stage": {"type": "string"},
                                },
                                "required": ["name", "market_position"],
                            },
                        },
                        "revenue_drivers": {"type": "array", "items": {"type": "string"}},
                        "key_customers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "concentration": {"type": "string"},
                                },
                            },
                        },
                        "key_competitors": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "code": {"type": "string"},
                                    "comparison": {"type": "string"},
                                },
                            },
                        },
                        "supply_chain_position": {"type": "string"},
                        "growth_catalysts": {"type": "string"},
                        "risk_factors": {"type": "string"},
                        "confidence_score": {"type": "number"},
                    },
                    "required": [
                        "competitive_positioning", "technology_moat", "key_products",
                        "revenue_drivers", "key_competitors",
                        "supply_chain_position", "growth_catalysts",
                        "risk_factors", "confidence_score",
                    ],
                },
                temperature=0.3,
                max_output_tokens=4096,
            )
            usage = pop_usage(result)
            if usage:
                logger.info("LLM 产品调研 %s: prompt=%s, output=%s tokens",
                           ts_code, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        except Exception as e:
            logger.warning("LLM 产品调研 %s 失败: %s", ts_code, e)
            if should_close:
                db.close()
            return None

        # ── 写入 company_products ──
        now = dt_date.today().isoformat()
        try:
            db.execute("""
            UPDATE company_products SET
                competitive_positioning = ?,
                technology_moat = ?,
                key_products_json = ?,
                revenue_drivers_json = ?,
                key_customers_json = ?,
                key_competitors_json = ?,
                supply_chain_position = ?,
                growth_catalysts = ?,
                risk_factors = ?,
                llm_confidence_score = ?,
                llm_sources_json = ?,
                llm_researched_at = ?,
                updated_at = datetime('now')
            WHERE ts_code = ?
            """, (
                result.get("competitive_positioning"),
                result.get("technology_moat"),
                json.dumps(result.get("key_products") or [], ensure_ascii=False),
                json.dumps(result.get("revenue_drivers") or [], ensure_ascii=False),
                json.dumps(result.get("key_customers") or [], ensure_ascii=False),
                json.dumps(result.get("key_competitors") or [], ensure_ascii=False),
                result.get("supply_chain_position"),
                result.get("growth_catalysts"),
                result.get("risk_factors"),
                result.get("confidence_score"),
                json.dumps({
                    "model": "deepseek-v4-pro",
                    "usage": usage,
                    "ts_code": ts_code,
                }, ensure_ascii=False, default=str),
                now,
                ts_code,
            ))
            db.commit()
        except Exception as e:
            logger.error("写入 LLM 产品调研 %s 失败: %s", ts_code, e)

        if should_close:
            db.close()
        return _load_company_products(db if not should_close else _get_db(), ts_code)

    except Exception as e:
        logger.error("research_products_with_llm(%s) 异常: %s", ts_code, e)
        if should_close:
            try:
                db.close()
            except Exception:
                pass
        return None


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("用法: --symbol 300124 | --batch [--fast] [--all] | --products [--top N] [--llm]")
        sys.exit(1)

    db = _get_db()
    ensure_tables(db)

    if "--symbol" in sys.argv:
        idx = sys.argv.index("--symbol")
        ts_code = sys.argv[idx + 1]
        logger.info("丰富单只: %s", ts_code)
        status = enrich_single(ts_code, include_products=True)
        logger.info("结果: %s", status)
        # 打印 prompt 片段
        section = build_enrich_prompt_section(ts_code, db)
        print(section)

    elif "--products" in sys.argv:
        # ── 批量拉取产品数据（zyjs） + 可选的 LLM 深度调研 ──
        do_llm = "--llm" in sys.argv
        top_n = None
        if "--top" in sys.argv:
            try:
                top_n = int(sys.argv[sys.argv.index("--top") + 1])
            except (IndexError, ValueError):
                pass

        # 查询待处理的股票
        if top_n:
            # --top N: 优先高分关注+观望
            cur = db.execute("""
            SELECT DISTINCT s.ts_code FROM all_market_llm_scores s
            WHERE s.as_of = (SELECT MAX(as_of) FROM all_market_llm_scores)
              AND s.recommendation IN ('关注', '观望')
              AND s.ts_code IN (SELECT ts_code FROM company_profiles)
              AND s.ts_code NOT IN (SELECT ts_code FROM company_products WHERE product_names IS NOT NULL)
            ORDER BY s.adjusted_score DESC
            LIMIT ?
            """, (top_n,))
        else:
            # 默认：全量（所有有公司档案的股票）
            cur = db.execute("""
            SELECT ts_code FROM company_profiles
            WHERE ts_code NOT IN (SELECT ts_code FROM company_products WHERE product_names IS NOT NULL)
            ORDER BY ts_code
            """)
        codes = [r["ts_code"] for r in cur.fetchall()]

        if not codes:
            logger.info("所有关注股票已有产品数据")
            db.close()
            return

        workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
        logger.info("产品数据批量拉取 %s 只, %s 线程%s",
                    len(codes), workers, " + LLM 深度调研" if do_llm else "")

        success_zyjs = 0
        success_llm = 0
        done = 0

        def _worker_products(ts):
            try:
                # Step 1: 拉取 zyjs 产品数据
                zyjs = fetch_product_detail_zyjs(ts)
                if zyjs:
                    # 每个 worker 独立开 DB 连接（线程安全）
                    wdb = _get_db()
                    try:
                        save_product_detail(wdb, zyjs)
                    finally:
                        wdb.close()
                # Step 2: 可选 LLM 深度调研
                llm_ok = False
                if do_llm and zyjs:
                    try:
                        result = research_products_with_llm(ts)
                        llm_ok = result is not None and result.get("llm_researched_at")
                    except Exception:
                        pass
                return {"zyjs": bool(zyjs), "llm": llm_ok}
            except Exception as e:
                logger.error("%s: %s", ts, e)
                return {"zyjs": False, "llm": False}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker_products, ts): ts for ts in codes}
            for fut in as_completed(futures):
                done += 1
                try:
                    st = fut.result()
                    if st.get("zyjs"):
                        success_zyjs += 1
                    if st.get("llm"):
                        success_llm += 1
                except Exception as e:
                    logger.error("线程异常: %s", e)

                if done % 50 == 0:
                    logger.info("进度 %s/%s: zyjs=%s llm=%s",
                                done, len(codes), success_zyjs, success_llm)

        logger.info("产品数据完成: zyjs=%s/%s, llm=%s/%s",
                    success_zyjs, len(codes), success_llm, len(codes))

    elif "--batch" in sys.argv:
        fast = "--fast" in sys.argv
        if "--all" in sys.argv:
            label = "全部剩余"
            cur = db.execute("""
            SELECT DISTINCT s.ts_code FROM all_market_llm_scores s
            WHERE s.as_of = '2026-06-05'
              AND s.ts_code NOT IN (SELECT ts_code FROM company_profiles)
            ORDER BY s.adjusted_score DESC
            """)
        else:
            label = "P0: 有概念+LLM关注"
            cur = db.execute("""
            SELECT DISTINCT s.ts_code FROM all_market_llm_scores s
            JOIN stock_concept_members c ON s.ts_code = c.ts_code
            WHERE s.as_of = '2026-06-05' AND s.recommendation = '关注'
              AND s.ts_code NOT IN (SELECT ts_code FROM company_profiles)
            ORDER BY s.adjusted_score DESC
            """)
        codes = [r["ts_code"] for r in cur.fetchall()]

        if not codes:
            logger.info("无待处理股票（可能已全部丰富）")
            db.close()
            return

        workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
        logger.info("批量丰富 [%s] %s 只 %s, %s 线程并行",
                    label, len(codes), "(fast 模式，跳过北向)" if fast else "", workers)

        # 预热基金缓存（线程安全，只需一次）
        _ensure_fund_hold_cache()

        success = {"profile": 0, "peers": 0, "reports": 0, "holdings": 0, "products": 0}
        done = 0

        def _worker_one(ts):
            try:
                return enrich_single(ts, fast=fast, include_products=True)
            except Exception as e:
                logger.error("%s: %s", ts, e)
                return {"profile": False, "peers": False, "reports": False, "holdings": False, "products": False}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker_one, ts): ts for ts in codes}
            for fut in as_completed(futures):
                done += 1
                try:
                    st = fut.result()
                    for k in success:
                        if st.get(k):
                            success[k] += 1
                except Exception as e:
                    logger.error("线程异常: %s", e)

                if done % 100 == 0:
                    logger.info("进度 %s/%s (%d%%): %s", done, len(codes),
                                int(done / len(codes) * 100), success)

        logger.info("完成: %s", success)

    db.close()


if __name__ == "__main__":
    main()
