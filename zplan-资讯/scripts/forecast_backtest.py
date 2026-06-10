#!/usr/bin/env python3
"""大盘预测历史回测：在历史节点仅用当时可得数据生成预测，再与实际走势对照。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/forecast_backtest.py
    cd zplan-资讯 && .venv/bin/python scripts/forecast_backtest.py --from 2026-01-01 --every 7
    cd zplan-资讯 && .venv/bin/python scripts/forecast_backtest.py --sample-count 20
    cd zplan-资讯 && .venv/bin/python scripts/forecast_backtest.py --dry-run   # 仅预览日期
    cd zplan-资讯 && .venv/bin/python scripts/forecast_backtest.py --eval-only # 仅评估已有回测

输出:
    - backtest_forecasts/forecasts.jsonl  原始预测 JSONL
    - backtest_forecasts/benchmark.json   评估汇总
    - 终端打印 Benchmark 报告
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(os.environ.get("ZPLAN_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(NEWS_ROOT))

from sqlalchemy import text

from zplan_shared.config import FORECAST_VERIFY_THRESHOLD_PCT, LLM_MODEL
from zplan_shared.llm.gemini import generate_json, llm_available
from zplan_shared.market import get_index_bars, latest_index_trade_date
from zplan_shared.models import init_db, SessionLocal

BEIJING_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)

# 复用 market_forecast 的 Schema 和工具函数
from scripts.market_forecast import (
    _A_INDEX_ORDER,
    _GLOBAL_INDEX_ORDER,
    _INDEX_NAMES,
    _FORECAST_SCHEMA,
    _get_daily_changes,
    _get_market_breadth,
    _get_northbound_recent,
    _get_industry_performance,
    _get_concept_heat,
    _get_policy_news,
    _build_index_summary,
    _build_global_summary,
    _build_forecast_prompt,
)

BACKTEST_DIR = NEWS_ROOT / "backtest_forecasts"
FORECASTS_JSONL = BACKTEST_DIR / "forecasts.jsonl"
BENCHMARK_JSON = BACKTEST_DIR / "benchmark.json"


# ═══ 交易日列表 ═══


def _get_trading_days(from_date: date, to_date: date) -> list[date]:
    """获取区间内所有有指数数据的交易日。"""
    with SessionLocal() as session:
        rows = session.execute(
            text(
                "SELECT DISTINCT trade_date FROM daily_index "
                "WHERE trade_date >= :f AND trade_date <= :t "
                "ORDER BY trade_date"
            ),
            {"f": from_date.isoformat(), "t": to_date.isoformat()},
        ).fetchall()
    return [date.fromisoformat(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


def _sample_dates(
    trading_days: list[date],
    every: int = 7,
    sample_count: int | None = None,
) -> list[date]:
    """从交易日列表中按间隔采样。

    总是包含最后一个交易日（最新），其余按间隔均匀分布。
    """
    if not trading_days:
        return []
    if sample_count and sample_count > 0:
        # 均匀采样 N 个
        n = min(sample_count, len(trading_days))
        step = max(1, len(trading_days) // n)
        sampled = trading_days[::step]
        # 确保最后一个在里面
        if sampled[-1] != trading_days[-1]:
            sampled.append(trading_days[-1])
        return sampled[-n:]  # 取最后 N 个
    # 按日历间隔采样
    sampled = []
    last_picked: date | None = None
    for d in trading_days:
        if last_picked is None or (d - last_picked).days >= every:
            sampled.append(d)
            last_picked = d
    # 确保最后一个在里面
    if sampled and sampled[-1] != trading_days[-1]:
        sampled.append(trading_days[-1])
    return sampled


# ═══ 单日预测 ═══


def _generate_forecast_for_date(as_of_date: date, *, skip_charts: bool = True) -> dict[str, Any] | None:
    """在指定历史日期生成预测（仅用该日期之前的数据）。

    Args:
        as_of_date: 预测日期（"当日"收盘后）
        skip_charts: 跳过图表生成（回测时不需要）
    """
    logger.info("回测预测: %s", as_of_date)

    with SessionLocal() as session:
        # 1. 市场数据（仅用 as_of_date 当天及之前的数据）
        changes = _get_daily_changes(session, as_of_date)
        breadth = _get_market_breadth(changes)
        northbound = _get_northbound_recent(session, days=5)
        industries = _get_industry_performance(session, changes)
        concepts = _get_concept_heat(session, changes)
        policy_news = _get_policy_news(session, days=2)

    # 2. 指数 K 线 + 形态搜索（截止到 as_of_date）
    index_summaries: list[dict[str, Any]] = []
    for code in _A_INDEX_ORDER:
        try:
            bars_df = get_index_bars(code, lookback=365, end=as_of_date)
        except Exception as exc:
            logger.warning("拉取指数 %s K 线失败: %s", code, exc)
            index_summaries.append({"code": code, "name": _INDEX_NAMES.get(code, code)})
            continue

        # 相似形态搜索
        patterns = None
        try:
            from zplan_shared.pattern_similarity import search_similar_index_patterns
            # 注意：pattern_similarity 内部使用 get_index_bars，需要确保也用 end=as_of_date
            # 该函数当前不支持 end 参数，需改用 _search_with_cutoff
            patterns = _search_similar_patterns_with_cutoff(code, as_of_date)
        except Exception as exc:
            logger.warning("指数 %s 相似形态搜索失败: %s", code, exc)

        summary = _build_index_summary(code, as_of_date, bars_df, patterns)
        index_summaries.append(summary)

    # 3. 外盘指数
    global_summaries: list[dict[str, Any]] = []
    for code in _GLOBAL_INDEX_ORDER:
        try:
            g_bars = get_index_bars(code, lookback=60, end=as_of_date)
            gs = _build_global_summary(code, g_bars)
            global_summaries.append(gs)
        except Exception as exc:
            logger.warning("外盘 %s 失败: %s", code, exc)

    # 4. LLM 预测
    prompt = _build_forecast_prompt(
        trade_date=as_of_date,
        breadth=breadth,
        northbound=northbound,
        industries=industries,
        concepts=concepts,
        index_summaries=index_summaries,
        global_summaries=global_summaries,
        policy_news=policy_news,
    )

    try:
        result = generate_json(
            prompt=prompt,
            response_schema=_FORECAST_SCHEMA,
            temperature=0.3,
            max_output_tokens=8192,
        )
        # generate_json 直接返回解析后的 dict，__usage__ 是元数据
        usage = result.pop("__usage__", None)
        if usage:
            logger.info("LLM 用量: %s", usage)
    except Exception as exc:
        logger.error("LLM 调用失败 (%s): %s", as_of_date, exc)
        return None

    if not result or not result.get("market_direction"):
        logger.error("LLM 返回无效 (%s): 缺少 market_direction", as_of_date)
        return None

    forecast_data = result
    market_dir = forecast_data.get("market_direction", {})
    return {
        "as_of_date": as_of_date.isoformat(),
        "market_direction": market_dir.get("direction", "?"),
        "direction_confidence": market_dir.get("confidence", 0),
        "forecast_data": forecast_data,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": LLM_MODEL,
    }


def _search_similar_patterns_with_cutoff(code: str, as_of_date: date) -> dict[str, Any] | None:
    """相似形态搜索 — 带数据截止日期，避免前视偏差。

    search_similar_index_patterns 内部调用 get_index_bars 时可能不会正确
    截止到 as_of_date，因此回测使用手动实现确保数据窗口正确。
    """
    bars = get_index_bars(code, lookback=2000, end=as_of_date)
    if bars.empty or len(bars) < 120:
        return None
    return _manual_pattern_search(code, bars, as_of_date)


def _manual_pattern_search(
    code: str,
    bars: "pd.DataFrame",
    as_of_date: date,
    top_k: int = 3,
    min_similarity: float = 0.65,
) -> dict[str, Any] | None:
    """手动相似形态搜索（完全控制数据窗口，杜绝前视偏差）。

    复用 pattern_similarity._feature_vector 计算特征，但自己控制搜索窗口。
    """
    import numpy as np
    from zplan_shared.features import enrich_bars, latest_features
    from zplan_shared.pattern_similarity import _feature_vector, _euclidean_distance, _normalize

    bars = bars.sort_index()
    enriched = enrich_bars(bars)

    # 当前窗口：最后 20 个交易日
    if len(enriched) < 60:
        return None

    # 用 latest_features 计算当前窗口的特征
    current_window = enriched.tail(20)
    cur_feats = latest_features(current_window)
    cur_close = float(current_window["close"].iloc[-1])
    cur_vec_raw = _feature_vector(cur_feats, cur_close)
    cur_vec = _normalize(cur_vec_raw)

    if cur_vec is None or np.all(cur_vec == 0):
        return None

    # 搜索历史窗口
    history_end = len(enriched) - 21
    matches = []
    for i in range(20, history_end, 5):  # 每 5 天采样
        window = enriched.iloc[i : i + 20]
        if len(window) < 20:
            continue
        try:
            hist_feats = latest_features(window)
            hist_close = float(window["close"].iloc[-1])
            hist_vec = _normalize(_feature_vector(hist_feats, hist_close))
            if hist_vec is None:
                continue
        except Exception:
            continue
        sim = _euclidean_distance(cur_vec, hist_vec)
        if sim >= min_similarity:
            fwd_idx = i + 20
            fwd_return = None
            if fwd_idx + 20 < len(enriched):
                fwd_close = float(enriched.iloc[fwd_idx]["close"])
                fwd20_close = float(enriched.iloc[fwd_idx + 20]["close"])
                if fwd_close > 0:
                    fwd_return = round((fwd20_close - fwd_close) / fwd_close * 100, 2)
            matches.append({
                "idx": i,
                "date": str(enriched.index[i + 10]),
                "similarity": round(float(sim), 4),
                "fwd_return_20d": fwd_return,
            })

    matches.sort(key=lambda m: -m["similarity"])
    top = matches[:top_k]

    wins = [m for m in top if m.get("fwd_return_20d") is not None and m["fwd_return_20d"] > 0]
    win_rate = len(wins) / len(top) if top else 0
    avg_fwd = round(float(np.mean([m["fwd_return_20d"] for m in top if m.get("fwd_return_20d") is not None])), 2) if top else 0

    return {
        "matches": top,
        "summary": {
            "verdict": f"历史相似形态胜率 {win_rate:.0%}，均前向收益 {avg_fwd:+.2f}%",
            "win_rate": win_rate,
            "avg_fwd_return": avg_fwd,
        },
    }


# ═══ 评估 ═══


def _evaluate_forecast_at_horizons(
    record: dict[str, Any],
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """对单条回测预测执行多周期评估。"""
    if horizons is None:
        horizons = [1, 3, 5, 20]

    as_of_date = date.fromisoformat(record["as_of_date"])
    fd = record.get("forecast_data") or {}
    market_dir = fd.get("market_direction") or {}
    predicted_dir = market_dir.get("direction", "?")

    horizon_results = {}
    for h in horizons:
        # 计算各指数 forward 涨跌
        from zplan_shared.forecast_evaluate import _fwd_change_for_code, _resolve_direction, _resolve_direction_cn
        import pandas as pd
        from zplan_shared.forecast_evaluate import _A_INDEX_CODES

        fwd = {}
        for code in _A_INDEX_CODES:
            fwd[code] = _fwd_change_for_code(code, h, as_of_date)

        actual_pct = fwd.get("000001", float("nan"))
        if pd.isna(actual_pct):
            horizon_results[str(h)] = {"error": "forward 数据不足"}
            continue

        actual_dir = _resolve_direction(actual_pct, FORECAST_VERIFY_THRESHOLD_PCT)

        # 各指数对照
        index_forecasts = fd.get("index_forecasts") or []
        idx_correct = 0
        idx_total = 0
        idx_details = []
        for ix in index_forecasts:
            code = ix.get("code", "")
            act_pct = fwd.get(code, float("nan"))
            if pd.isna(act_pct):
                continue
            act_cn = _resolve_direction_cn(act_pct, FORECAST_VERIFY_THRESHOLD_PCT)
            pred_cn = ix.get("direction", "")
            matched = (
                (pred_cn == "偏多" and act_cn == "偏多")
                or (pred_cn == "偏空" and act_cn == "偏空")
                or (pred_cn == "震荡" and act_cn == "震荡")
            )
            if matched:
                idx_correct += 1
            idx_total += 1
            idx_details.append({
                "code": code,
                "name": ix.get("name", code),
                "predicted": pred_cn,
                "actual": act_cn,
                "actual_pct": act_pct,
                "correct": matched,
            })

        horizon_results[str(h)] = {
            "predicted": predicted_dir,
            "actual": actual_dir,
            "actual_pct": round(actual_pct, 2),
            "direction_correct": predicted_dir == actual_dir,
            "index_correct": f"{idx_correct}/{idx_total}",
            "index_correct_count": idx_correct,
            "index_total": idx_total,
        }

    return horizon_results


# ═══ 主流程 ═══


def main():
    import argparse
    ap = argparse.ArgumentParser(description="大盘预测历史回测")
    ap.add_argument("--from", dest="from_date", default=None, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", default=None, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--every", type=int, default=7, help="采样间隔（交易日）")
    ap.add_argument("--sample-count", type=int, default=None, help="采样数量（覆盖 --every）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览采样日期")
    ap.add_argument("--eval-only", action="store_true", help="仅评估已有回测 JSONL")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM，仅用规则信号")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    init_db()
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

    # 确定日期范围
    today = latest_index_trade_date()
    if not today:
        logger.error("daily_index 无数据")
        sys.exit(1)

    from_d = date.fromisoformat(args.from_date) if args.from_date else today - timedelta(days=180)
    to_d = date.fromisoformat(args.to_date) if args.to_date else today

    trading_days = _get_trading_days(from_d, to_d)
    logger.info("交易日范围: %s ~ %s, 共 %s 天", from_d, to_d, len(trading_days))

    sampled = _sample_dates(trading_days, every=args.every, sample_count=args.sample_count)
    logger.info("采样 %s 个日期: %s ... %s", len(sampled), sampled[0] if sampled else "N/A", sampled[-1] if sampled else "N/A")

    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"回测日期预览（共 {len(sampled)} 个）")
        print(f"{'='*60}")
        for i, d in enumerate(sampled):
            print(f"  {i+1:>3}. {d} ({d.strftime('%A')})")
        est_cost = len(sampled) * 0.30
        print(f"\n预计 LLM 费用: ~¥{est_cost:.2f} ({len(sampled)} 次 × ¥0.30/次)")
        print(f"模型: {LLM_MODEL}")
        return

    if args.eval_only:
        _run_eval_only()
        return

    # ═══ 逐日生成预测 ═══
    records = []
    fail_n = 0
    for i, d in enumerate(sampled):
        ts = datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
        print(f"\n[{ts}] [{i+1}/{len(sampled)}] 回测预测: {d}")

        if not llm_available():
            print(f"  ⚠️ LLM 不可用，终止")
            break

        record = _generate_forecast_for_date(d, skip_charts=True)
        if record is None:
            fail_n += 1
            print(f"  ❌ 预测失败")
            continue

        records.append(record)

        # 每 5 条存盘一次
        if len(records) % 5 == 0:
            _save_jsonl(records)

    # 最后存盘
    _save_jsonl(records)

    print(f"\n{'='*60}")
    print(f"回测预测完成: {len(records)} 成功, {fail_n} 失败")
    print(f"结果: {FORECASTS_JSONL}")
    print(f"{'='*60}")

    # ═══ 评估 ═══
    _run_benchmark(records)


def _save_jsonl(records: list[dict]) -> None:
    """增量保存预测记录到 JSONL。"""
    existing = set()
    if FORECASTS_JSONL.exists():
        for line in FORECASTS_JSONL.read_text(encoding="utf-8").strip().splitlines():
            try:
                r = json.loads(line)
                existing.add(r.get("as_of_date", ""))
            except json.JSONDecodeError:
                pass

    with FORECASTS_JSONL.open("a", encoding="utf-8") as f:
        for r in records:
            if r.get("as_of_date", "") not in existing:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                existing.add(r.get("as_of_date", ""))


def _load_jsonl() -> list[dict[str, Any]]:
    if not FORECASTS_JSONL.exists():
        return []
    records = []
    for line in FORECASTS_JSONL.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _run_eval_only() -> None:
    records = _load_jsonl()
    if not records:
        print("无回测数据，请先运行回测")
        return
    _run_benchmark(records)


def _run_benchmark(records: list[dict[str, Any]]) -> None:
    """运行多周期评估并打印 Benchmark 报告。"""
    horizons = [1, 3, 5, 20]

    print(f"\n{'='*60}")
    print(f"📊 Benchmark 评估（{len(records)} 条预测）")
    print(f"{'='*60}")

    # 逐条评估
    evaluated = []
    for i, rec in enumerate(records):
        d = rec["as_of_date"]
        print(f"  [{i+1}/{len(records)}] 评估 {d}...", end=" ")
        try:
            hresult = _evaluate_forecast_at_horizons(rec, horizons)
            rec["evaluation"] = hresult
            evaluated.append(rec)
            h1 = hresult.get("1", {})
            print(f"1d: {h1.get('predicted','?')}→{h1.get('actual','?')} {'✅' if h1.get('direction_correct') else '❌'}")
        except Exception as exc:
            print(f"❌ {exc}")

    # 汇总统计
    print(f"\n{'─'*60}")
    print(f"📈 多周期准确率汇总")
    print(f"{'─'*60}")
    print(f"{'Horizon':<12} {'样本':>6} {'正确':>6} {'准确率':>10} {'均值收益':>10}")
    print(f"{'─'*60}")

    benchmark = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "sample_count": len(records), "horizons": {}}

    for h in horizons:
        hkey = str(h)
        correct = 0
        total = 0
        returns = []
        for rec in evaluated:
            ev = (rec.get("evaluation") or {}).get(hkey) or {}
            if ev.get("error"):
                continue
            total += 1
            if ev.get("direction_correct"):
                correct += 1
            ap = ev.get("actual_pct")
            if ap is not None:
                returns.append(ap)

        acc = round(correct / total * 100, 1) if total > 0 else None
        avg_ret = round(sum(returns) / len(returns), 2) if returns else None
        acc_str = f"{acc}%" if acc is not None else "N/A"
        ret_str = f"{avg_ret}%" if avg_ret is not None else "N/A"
        print(f"{h}天{'':>8} {total:>6} {correct:>6} {acc_str:>10} {ret_str:>10}")

        benchmark["horizons"][hkey] = {
            "total": total,
            "correct": correct,
            "accuracy_pct": acc,
            "avg_actual_return": avg_ret,
        }

    # 方向对称性
    print(f"\n{'─'*60}")
    print(f"📊 方向对称性检查")
    print(f"{'─'*60}")
    dir_stats: dict[str, dict[str, int]] = {}
    for rec in evaluated:
        ev = (rec.get("evaluation") or {}).get("1") or {}
        pred = ev.get("predicted", "?")
        if pred not in dir_stats:
            dir_stats[pred] = {"count": 0, "correct": 0}
        dir_stats[pred]["count"] += 1
        if ev.get("direction_correct"):
            dir_stats[pred]["correct"] += 1

    for d, s in sorted(dir_stats.items()):
        pct = round(s["correct"] / s["count"] * 100, 1) if s["count"] > 0 else 0
        bar = "█" * int(pct / 5)
        print(f"  {d:<15s} {s['count']:>4} 次  {s['correct']:>4} 正确  {pct:>5.1f}% {bar}")

    benchmark["by_direction"] = {
        d: {**s, "accuracy_pct": round(s["correct"] / s["count"] * 100, 1) if s["count"] > 0 else 0}
        for d, s in dir_stats.items()
    }

    # 置信度校准
    print(f"\n{'─'*60}")
    print(f"📊 置信度校准")
    print(f"{'─'*60}")
    bins = [(0, 50, "0-50%"), (50, 65, "50-65%"), (65, 80, "65-80%"), (80, 101, "80-100%")]
    for lo, hi, label in bins:
        in_bin = []
        for rec in evaluated:
            conf = rec.get("direction_confidence", 0) or 0
            ev = (rec.get("evaluation") or {}).get("1") or {}
            if lo <= conf < hi and not ev.get("error"):
                in_bin.append(ev.get("direction_correct", False))
        acc = round(sum(in_bin) / len(in_bin) * 100, 1) if in_bin else None
        bar = "█" * int((acc or 0) / 5)
        print(f"  {label:<10s} {len(in_bin):>4} 次  实际准确率 {acc if acc is not None else 'N/A':>6}% {bar}")

    # 保存
    BENCHMARK_JSON.write_text(
        json.dumps(benchmark, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n📁 完整报告: {BENCHMARK_JSON}")
    print(f"📁 预测明细: {FORECASTS_JSONL}")

    # 同时保存带评估的预测
    eval_jsonl = BACKTEST_DIR / "forecasts_evaluated.jsonl"
    with eval_jsonl.open("w", encoding="utf-8") as f:
        for rec in evaluated:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(f"📁 评估明细: {eval_jsonl}")


if __name__ == "__main__":
    main()
