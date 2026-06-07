"""P0 + P3 修复脚本。

P0: 回填 daily_features 近 60 个交易日（供趋势分析）
P3: 港股 LLM 基础分（复用已有 daily_features 数据）

用法：
  .venv/bin/python3 fix_p0_p3.py --p0        # 仅 P0
  .venv/bin/python3 fix_p0_p3.py --p3        # 仅 P3
  .venv/bin/python3 fix_p0_p3.py --all       # 全部
"""
import json, logging, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-共享/src")
sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-选股/src")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB = Path("/Users/richard/my_stock_ai/zplan-资讯/zplan.db")
AS_OF = "2026-06-05"

def _db():
    db = sqlite3.connect(str(DB), isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


# ═══════════════════════════════════════════
# P0: 回填 daily_features
# ═══════════════════════════════════════════

def p0_backfill_features():
    """用 get_history_window + scan_universe_features 回填近 60 个交易日。"""
    from zplan_shared.etl_daily_features import run_daily_features_update
    from zplan_shared.market import get_history_window, latest_trade_date
    import pandas as pd

    db = _db()
    # 获取最近 60 个交易日
    cur = db.execute("""
    SELECT DISTINCT trade_date FROM daily_prices
    WHERE market = 'a'
    ORDER BY trade_date DESC LIMIT 60
    """)
    dates = [r['trade_date'] for r in cur.fetchall()]
    logger.info("P0: 回填 %s 个交易日", len(dates))

    done = 0
    for trade_date in sorted(dates):
        # 跳过已有的
        existing = db.execute(
            "SELECT COUNT(*) FROM daily_features WHERE trade_date = ? AND market = 'a'",
            (trade_date,)
        ).fetchone()[0]
        if existing > 1000:
            logger.debug("P0: %s 已有 %s 条，跳过", trade_date, existing)
            continue

        try:
            stats = run_daily_features_update(as_of=trade_date, calendar_days=90, market='a')
            done += 1
            if done % 10 == 0:
                logger.info("P0: 进度 %s/%s", done, len(dates))
        except Exception as e:
            logger.warning("P0: %s 失败: %s", trade_date, e)
            time.sleep(1)

    final = db.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_features WHERE market='a'").fetchone()[0]
    logger.info("P0 完成: %s 个交易日有特征数据", final)
    db.close()


# ═══════════════════════════════════════════
# P3: 港股 LLM 基础分
# ═══════════════════════════════════════════

def p3_hk_llm_score():
    from pick_agent.llm_research import brief_review_scan_picks
    from zplan_shared.llm.gemini import llm_available

    if not llm_available():
        logger.error("未配置 API Key")
        return

    db = _db()

    # 港股待评分（有 daily_features 但无 LLM 评分）
    cur = db.execute("""
    SELECT df.ts_code, sl.name,
           df.close, df.ret_20d, df.kdj_k, df.kdj_d,
           df.high_60d_pct, df.vol_ratio20,
           df.ma5_cross_ma20, df.macd_cross_up, df.vol_breakout,
           df.kdj_golden_cross, df.kdj_death_cross, df.above_ma20
    FROM daily_features df
    JOIN stock_list sl ON df.ts_code = sl.ts_code
    WHERE df.trade_date = ? AND df.market = 'hk'
      AND sl.market = 'hk'
      AND df.ts_code NOT IN (
          SELECT ts_code FROM all_market_llm_scores WHERE as_of = ?
      )
    ORDER BY sl.ts_code
    """, (AS_OF, AS_OF))

    rows = cur.fetchall()
    logger.info("P3: 港股待处理 %s 只", len(rows))
    if not rows:
        db.close()
        return

    picks = []
    for r in rows:
        signals = []
        if r['ma5_cross_ma20']: signals.append('ma5_cross_ma20')
        if r['macd_cross_up']: signals.append('macd_cross_up')
        if r['vol_breakout']: signals.append('vol_breakout')
        if r['kdj_golden_cross']: signals.append('kdj_golden_cross')
        if r['kdj_death_cross']: signals.append('kdj_death_cross')
        if r['above_ma20']: signals.append('above_ma20')

        def _f(v):
            try: return float(v)
            except: return None

        picks.append({
            'ts_code': r['ts_code'],
            'name': r['name'],
            'industry': '港股',
            'concepts': [],
            'concept_count': 0,
            'close': _f(r['close']),
            'ret_20d': _f(r['ret_20d']),
            'high_60d_pct': _f(r['high_60d_pct']),
            'vol_ratio20': _f(r['vol_ratio20']),
            'tech_score': 50,
            'composite_score': 50.0,
            'rule_composite_score': 50.0,
            'suggested_buy': None,
            'close_vs_buy_gap_pct': None,
            'kdj_k': _f(r['kdj_k']),
            'kdj_d': _f(r['kdj_d']),
            'signals': signals[:3],
            'news_48h': None,
        })

    logger.info("P3: 开始 LLM 扫描 %s 只港股...", len(picks))
    BATCH = 10
    saved = 0
    t0 = time.monotonic()

    for i in range(0, len(picks), BATCH):
        chunk = picks[i:i+BATCH]
        try:
            reviewed, usage = brief_review_scan_picks(chunk, as_of=AS_OF, per_stock=False, batch_size=BATCH)
            for rv in reviewed:
                brief = rv.get('llm_brief') or {}
                db.execute("""
                INSERT OR REPLACE INTO all_market_llm_scores
                    (ts_code, as_of, rule_composite, adjusted_score, recommendation,
                     trend_one_liner, vs_rule_engine, risk_flags_json,
                     risk_penalty, confidence_adjustment, llm_raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rv.get('ts_code'), AS_OF,
                    rv.get('rule_composite_score') or 50,
                    rv.get('adjusted_score'),
                    brief.get('recommendation'),
                    brief.get('trend'),
                    brief.get('vs_rule_engine'),
                    json.dumps(brief.get('risk_flags') or [], ensure_ascii=False),
                    brief.get('risk_penalty'),
                    brief.get('confidence_adjustment'),
                    json.dumps(brief, ensure_ascii=False) if brief else None,
                ))
                saved += 1
            db.commit()
        except Exception as e:
            logger.error("P3 批次 %s 失败: %s", i//BATCH, e)

        if saved % 100 == 0:
            elapsed = time.monotonic() - t0
            logger.info("P3: %s/%s (%.0fs)", saved, len(picks), elapsed)
        time.sleep(0.3)

    elapsed = time.monotonic() - t0
    logger.info("P3 完成: %s 只港股, %.0f 秒", saved, elapsed)

    # 统计
    cur = db.execute("""
    SELECT recommendation, COUNT(*) FROM all_market_llm_scores
    WHERE as_of = ? AND ts_code IN (SELECT ts_code FROM stock_list WHERE market='hk')
    GROUP BY recommendation
    """, (AS_OF,))
    for r in cur.fetchall():
        logger.info("  %s: %s", r[0], r[1])

    db.close()


# ═══════════════════════════════════════════

if __name__ == '__main__':
    if '--p0' in sys.argv or '--all' in sys.argv:
        p0_backfill_features()
    if '--p3' in sys.argv or '--all' in sys.argv:
        p3_hk_llm_score()
