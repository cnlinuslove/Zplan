"""全市场 A 股 LLM 基础分批量扫描（断点续跑 + 结果入库）。

用法：
  /Users/richard/my_stock_ai/zplan-选股/.venv/bin/python3 all_market_llm_score.py
  # 中断后重跑自动续跑（跳过已评分的 ts_code）

成本估算：~4,244 只 × 批量 10 ≈ 425 次调用 × $0.006 ≈ $2.5-3
耗时：~425 × 1.5s ≈ 11 分钟 + 预处理 ≈ 15-20 分钟
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
from zplan_shared.llm.gemini import gemini_available, pop_usage

DB = Path("/Users/richard/my_stock_ai/zplan-资讯/zplan.db")
OUT_TABLE = "all_market_llm_scores"
BATCH_SIZE = 10

def ensure_table(db: sqlite3.Connection):
    db.execute(f"""
    CREATE TABLE IF NOT EXISTS {OUT_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code VARCHAR(16) NOT NULL,
        as_of DATE NOT NULL,
        rule_composite FLOAT,
        adjusted_score FLOAT,
        recommendation VARCHAR(16),
        trend_one_liner VARCHAR(256),
        vs_rule_engine VARCHAR(256),
        risk_flags_json TEXT,
        risk_penalty FLOAT,
        confidence_adjustment FLOAT,
        llm_raw_json TEXT,
        usage_json TEXT,
        updated_at_utc DATETIME DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_all_market_llm UNIQUE (ts_code, as_of)
    )
    """)
    db.commit()

def get_completed(db: sqlite3.Connection, as_of: str) -> set[str]:
    cur = db.execute(f"SELECT ts_code FROM {OUT_TABLE} WHERE as_of = ?", (as_of,))
    return {r[0] for r in cur.fetchall()}

def preload_concepts(db: sqlite3.Connection) -> dict[str, list[str]]:
    """一次性加载全市场概念，避免 N+1 查询。"""
    cur = db.execute("SELECT ts_code, concept_name FROM stock_concept_members WHERE market='a' ORDER BY ts_code")
    mapping: dict[str, list[str]] = {}
    for ts_code, cname in cur.fetchall():
        mapping.setdefault(ts_code, []).append(cname)
    logger.info("预加载概念: %s 只股票", len(mapping))
    return mapping

def main():
    if not gemini_available():
        logger.error("未配置 DEEPSEEK_API_KEY")
        sys.exit(1)

    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    ensure_table(db)

    AS_OF = "2026-06-05"
    RULE_VERSION = "pick-2026-06-anti-chase"

    # 已完成的跳过
    completed = get_completed(db, AS_OF)
    logger.info("已完成: %s 只", len(completed))

    # 加载全市场概念
    all_concepts = preload_concepts(db)

    # 查询待处理股票
    cur = db.execute("""
    SELECT s.ts_code, s.name, s.tech_score, s.composite_score,
           s.verdict, s.close_price, s.signals_json,
           json_extract(s.features_json, '$.ret_20d') as ret_20d,
           json_extract(s.features_json, '$.kdj_k') as kdj_k,
           json_extract(s.features_json, '$.kdj_d') as kdj_d,
           json_extract(s.features_json, '$.high_60d_pct') as high_60d_pct,
           json_extract(s.features_json, '$.vol_ratio20') as vol_ratio20
    FROM stock_rule_scores s
    WHERE s.trade_date_as_of = ?
      AND s.rule_version = ?
      AND s.market = 'a'
    ORDER BY s.composite_score DESC
    """, (AS_OF, RULE_VERSION))

    all_rows = cur.fetchall()
    pending = [r for r in all_rows if r['ts_code'] not in completed]
    logger.info("总候选: %s 只, 待处理: %s 只", len(all_rows), len(pending))

    if not pending:
        logger.info("全部完成！")
        db.close()
        return

    # 构建 picks
    picks: list[dict] = []
    for r in pending:
        ts = r['ts_code']
        concepts = all_concepts.get(ts, [])
        signals = json.loads(r['signals_json']) if r['signals_json'] else []
        ret20 = float(r['ret_20d']) if r['ret_20d'] else None
        kdj_k = float(r['kdj_k']) if r['kdj_k'] else None
        kdj_d = float(r['kdj_d']) if r['kdj_d'] else None
        high60 = float(r['high_60d_pct']) if r['high_60d_pct'] else None
        vol20 = float(r['vol_ratio20']) if r['vol_ratio20'] else None

        picks.append({
            'ts_code': ts,
            'name': r['name'],
            'industry': None,
            'concepts': concepts[:6],
            'concept_count': len(concepts),
            'close': r['close_price'],
            'ret_20d': ret20,
            'high_60d_pct': high60,
            'vol_ratio20': vol20,
            'tech_score': r['tech_score'],
            'composite_score': r['composite_score'],
            'rule_composite_score': r['composite_score'],
            'suggested_buy': None,
            'close_vs_buy_gap_pct': None,
            'kdj_k': kdj_k,
            'kdj_d': kdj_d,
            'signals': signals[:3],
            'news_48h': None,  # 全量扫描不拉新闻（太慢）
        })

    # 分批次跑
    total_batches = (len(picks) + BATCH_SIZE - 1) // BATCH_SIZE
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "batch_calls": 0}
    saved_count = 0
    t_start = time.monotonic()

    for batch_idx in range(0, len(picks), BATCH_SIZE):
        chunk = picks[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1

        try:
            reviewed, usage = brief_review_scan_picks(
                chunk,
                as_of=AS_OF,
                per_stock=False,  # 批量模式，快
                batch_size=BATCH_SIZE,
            )
        except Exception as e:
            logger.error("批次 %s/%s 失败: %s, 降级逐只...", batch_num, total_batches, e)
            # 降级逐只
            reviewed, usage = [], None
            for p in chunk:
                try:
                    # Fallback: single stock
                    from pick_agent.llm_research import _brief_review_one
                    one = _brief_review_one(p, as_of=AS_OF, model=None)
                    u = one.pop("_usage", None)
                    reviewed.append(one)
                    if u:
                        if usage is None:
                            usage = dict(u)
                        else:
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                usage[k] = int(usage.get(k) or 0) + int(u.get(k) or 0)
                except Exception as e2:
                    logger.error("  逐只 %s 也失败: %s", p.get('ts_code'), e2)
                    reviewed.append(p)

        if usage:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total_usage[k] = int(total_usage.get(k) or 0) + int(usage.get(k) or 0)
            total_usage["batch_calls"] += 1

        # 入库
        for r in reviewed:
            brief = r.get('llm_brief') or {}
            raw_json = json.dumps(brief, ensure_ascii=False) if brief else None
            risk_flags = brief.get('risk_flags') or []
            try:
                db.execute(f"""
                INSERT OR REPLACE INTO {OUT_TABLE}
                    (ts_code, as_of, rule_composite, adjusted_score, recommendation,
                     trend_one_liner, vs_rule_engine, risk_flags_json,
                     risk_penalty, confidence_adjustment, llm_raw_json, usage_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r.get('ts_code'), AS_OF,
                    r.get('rule_composite_score') or r.get('composite_score'),
                    r.get('adjusted_score'),
                    brief.get('recommendation'),
                    brief.get('trend'),
                    brief.get('vs_rule_engine'),
                    json.dumps(risk_flags, ensure_ascii=False),
                    brief.get('risk_penalty'),
                    brief.get('confidence_adjustment'),
                    raw_json,
                    json.dumps(usage) if usage else None,
                ))
                saved_count += 1
            except Exception as e:
                logger.error("入库 %s 失败: %s", r.get('ts_code'), e)

        db.commit()

        # 进度
        elapsed = time.monotonic() - t_start
        progress = saved_count / len(pending) * 100
        eta = elapsed / max(saved_count, 1) * (len(pending) - saved_count)
        logger.info("批次 %s/%s | 已存 %s/%s (%.0f%%) | tokens: %s | 耗时 %.0fs | ETA %.0fs",
                    batch_num, total_batches, saved_count, len(pending),
                    progress, total_usage.get("total_tokens", 0), elapsed, eta)

        # 限速（批量模式内部已有限速，但额外加一点安全边界）
        time.sleep(0.3)

    elapsed = time.monotonic() - t_start
    logger.info("=" * 60)
    logger.info("完成！%s 只股票，耗时 %.0f 秒", saved_count, elapsed)
    logger.info("总用量: prompt=%s, completion=%s, total=%s, calls=%s",
                total_usage.get('prompt_tokens'), total_usage.get('completion_tokens'),
                total_usage.get('total_tokens'), total_usage.get('batch_calls'))
    logger.info(f"结果表: {OUT_TABLE}")

    # 快速统计
    cur = db.execute(f"""
    SELECT recommendation, COUNT(*) as cnt
    FROM {OUT_TABLE} WHERE as_of = ?
    GROUP BY recommendation ORDER BY cnt DESC
    """, (AS_OF,))
    logger.info("推荐分布:")
    for r in cur.fetchall():
        logger.info("  %s: %s", r[0], r[1])

    db.close()

if __name__ == '__main__':
    main()
