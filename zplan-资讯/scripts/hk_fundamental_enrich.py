"""P4+P5: 港股基本面丰富（估值 + 财务 + 公司档案）。

数据源：
  - stock_hk_financial_indicator_em → PE/PB/ROE/EPS/营收/利润
  - stock_hk_company_profile_em → 行业/主营业务

结果写入已有表（仅新增 market='hk' 行，不影响 A 股数据）。

用法：
  .venv/bin/python3 hk_fundamental_enrich.py          # 预览
  .venv/bin/python3 hk_fundamental_enrich.py --apply  # 执行
"""
import logging, sqlite3, sys, time
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB = Path("/Users/richard/my_stock_ai/zplan-资讯/zplan.db")
AS_OF = "2026-06-05"

def _db():
    db = sqlite3.connect(str(DB), isolation_level=None)
    db.row_factory = sqlite3.Row
    return db

def _safe_f(v):
    try: return None if v is None else float(v)
    except: return None

def fetch_one(ts_code: str):
    """拉取港股单只财务+估值+档案。"""
    import akshare as ak
    result = {}
    code_clean = str(ts_code).replace(".HK","").replace(".hk","").strip()
    # API 接受带前导零的 5 位代码，保留原样

    # 1) 财务指标
    try:
        df = ak.stock_hk_financial_indicator_em(symbol=code_clean)
        if df is not None and not df.empty:
            row = df.iloc[0]
            result["pe"] = _safe_f(row.get("市盈率"))
            result["pb"] = _safe_f(row.get("市净率"))
            result["roe"] = _safe_f(row.get("股东权益回报率(%)"))
            result["eps"] = _safe_f(row.get("基本每股收益(元)"))
            result["revenue"] = _safe_f(row.get("营业总收入"))
            result["net_profit"] = _safe_f(row.get("净利润"))
            result["total_mv"] = _safe_f(row.get("总市值(港元)"))
            result["dividend_yield"] = _safe_f(row.get("股息率TTM(%)"))
            result["net_margin"] = _safe_f(row.get("销售净利率(%)"))
            result["bvps"] = _safe_f(row.get("每股净资产(元)"))
    except Exception as e:
        logger.debug("%s financial: %s", ts_code, e)

    # 2) 公司档案
    try:
        df2 = ak.stock_hk_company_profile_em(symbol=code_clean)
        if df2 is not None and not df2.empty:
            row2 = df2.iloc[0]
            result["name_cn"] = str(row2.get("公司名称", ""))[:128]
            result["industry"] = str(row2.get("所属行业", ""))[:128]
            result["main_business"] = str(row2.get("主营业务", ""))[:512]
    except Exception as e:
        logger.debug("%s profile: %s", ts_code, e)

    return result


def main():
    apply = "--apply" in sys.argv
    db = _db()

    # 统计
    cur = db.execute("""
    SELECT COUNT(DISTINCT dp.ts_code) as total,
           SUM(CASE WHEN sn.ts_code IS NOT NULL THEN 1 ELSE 0 END) as has_snap,
           SUM(CASE WHEN fi.ts_code IS NOT NULL THEN 1 ELSE 0 END) as has_fin
    FROM daily_prices dp
    LEFT JOIN daily_snapshot sn ON dp.ts_code = sn.ts_code AND sn.market = 'hk'
    LEFT JOIN financial_indicators fi ON dp.ts_code = fi.ts_code AND fi.market = 'hk'
    WHERE dp.market = 'hk' AND dp.trade_date = ?
    """, (AS_OF,))
    r = cur.fetchone()
    logger.info("港股现状: total=%s, 有估值=%s, 有财务=%s", r['total'], r['has_snap'], r['has_fin'])

    # 预览
    codes = db.execute("""
    SELECT ts_code, name FROM stock_list WHERE market = 'hk'
    ORDER BY ts_code LIMIT 5
    """).fetchall()

    for c in codes:
        data = fetch_one(c['ts_code'])
        print(f"\n{c['ts_code']} {c['name']}: PE={data.get('pe')} PB={data.get('pb')} "
              f"ROE={data.get('roe')} 行业={data.get('industry','?')}")

    if not apply:
        print(f"\n预估: {r['total']} 只港股需要丰富，约 {r['total']*3/60:.0f} 分钟")
        print("加 --apply 执行")
        db.close()
        return

    # 批量执行
    all_codes = db.execute("""
    SELECT ts_code FROM stock_list WHERE market = 'hk' ORDER BY ts_code
    """).fetchall()

    snap_saved = fin_saved = 0
    for i, c in enumerate(all_codes, 1):
        ts = c['ts_code']
        data = fetch_one(ts)
        if not data:
            continue

        # 写入 daily_snapshot
        if data.get("pe") or data.get("pb") or data.get("total_mv"):
            try:
                db.execute("""
                INSERT OR REPLACE INTO daily_snapshot
                    (ts_code, trade_date, pe_ttm, pb, total_mv, market, source, ingested_at)
                VALUES (?, ?, ?, ?, ?, 'hk', 'akshare_hk', datetime('now'))
                """, (ts, AS_OF, data.get("pe"), data.get("pb"), data.get("total_mv")))
                snap_saved += 1
            except: pass

        # 写入 financial_indicators
        if data.get("net_profit") or data.get("revenue"):
            try:
                db.execute("""
                INSERT OR REPLACE INTO financial_indicators
                    (ts_code, report_date, pe_ttm, pb, revenue, net_profit, roe, market, source, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'hk', 'akshare_hk', datetime('now'))
                """, (ts, "2026-03-31", data.get("pe"), data.get("pb"),
                      data.get("revenue"), data.get("net_profit"), data.get("roe")))
                fin_saved += 1
            except: pass

        # 写入 company_profiles（港股版）
        if data.get("name_cn") or data.get("industry"):
            try:
                db.execute("""
                INSERT OR REPLACE INTO company_profiles
                    (ts_code, full_name, industry_csrc, main_business, fetched_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """, (ts, data.get("name_cn"), data.get("industry"), data.get("main_business")))
            except: pass

        if i % 100 == 0:
            logger.info("进度 %s/%s: snap=%s fin=%s", i, len(all_codes), snap_saved, fin_saved)
        time.sleep(0.3)

    logger.info("完成: snap=%s fin=%s", snap_saved, fin_saved)
    db.close()


if __name__ == '__main__':
    main()
