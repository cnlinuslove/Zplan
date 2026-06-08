"""P7: 生成形态事件 + 激活新闻管道。

P7a: 从 daily_features 提取关键技术形态 → pattern_events
P7b: 运行 sentiment ETL → 财讯快报 / 北向资金 / 市场情绪

用法：
  .venv/bin/python3 run_p7_patterns.py --patterns    # 仅形态
  .venv/bin/python3 run_p7_patterns.py --news        # 仅新闻
  .venv/bin/python3 run_p7_patterns.py --all         # 全部
"""
import json, logging, sqlite3, sys, time
from datetime import date, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB = Path("/Users/richard/my_stock_ai/zplan-资讯/zplan.db")
AS_OF = "2026-06-05"


def _db():
    db = sqlite3.connect(str(DB), isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


# ═══════════════════════════════════════════
# P7a: 形态事件生成
# ═══════════════════════════════════════════

PATTERN_DEFS = [
    # (event_type, label, condition_sql, horizon_days)
    ("BO", "ma5_cross_ma20", "ma5_cross_ma20 = 1", 5),
    ("BO", "macd_cross_up", "macd_cross_up = 1", 10),
    ("BO", "vol_breakout", "vol_breakout = 1", 5),
    ("BO", "kdj_golden_cross", "kdj_golden_cross = 1", 5),
    ("BD", "kdj_death_cross", "kdj_death_cross = 1", 5),
    ("BD", "ret20_overbought", "ret_20d > 0.15", 5),
    ("BD", "ret5_oversold", "ret_5d < -0.10", 5),
]

def generate_pattern_events():
    db = _db()

    # 获取最近有特征的股票
    cur = db.execute("""
    SELECT df.ts_code, sl.name, df.trade_date, df.close,
           df.pct_chg, df.ret_5d, df.ret_20d, df.ret_60d, df.vol_ratio20,
           df.ma5_cross_ma20, df.macd_cross_up, df.vol_breakout,
           df.kdj_golden_cross, df.kdj_death_cross, df.atr_pct
    FROM daily_features df
    JOIN stock_list sl ON df.ts_code = sl.ts_code
    WHERE df.trade_date = ? AND df.market = 'a' AND sl.market = 'a'
    """, (AS_OF,))

    rows = cur.fetchall()
    logger.info("扫描 %s 只股票形态...", len(rows))

    saved = 0
    for r in rows:
        ts = r['ts_code']
        close = r['close']
        atr = r['atr_pct'] or 0.02
        vol20 = r['vol_ratio20'] or 1.0
        ret20 = r['ret_20d'] or 0

        for event_type, label, condition, horizon in PATTERN_DEFS:
            # 检查条件
            match = False
            if condition == "ma5_cross_ma20 = 1":
                match = r['ma5_cross_ma20'] == 1
            elif condition == "macd_cross_up = 1":
                match = r['macd_cross_up'] == 1
            elif condition == "vol_breakout = 1":
                match = r['vol_breakout'] == 1
            elif condition == "kdj_golden_cross = 1":
                match = r['kdj_golden_cross'] == 1
            elif condition == "kdj_death_cross = 1":
                match = r['kdj_death_cross'] == 1
            elif condition == "ret20_overbought":
                match = ret20 > 0.15
            elif condition == "ret5_oversold":
                match = (r['ret_5d'] or 0) < -0.10

            if not match:
                continue

            # label_confidence: 组合信号加分
            confidence = 0.5
            if event_type == "BO" and vol20 > 1.5:
                confidence = 0.75
            if event_type == "BO" and ret20 > 0.05:
                confidence = 0.6  # 追高降分

            # runup_pct: 近5日涨幅
            runup = r['pct_chg'] or 0

            # forward_return: 留空，回测时填充
            forward_return = None

            extra = json.dumps({
                "close": close, "atr_pct": atr, "vol_ratio20": vol20,
                "ret_20d": ret20, "pct_chg": r['pct_chg']
            }, ensure_ascii=False)

            try:
                db.execute("""
                INSERT OR REPLACE INTO pattern_events
                    (ts_code, event_date, event_type, formation_start,
                     horizon_days, label, label_confidence,
                     runup_pct, forward_return, close_at_event, atr_pct,
                     horizon_end_date, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (
                    ts, AS_OF, event_type, AS_OF, horizon,
                    label, round(confidence, 2),
                    round(runup, 4), forward_return, close, round(atr, 4),
                    AS_OF, extra,
                ))
                saved += 1
            except Exception:
                pass

    db.commit()
    logger.info("形态事件: %s 条 (pattern_events)", saved)

    # 统计
    cur2 = db.execute("""
    SELECT label, COUNT(*) as cnt FROM pattern_events
    WHERE event_date = ? GROUP BY label ORDER BY cnt DESC
    """, (AS_OF,))
    for row in cur2.fetchall():
        logger.info("  %s: %s", row['label'], row['cnt'])

    db.close()


# ═══════════════════════════════════════════
# P7b: 新闻管道（运行已有 sentiment ETL）
# ═══════════════════════════════════════════

def run_news_etl():
    import sys, os
    sys.path.insert(0, str(Path("/Users/richard/my_stock_ai/zplan-资讯")))
    os.chdir(str(Path("/Users/richard/my_stock_ai/zplan-资讯")))

    from sentiment_etl.runner import run_sentiment_etl
    stats = run_sentiment_etl(push_wechat=False)
    logger.info("新闻 ETL 完成: %s", stats)


# ═══════════════════════════════════════════

if __name__ == '__main__':
    if '--patterns' in sys.argv or '--all' in sys.argv:
        generate_pattern_events()
    if '--news' in sys.argv or '--all' in sys.argv:
        run_news_etl()
