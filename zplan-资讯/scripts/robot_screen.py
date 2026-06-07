"""机器人概念股筛选：规则引擎 + LLM 基础分风险扫描。"""
import json
import sqlite3
import sys
sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-选股/src")
sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-共享/src")

from pick_agent.concept_tags import concepts_for_code
from pick_agent.llm_research import brief_review_scan_picks

DB = "/Users/richard/my_stock_ai/zplan-资讯/zplan.db"

def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    # 1. 查询候选池
    cur = db.execute("""
    WITH concept_stocks AS (
        SELECT DISTINCT ts_code FROM stock_concept_members
        WHERE concept_name IN ('人形机器人', '机器人执行器') AND market='a'
    ),
    latest_scores AS (
        SELECT s.*,
               json_extract(s.features_json, '$.ret_20d') as ret_20d,
               json_extract(s.features_json, '$.kdj_k') as kdj_k,
               json_extract(s.features_json, '$.kdj_d') as kdj_d,
               json_extract(s.features_json, '$.high_60d_pct') as high_60d_pct,
               json_extract(s.features_json, '$.vol_ratio20') as vol_ratio20
        FROM stock_rule_scores s
        WHERE s.trade_date_as_of = '2026-06-05'
          AND s.rule_version = 'pick-2026-06-anti-chase'
    ),
    latest_snap AS (
        SELECT * FROM daily_snapshot WHERE trade_date = '2026-06-05'
    ),
    latest_fin AS (
        SELECT f1.*, f2.revenue as prev_revenue
        FROM financial_indicators f1
        LEFT JOIN financial_indicators f2 ON f1.ts_code = f2.ts_code AND f2.report_date = '2025-03-31'
        WHERE f1.report_date = '2026-03-31'
    )
    SELECT
        cs.ts_code, sc.name, sc.tech_score, sc.composite_score,
        sc.verdict, sc.close_price, sc.signals_json,
        sc.ret_20d, sc.kdj_k, sc.kdj_d, sc.high_60d_pct, sc.vol_ratio20,
        fi.revenue, fi.prev_revenue, fi.net_profit,
        sn.pe_ttm, sn.pb, sn.total_mv, sn.turnover_rate
    FROM concept_stocks cs
    JOIN latest_scores sc ON cs.ts_code = sc.ts_code
    LEFT JOIN latest_fin fi ON cs.ts_code = fi.ts_code
    LEFT JOIN latest_snap sn ON cs.ts_code = sn.ts_code
    """)

    raw = cur.fetchall()
    print(f"1. 候选池（有规则评分）: {len(raw)} 只")

    # 2. 过滤
    candidates = []
    for r in raw:
        score = r['composite_score'] or 0
        pe = r['pe_ttm']
        profit = r['net_profit']
        if score < 40:
            continue
        if profit is None or profit <= 0:
            continue
        if pe is not None and (pe <= 0 or pe > 80):
            continue

        revenue = r['revenue']
        prev_rev = r['prev_revenue']
        rev_growth = None
        if revenue and prev_rev and prev_rev > 0:
            rev_growth = round((revenue - prev_rev) / prev_rev * 100, 1)

        signals = json.loads(r['signals_json']) if r['signals_json'] else []
        ret20 = float(r['ret_20d']) if r['ret_20d'] else None
        kdj_k = float(r['kdj_k']) if r['kdj_k'] else None
        kdj_d = float(r['kdj_d']) if r['kdj_d'] else None
        high60 = float(r['high_60d_pct']) if r['high_60d_pct'] else None
        vol20 = float(r['vol_ratio20']) if r['vol_ratio20'] else None

        candidates.append({
            'ts_code': r['ts_code'],
            'name': r['name'],
            'composite_score': round(score, 1),
            'tech_score': round(r['tech_score'], 1) if r['tech_score'] else 0,
            'close': r['close_price'],
            'ret_20d': ret20,
            'kdj_k': kdj_k,
            'kdj_d': kdj_d,
            'high_60d_pct': high60,
            'vol_ratio20': vol20,
            'signals': signals,
            # extra fields for output
            '_pe_ttm': round(pe, 1) if pe else None,
            '_pb': round(r['pb'], 2) if r['pb'] else None,
            '_total_mv': round(r['total_mv'], 0) if r['total_mv'] else None,
            '_profit_100m': round(profit / 1e8, 2) if profit else None,
            '_rev_growth': rev_growth,
            '_turnover': round(r['turnover_rate'], 2) if r['turnover_rate'] else None,
        })

    candidates.sort(key=lambda x: x['composite_score'], reverse=True)
    top_n = min(40, len(candidates))
    top = candidates[:top_n]
    print(f"2. 财务筛选后: {len(candidates)} 只, 跑 LLM top {top_n}")

    # 3. 附加概念标签
    for p in top:
        p['concepts'] = concepts_for_code(str(p['ts_code']), limit=6)

    # 4. 跑 LLM 简评
    print("3. 开始 LLM 风险扫描（每只约 1.5s）...")
    reviewed, usage = brief_review_scan_picks(
        top,
        as_of="2026-06-05",
        per_stock=True,
    )

    if usage:
        print(f"   LLM 用量: prompt={usage.get('prompt_tokens')}, "
              f"completion={usage.get('completion_tokens')}, "
              f"total={usage.get('total_tokens')}")

    # 5. 排序输出
    # 先按 adjusted_score 排（包含风险扣分），fallback composite_score
    def sort_key(p):
        adj = p.get('adjusted_score')
        if adj is not None:
            return adj
        return p.get('composite_score', 0)

    reviewed.sort(key=sort_key, reverse=True)

    print(f"\n{'='*120}")
    print(f"  机器人概念股推荐 — 人形机器人 + 机器人执行器（数据截止 2026-06-05）")
    print(f"{'='*120}")
    print(f"{'排名':<4} {'代码':<12} {'名称':<8} {'调整分':>6} {'规则分':>6} {'建议':<6} {'PE':>7} {'利润亿':>7} {'营收增速':>7} {'20日涨':>7} {'风险要点'}")
    print(f"{'-'*120}")

    for i, r in enumerate(reviewed, 1):
        brief = r.get('llm_brief') or {}
        adj = r.get('adjusted_score') or r.get('composite_score')
        rec = brief.get('recommendation', r.get('verdict', '-'))
        flags = brief.get('risk_flags') or []
        risk_str = '、'.join(flags[:3]) if flags else '-'
        pe = r.get('_pe_ttm')
        profit = r.get('_profit_100m')
        rev_g = r.get('_rev_growth')
        ret20 = r.get('ret_20d')

        pe_str = f"{pe:.1f}" if pe else "-"
        profit_str = f"{profit:.1f}" if profit else "-"
        rev_str = f"{rev_g:.1f}%" if rev_g is not None else "-"
        ret_str = f"{ret20:.1f}%" if ret20 is not None else "-"

        print(f"{i:<4} {r['ts_code']:<12} {r['name']:<8} {adj:>6.1f} {r['composite_score']:>6.1f} "
              f"{rec:<6} {pe_str:>7} {profit_str:>7} {rev_str:>7} {ret_str:>7} {risk_str}")

    # 6. 推荐详情
    print(f"\n{'='*120}")
    print("  LLM 逐只简评")
    print(f"{'='*120}")
    for i, r in enumerate(reviewed, 1):
        brief = r.get('llm_brief') or {}
        trend = brief.get('trend', '')
        vs_rule = brief.get('vs_rule_engine', '')
        rec = brief.get('recommendation', '-')
        mv = r.get('_total_mv')
        mv_str = f"{mv/1e8:.0f}亿" if mv else "-"
        print(f"\n{i}. {r['ts_code']} {r['name']} | 调整分{r.get('adjusted_score', '-'):.1f} | {rec} | PE{r.get('_pe_ttm','-')} | 市值{mv_str}")
        print(f"   📈 {trend}")
        print(f"   ⚠️  vs规则引擎: {vs_rule}")

    db.close()
    return reviewed

if __name__ == '__main__':
    main()
