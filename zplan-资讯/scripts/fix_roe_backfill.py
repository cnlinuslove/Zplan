"""P1: 回填 financial_indicators.roe（从 snapshot 估值 + 利润计算）。

净资产 ≈ 总市值 / PB；ROE ≈ 净利润 / 净资产 × 100

用法：
  .venv/bin/python3 fix_roe_backfill.py          # 先预览
  .venv/bin/python3 fix_roe_backfill.py --apply   # 执行
"""
import sqlite3
import sys

DB = "/Users/richard/my_stock_ai/zplan-资讯/zplan.db"
REPORT_DATE = "2026-03-31"
SNAP_DATE = "2026-06-05"

def main():
    apply = "--apply" in sys.argv
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    # 计数
    cur = db.execute("""
    SELECT COUNT(*) as total,
           SUM(CASE WHEN net_profit > 0 AND roe IS NULL THEN 1 ELSE 0 END) as 待补,
           SUM(CASE WHEN roe IS NOT NULL THEN 1 ELSE 0 END) as 已有
    FROM financial_indicators WHERE report_date = ?
    """, (REPORT_DATE,))
    r = cur.fetchone()
    print(f"报告期 {REPORT_DATE}: total={r['total']}, 已有ROE={r['已有']}, 待补={r['待补']}")

    # 预览
    cur2 = db.execute("""
    SELECT fi.ts_code, sl.name,
           ROUND(fi.net_profit/1e8, 2) as 利润亿,
           ROUND(sn.total_mv/1e8, 0) as 市值亿,
           ROUND(sn.pb, 2) as PB,
           ROUND(fi.net_profit / (sn.total_mv / sn.pb) * 100, 2) as 计算ROE
    FROM financial_indicators fi
    JOIN daily_snapshot sn ON fi.ts_code = sn.ts_code AND sn.trade_date = ?
    JOIN stock_list sl ON fi.ts_code = sl.ts_code
    WHERE fi.report_date = ?
      AND fi.net_profit > 0 AND fi.roe IS NULL
      AND sn.pb > 0 AND sn.total_mv > 0
    ORDER BY 计算ROE DESC
    LIMIT 20
    """, (SNAP_DATE, REPORT_DATE))

    print("\n预览（前20）:")
    print(f"{'代码':<10} {'名称':<10} {'利润亿':>8} {'市值亿':>8} {'PB':>6} {'ROE%':>8}")
    for row in cur2.fetchall():
        print(f"{row['ts_code']:<10} {row['name']:<10} {row['利润亿']:>8.2f} {row['市值亿']:>8.0f} {row['PB']:>6.2f} {row['计算ROE']:>8.1f}")

    if not apply:
        print("\n加 --apply 执行回填")
        db.close()
        return

    # 执行
    count = db.execute("""
    UPDATE financial_indicators SET roe = ROUND(
        net_profit / (
            SELECT total_mv / pb FROM daily_snapshot
            WHERE ts_code = financial_indicators.ts_code
              AND trade_date = ?
              AND pb > 0 AND total_mv > 0
        ) * 100, 2
    )
    WHERE report_date = ?
      AND roe IS NULL
      AND net_profit > 0
    """, (SNAP_DATE, REPORT_DATE)).rowcount

    db.commit()
    print(f"\n已回填 {count} 只 ROE")

    # 验证
    cur3 = db.execute("SELECT COUNT(*) as cnt FROM financial_indicators WHERE report_date=? AND roe IS NOT NULL", (REPORT_DATE,))
    print(f"Q1 2026 有 ROE: {cur3.fetchone()['cnt']} / {r['total']}")

    db.close()

if __name__ == "__main__":
    main()
