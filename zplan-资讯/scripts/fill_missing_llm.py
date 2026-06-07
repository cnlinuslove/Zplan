"""补录缺失的 LLM 基础分：有技术特征但没 LLM 评分的股票。

两类：
  A) 有规则评分(v1 theme) → 正常流程
  B) 无规则评分 → composite_score=50，纯靠 LLM 从原始数据判断
"""
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-选股/src")
sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-共享/src")

from pick_agent.llm_research import brief_review_scan_picks
from zplan_shared.llm.gemini import gemini_available

DB = Path("/Users/richard/my_stock_ai/zplan-资讯/zplan.db")
OUT_TABLE = "all_market_llm_scores"
AS_OF = "2026-06-05"
BATCH_SIZE = 10

def preload_concepts(db: sqlite3.Connection) -> dict[str, list[str]]:
    cur = db.execute("SELECT ts_code, concept_name FROM stock_concept_members WHERE market='a' ORDER BY ts_code")
    mapping: dict[str, list[str]] = {}
    for ts_code, cname in cur.fetchall():
        mapping.setdefault(ts_code, []).append(cname)
    return mapping

def main():
    if not gemini_available():
        logger.error("未配置 DEEPSEEK_API_KEY")
        sys.exit(1)

    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row

    all_concepts = preload_concepts(db)
    logger.info("预加载概念: %s 只", len(all_concepts))

    # 找缺失的股票：有 daily_features 但没有 LLM 评分
    cur = db.execute("""
    SELECT df.ts_code, df.trade_date,
           df.ret_20d, df.kdj_k, df.kdj_d, df.high_60d_pct, df.vol_ratio20,
           df.close, df.ma5_cross_ma20, df.macd_cross_up, df.vol_breakout,
           df.kdj_golden_cross, df.kdj_death_cross, df.above_ma20
    FROM daily_features df
    WHERE df.trade_date = ?
      AND df.market = 'a'
      AND df.ts_code NOT IN (
          SELECT ts_code FROM all_market_llm_scores WHERE as_of = ?
      )
    """, (AS_OF, AS_OF))

    missing = list(cur.fetchall())
    logger.info("待补录: %s 只", len(missing))

    if not missing:
        logger.info("全部已覆盖！")
        db.close()
        return

    # 查这些股票的规则评分（v1 theme）+ 估值 + 财务
    codes = [r['ts_code'] for r in missing]
    placeholders = ','.join(['?'] * len(codes))

    # 规则评分
    rule_scores = {}
    cur2 = db.execute(f"""
        SELECT ts_code, composite_score, tech_score, verdict, close_price, name, signals_json
        FROM stock_rule_scores
        WHERE trade_date_as_of = ? AND rule_version = 'pick-2026-05-theme' AND ts_code IN ({placeholders})
    """, [AS_OF] + codes)
    for r in cur2.fetchall():
        rule_scores[r['ts_code']] = dict(r)

    # 估值快照
    snapshots = {}
    cur3 = db.execute(f"""
        SELECT ts_code, pe_ttm, pb, total_mv, turnover_rate
        FROM daily_snapshot WHERE trade_date = ? AND ts_code IN ({placeholders})
    """, [AS_OF] + codes)
    for r in cur3.fetchall():
        snapshots[r['ts_code']] = dict(r)

    # 财务
    financials = {}
    cur4 = db.execute(f"""
        SELECT ts_code, net_profit, revenue, roe
        FROM financial_indicators WHERE report_date = '2026-03-31' AND ts_code IN ({placeholders})
    """, codes)
    for r in cur4.fetchall():
        financials[r['ts_code']] = dict(r)

    # 名称
    names = {}
    cur5 = db.execute(f"SELECT ts_code, name FROM stock_list WHERE ts_code IN ({placeholders})", codes)
    for r in cur5.fetchall():
        names[r['ts_code']] = r['name']

    # 构建 picks
    picks: list[dict] = []
    type_a = type_b = 0
    for r in missing:
        ts = r['ts_code']
        concepts = all_concepts.get(ts, [])
        rule = rule_scores.get(ts)
        snap = snapshots.get(ts)
        fin = financials.get(ts)

        # 解析数据
        def _f(v):
            try: return float(v)
            except: return None

        ret20 = _f(r['ret_20d'])
        kdj_k = _f(r['kdj_k'])
        kdj_d = _f(r['kdj_d'])
        high60 = _f(r['high_60d_pct'])
        vol20 = _f(r['vol_ratio20'])
        close = _f(r['close'])

        # 从布尔列合成 signals 列表
        signals = []
        if r['ma5_cross_ma20']: signals.append('ma5_cross_ma20')
        if r['macd_cross_up']: signals.append('macd_cross_up')
        if r['vol_breakout']: signals.append('vol_breakout')
        if r['kdj_golden_cross']: signals.append('kdj_golden_cross')
        if r['kdj_death_cross']: signals.append('kdj_death_cross')
        if r['above_ma20']: signals.append('above_ma20')

        if rule:
            # A 类：有规则评分
            composite = rule['composite_score']
            tech_score = rule['tech_score']
            name = rule.get('name')
            rule_sigs = []
            if rule.get('signals_json'):
                try: rule_sigs = json.loads(rule['signals_json'])
                except: pass
            type_a += 1
        else:
            # B 类：无规则评分，给中性分
            composite = 50.0
            tech_score = None
            name = names.get(ts)
            type_b += 1

        picks.append({
            'ts_code': ts,
            'name': name,
            'industry': None,
            'concepts': concepts[:6],
            'concept_count': len(concepts),
            'close': close or (rule.get('close_price') if rule else None),
            'ret_20d': ret20,
            'high_60d_pct': high60,
            'vol_ratio20': vol20,
            'tech_score': tech_score,
            'composite_score': composite,
            'rule_composite_score': composite,
            'suggested_buy': None,
            'close_vs_buy_gap_pct': None,
            'kdj_k': kdj_k,
            'kdj_d': kdj_d,
            'signals': signals[:3],
            'news_48h': None,
        })

    logger.info("A 类(有规则分): %s, B 类(无规则分): %s", type_a, type_b)

    # 分批次跑
    total_batches = (len(picks) + BATCH_SIZE - 1) // BATCH_SIZE
    saved = 0
    t_start = time.monotonic()

    for batch_idx in range(0, len(picks), BATCH_SIZE):
        chunk = picks[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1

        try:
            reviewed, usage = brief_review_scan_picks(
                chunk, as_of=AS_OF, per_stock=False, batch_size=BATCH_SIZE)
        except Exception as e:
            logger.error("批次 %s/%s 失败: %s，降级逐只", batch_num, total_batches, e)
            reviewed = []
            for p in chunk:
                try:
                    from pick_agent.llm_research import _brief_review_one
                    one = _brief_review_one(p, as_of=AS_OF, model=None)
                    one.pop("_usage", None)
                    reviewed.append(one)
                except Exception as e2:
                    logger.error("  逐只 %s 失败: %s", p.get('ts_code'), e2)
                    reviewed.append(p)

        for rv in reviewed:
            brief = rv.get('llm_brief') or {}
            try:
                db.execute(f"""
                INSERT OR REPLACE INTO {OUT_TABLE}
                    (ts_code, as_of, rule_composite, adjusted_score, recommendation,
                     trend_one_liner, vs_rule_engine, risk_flags_json,
                     risk_penalty, confidence_adjustment, llm_raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rv.get('ts_code'), AS_OF,
                    rv.get('rule_composite_score') or rv.get('composite_score'),
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
            except Exception as e:
                logger.error("入库 %s 失败: %s", rv.get('ts_code'), e)

        db.commit()

        elapsed = time.monotonic() - t_start
        pct = saved / len(picks) * 100
        eta = elapsed / max(saved, 1) * (len(picks) - saved)
        logger.info("批次 %s/%s | 已存 %s/%s (%.0f%%) | 耗时 %.0fs | ETA %.0fs",
                    batch_num, total_batches, saved, len(picks), pct, elapsed, eta)
        time.sleep(0.3)

    elapsed = time.monotonic() - t_start
    logger.info("补录完成！%s 只，耗时 %.0f 秒", saved, elapsed)

    # 最终统计
    cur = db.execute(f"""
    SELECT recommendation, COUNT(*) as cnt
    FROM {OUT_TABLE} WHERE as_of = ?
    GROUP BY recommendation ORDER BY cnt DESC
    """, (AS_OF,))
    logger.info("全市场推荐分布:")
    for r in cur.fetchall():
        logger.info("  %s: %s", r[0], r[1])

    total = db.execute(f"SELECT COUNT(*) FROM {OUT_TABLE} WHERE as_of=?", (AS_OF,)).fetchone()[0]
    logger.info("全市场覆盖: %s / 5482 只有特征", total)

    db.close()

if __name__ == '__main__':
    main()
