"""用 DeepSeek 对 run 8 的 300 只股票重新 LLM 简评，保留 Gemini 版作对比。

用法：
  cd zplan-选股 && .venv/bin/python scripts/deepseek_rescore_run8.py

缓存：LLM 结果缓存到 /tmp/deepseek_rescore_cache.json，断点续跑。
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "zplan-共享", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("deepseek_rescore")

CACHE_PATH = "/tmp/deepseek_rescore_cache.json"

from zplan_shared.models import PickEntry, PickRun, SessionLocal
from zplan_shared.llm.deepseek import deepseek_available
from pick_agent.llm_research import _brief_review_one
from pick_agent.ranking import assign_ranks, sort_picks_for_rank
from pick_agent.strategy import load_strategy


def load_cache() -> dict[str, dict]:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False)


def main():
    # 1. 读取 run 8 的 pick_entries
    with SessionLocal() as session:
        entries = (
            session.query(PickEntry)
            .filter(PickEntry.run_id == 8)
            .order_by(PickEntry.rank_in_run)
            .all()
        )

    if not entries:
        logger.error("未找到 run 8 的 pick_entries")
        return 1

    picks: list[dict] = []
    for e in entries:
        analysis = {}
        if e.analysis_process_json:
            try:
                analysis = json.loads(e.analysis_process_json)
            except json.JSONDecodeError:
                pass

        p = {
            "ts_code": e.ts_code,
            "name": e.name,
            "close": e.close_price,
            "composite_score": e.rule_composite_score,
            "rule_composite_score": e.rule_composite_score,
            "tech_score": e.rule_tech_score,
            "ret_20d": analysis.get("ret_20d"),
            "high_60d_pct": analysis.get("high_60d_pct"),
            "kdj_k": (analysis.get("kdj") or {}).get("k"),
            "kdj_d": (analysis.get("kdj") or {}).get("d"),
            "signals": analysis.get("signals", []),
            "news_mentions_48h": analysis.get("news_mentions_48h", 0),
            "predicted_buy_price": e.predicted_buy_price,
            "predicted_target_price": e.predicted_target_price,
            "predicted_stop_loss": e.predicted_stop_loss,
            "industry": analysis.get("industry_relative_note"),
            "concepts": analysis.get("concepts"),
            "llm_brief": analysis.get("llm_brief"),
        }
        picks.append(p)

    logger.info("从 run 8 读取 %s 只股票", len(picks))

    # 2. 检查缓存
    cache = load_cache()
    cached_count = sum(1 for p in picks if p["ts_code"] in cache)
    if cached_count:
        logger.info("缓存命中 %s/%s 只，跳过已完成的 API 调用", cached_count, len(picks))

    # 3. 逐只用 DeepSeek 简评（带缓存）
    usage_total = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model": "deepseek-chat", "batch_calls": 0, "mode": "per_stock"}
    rescored: list[dict] = []

    for i, p in enumerate(picks, 1):
        code = p["ts_code"]
        if code in cache:
            # 从缓存恢复
            cached = cache[code]
            out = {**p}
            out["llm_composite_score"] = cached.get("llm_composite_score")
            out["composite_score"] = cached.get("llm_composite_score") or p["rule_composite_score"]
            out["llm_brief"] = cached.get("llm_brief")
            rescored.append(out)
        else:
            try:
                one = _brief_review_one(p, as_of="2026-05-21", model="deepseek-chat")
            except Exception as exc:
                logger.warning("❌ %s/%s %s %s: %s", i, len(picks), code, p.get("name"), exc)
                one = {**p}
                one["llm_composite_score"] = p["rule_composite_score"]
                one["composite_score"] = p["rule_composite_score"]
                one["llm_brief"] = {"trend": f"API 失败: {exc}", "recommendation": "观望", "vs_rule_engine": "API 调用失败"}

            u = one.pop("_usage", None)
            if u:
                for k in ("prompt_tokens", "output_tokens", "total_tokens"):
                    usage_total[k] = int(usage_total.get(k) or 0) + int(u.get(k) or 0)
            usage_total["batch_calls"] = i

            rescored.append(one)

            # 写入缓存
            cache[code] = {
                "llm_composite_score": one.get("llm_composite_score"),
                "composite_score": one.get("composite_score"),
                "llm_brief": one.get("llm_brief"),
            }
            save_cache(cache)

        if i % 25 == 0 or i == len(picks):
            logger.info("DeepSeek 简评 %s/%s", i, len(picks))

    # 4. 排序
    strat = load_strategy()
    rescored = sort_picks_for_rank(rescored, strat)
    assign_ranks(rescored)

    # 5. 保存
    from zplan_shared.pick_store import save_scan_run

    result = {
        "ok": True,
        "agent": "pick",
        "run_kind": "llm_top300",
        "as_of": "2026-05-21",
        "rule_version": strat.rule_version,
        "source": "stock_rule_scores",
        "top_n": len(rescored),
        "deepen": False,
        "picks": rescored,
        "qualified": len(rescored),
        "llm_scan_brief": True,
        "llm_usage": usage_total,
    }

    run_id = save_scan_run(
        result,
        params={
            "top_n": len(rescored),
            "source": "run_8_reevaluation",
            "deepen": False,
            "note": "DeepSeek 重打分，原 Gemini run 8 保留作对比",
        },
    )

    # 修正 run_kind
    with SessionLocal() as session:
        run = session.query(PickRun).filter(PickRun.id == run_id).first()
        if run:
            run.run_kind = "llm_top300"
            session.commit()

    logger.info("✅ 新 run_id=%s（run_kind=llm_top300）已保存", run_id)

    # 6. 对比摘要
    ds_scores = [p.get("llm_composite_score") or p.get("composite_score") or 0 for p in rescored]
    gm_scores = [e.llm_composite_score or 0 for e in entries]
    rule_scores = [e.rule_composite_score or 0 for e in entries]

    if ds_scores and gm_scores:
        d_avg = sum(ds_scores) / len(ds_scores)
        g_avg = sum(gm_scores) / len(gm_scores)
        r_avg = sum(rule_scores) / len(rule_scores)
        deltas = [d - g for d, g in zip(ds_scores, gm_scores)]

        logger.info("")
        logger.info("=" * 60)
        logger.info("Gemini vs DeepSeek 打分对比（300 只）")
        logger.info("=" * 60)
        logger.info("Rule 均分:       %.1f", r_avg)
        logger.info("Gemini 均分:     %.1f  (vs rule %+.1f)", g_avg, g_avg - r_avg)
        logger.info("DeepSeek 均分:   %.1f  (vs rule %+.1f)", d_avg, d_avg - r_avg)
        logger.info("DS vs Gemini:    %+.1f  (范围 %.1f ~ %.1f)", sum(deltas)/len(deltas), min(deltas), max(deltas))

        def bucket(scores):
            dist = {"<70": 0, "70-79": 0, "80-89": 0, "90-100": 0}
            for s in scores:
                if s < 70: dist["<70"] += 1
                elif s < 80: dist["70-79"] += 1
                elif s < 90: dist["80-89"] += 1
                else: dist["90-100"] += 1
            return dist

        gd = bucket(gm_scores)
        dd = bucket(ds_scores)

        logger.info("")
        logger.info("分数分布:")
        logger.info("  区间      Gemini   DeepSeek")
        for k in ["<70", "70-79", "80-89", "90-100"]:
            logger.info("  %-8s  %3d      %3d", k, gd[k], dd[k])

        logger.info("")
        logger.info("Token 用量: prompt=%s output=%s total=%s",
                     usage_total.get("prompt_tokens"), usage_total.get("output_tokens"), usage_total.get("total_tokens"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
