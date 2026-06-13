"""LLM 选股失败模式诊断与回测（Top 池 / llm_top300）。"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import desc, select

from zplan_shared.features import suggested_price_levels
from zplan_shared.market import get_bars, latest_trade_date
from zplan_shared.models import (
    PickEntry,
    PickLlmEvaluation,
    PickRun,
    SessionLocal,
    init_db,
)
from zplan_shared.pick_predictions import backfill_entry_predictions

FAIL_TAG_LABELS: dict[str, str] = {
    "momentum_chase": "20日涨幅过高仍强推（追高风险）",
    "near_60d_high": "接近60日阶段高点",
    "score_inflation": "LLM 分显著高于规则分且理由空洞",
    "generic_bullish": "趋势描述为套话，未引用具体信号",
    "buy_unreachable": "收盘价远高于建议买价，短期难回踩成交",
    "forward_loss": "验证期内收盘收益为负",
    "forward_flat": "验证期内收益接近 0，推荐未兑现",
    "no_forward_data": "尚无足够后续 K 线",
    "over_recommendation": "推荐档位偏积极（推荐/积极关注）但存在多项风险",
    # 港股专属标签
    "penny_stock": "仙股（价格过低，流动性风险）",
    "low_liquidity": "成交额/换手率过低，流动性不足",
}


# 港股动量阈值（无涨跌停，20日涨幅波动更大）
_HK_MOMENTUM_RET20_THRESHOLD = 15.0
_HK_BUY_GAP_FAIL_PCT = 5.0
_HK_NEAR_HIGH_THRESHOLD = 95.0


def _market_thresholds(market: str) -> dict[str, float]:
    """按市场返回诊断阈值。"""
    if market == "hk":
        return {
            "momentum_ret20_threshold": _HK_MOMENTUM_RET20_THRESHOLD,
            "buy_gap_fail_pct": _HK_BUY_GAP_FAIL_PCT,
            "near_high_threshold": _HK_NEAR_HIGH_THRESHOLD,
        }
    return {
        "momentum_ret20_threshold": 8.0,
        "buy_gap_fail_pct": 3.0,
        "near_high_threshold": 90.0,
    }

GENERIC_BULLISH = (
    "均线多头排列",
    "技术形态强劲",
    "多指标金叉",
    "技术分满分",
    "上升趋势",
    "动能充足",
    "符合规则",
    "符合多项",
)

BULLISH_RECS = frozenset({"推荐", "积极关注", "强烈关注", "建议关注", "买入"})


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _forward_return(bars: pd.DataFrame, as_of: date, horizon: int) -> tuple[float | None, str]:
    idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
    bars = bars.copy()
    bars.index = idx
    as_ts = pd.Timestamp(as_of)
    on = bars[bars.index <= as_ts]
    after = bars[bars.index > as_ts]
    if on.empty:
        return None, "no_bars"
    close0 = float(on["close"].iloc[-1])
    if after.empty:
        return None, "pending"
    chunk = after.head(horizon)
    ret = round((float(chunk["close"].iloc[-1]) - close0) / close0 * 100, 4)
    status = "complete" if len(chunk) >= horizon else "partial"
    return ret, status


def diagnose_entry(
    entry: PickEntry,
    run: PickRun,
    *,
    horizon_days: int = 5,
    momentum_ret20_threshold: float | None = None,
    buy_gap_fail_pct: float | None = None,
    near_high_threshold: float | None = None,
    score_inflation_delta: float = 5.0,
) -> dict[str, Any]:
    """单条 LLM pick 的结构化诊断。阈值未指定时按市场自适应。"""
    market = getattr(entry, "market", None) or getattr(run, "market", None) or "a"
    mk_thresh = _market_thresholds(market)
    if momentum_ret20_threshold is None:
        momentum_ret20_threshold = mk_thresh["momentum_ret20_threshold"]
    if buy_gap_fail_pct is None:
        buy_gap_fail_pct = mk_thresh["buy_gap_fail_pct"]
    if near_high_threshold is None:
        near_high_threshold = mk_thresh["near_high_threshold"]

    proc = _loads(entry.analysis_process_json) or {}
    brief = proc.get("llm_brief") or {}
    ret_20d = proc.get("ret_20d")
    high_60d_pct = proc.get("high_60d_pct")
    trend = (brief.get("trend") or "").strip()
    rec = entry.recommendation or brief.get("recommendation")
    llm_s = entry.llm_composite_score
    rule_s = entry.rule_composite_score
    delta = round(float(llm_s) - float(rule_s), 2) if llm_s is not None and rule_s else None

    as_of = run.trade_date_as_of or (
        entry.created_at_utc.date() if entry.created_at_utc else None
    )
    tags: list[str] = []

    if ret_20d is not None and float(ret_20d) >= momentum_ret20_threshold:
        tags.append("momentum_chase")
    if high_60d_pct is not None and float(high_60d_pct) >= near_high_threshold:
        tags.append("near_60d_high")
    if delta is not None and delta >= score_inflation_delta:
        generic = trend and any(p in trend for p in GENERIC_BULLISH)
        if generic or not brief.get("vs_rule_engine"):
            tags.append("score_inflation")
    if trend and sum(1 for p in GENERIC_BULLISH if p in trend) >= 2:
        tags.append("generic_bullish")

    close = entry.close_price
    buy = entry.predicted_buy_price
    gap = None
    if as_of and entry.ts_code:
        bars = get_bars(entry.ts_code, market=market)
        if not bars.empty:
            idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
            bars.index = idx
            on = bars[bars.index <= pd.Timestamp(as_of)]
            if not on.empty:
                bar_close = float(on["close"].iloc[-1])
                # 若 buy 来自规则引擎（非 LLM 设定），随 close 一起重算以保证一致性
                price_src = entry.price_source or "rule"
                if price_src == "rule" or buy is None:
                    levels = suggested_price_levels(on)
                    close = bar_close
                    buy = levels.get("suggested_buy", buy)
                elif bar_close and buy:
                    # LLM 设定的 buy_price：用 bar_close 做 gap 校验，但不覆盖 buy
                    close = bar_close
    if close and buy:
        gap = round((close - buy) / buy * 100, 4)
        if gap > buy_gap_fail_pct:
            tags.append("buy_unreachable")

    fwd_ret, fwd_status = None, "no_as_of"
    if as_of and entry.ts_code:
        bars = get_bars(entry.ts_code, market=market)
        if not bars.empty:
            fwd_ret, fwd_status = _forward_return(bars, as_of, horizon_days)
    if fwd_status == "pending":
        tags.append("no_forward_data")
    elif fwd_ret is not None:
        if fwd_ret < -0.5:
            tags.append("forward_loss")
        elif abs(fwd_ret) < 0.3:
            tags.append("forward_flat")

    # 最新行情（当前状态）
    latest_close = None
    latest_date = None
    latest_pct_chg = None
    if as_of and entry.ts_code:
        bars2 = get_bars(entry.ts_code, market=market)
        if not bars2.empty:
            idx2 = pd.DatetimeIndex(pd.to_datetime(bars2.index)).normalize()
            bars2.index = idx2
            after_as_of = bars2[bars2.index >= pd.Timestamp(as_of)]
            if not after_as_of.empty:
                last = after_as_of.iloc[-1]
                latest_close = float(last["close"])
                latest_date = str(last.name.date()) if hasattr(last.name, 'date') else str(last.name)[:10]
                latest_pct_chg = float(last["pct_chg"]) if "pct_chg" in after_as_of.columns and pd.notna(last.get("pct_chg")) else None

    if rec in BULLISH_RECS and len([t for t in tags if t not in ("no_forward_data",)]) >= 2:
        tags.append("over_recommendation")

    # near_60d_high 不单独判 fail：高位不是原罪，需配合其他风险标签
    # 仅 momentum_chase / score_inflation / buy_unreachable / generic_bullish / over_recommendation 可独立判 fail
    pick_time_fail = any(
        t in tags
        for t in (
            "momentum_chase",
            "score_inflation",
            "buy_unreachable",
            "generic_bullish",
            "over_recommendation",
        )
    )
    forward_fail = "forward_loss" in tags

    if fwd_status == "pending":
        verdict = "fail" if pick_time_fail else "pending"
    elif forward_fail or (pick_time_fail and len(tags) >= 2):
        verdict = "fail"
    elif fwd_ret is not None and fwd_ret > 0 and not pick_time_fail:
        verdict = "pass"
    elif pick_time_fail:
        verdict = "fail"
    else:
        verdict = "inconclusive"

    return {
        "entry_id": entry.id,
        "run_id": run.id,
        "rank": entry.rank_in_run,
        "ts_code": entry.ts_code,
        "name": entry.name,
        "as_of_date": str(as_of) if as_of else None,
        "horizon_days": horizon_days,
        "verdict": verdict,
        "llm_score": llm_s,
        "rule_score": rule_s,
        "score_delta": delta,
        "ret_20d_at_pick": ret_20d,
        "high_60d_pct": high_60d_pct,
        "close_vs_buy_gap_pct": gap,
        "return_from_close_pct": fwd_ret,
        "forward_status": fwd_status,
        "failure_tags": tags,
        "failure_labels": {t: FAIL_TAG_LABELS.get(t, t) for t in tags},
        "llm_trend": trend,
        "recommendation": rec,
        "predicted_buy": buy,
        "close_at_pick": close,
        "latest_close": latest_close,
        "latest_date": latest_date,
        "latest_pct_chg": latest_pct_chg,
    }


def upsert_llm_eval(session: Any, payload: dict[str, Any]) -> PickLlmEvaluation:
    entry_id = int(payload["entry_id"])
    existing = session.execute(
        select(PickLlmEvaluation).where(PickLlmEvaluation.entry_id == entry_id)
    ).scalar_one_or_none()
    fields = {
        "run_id": int(payload["run_id"]),
        "rank_in_run": payload.get("rank"),
        "ts_code": str(payload["ts_code"]),
        "as_of_date": date.fromisoformat(str(payload["as_of_date"])[:10])
        if payload.get("as_of_date")
        else None,
        "horizon_days": int(payload.get("horizon_days") or 5),
        "verdict": str(payload.get("verdict") or "pending"),
        "llm_score": payload.get("llm_score"),
        "rule_score": payload.get("rule_score"),
        "score_delta": payload.get("score_delta"),
        "ret_20d_at_pick": payload.get("ret_20d_at_pick"),
        "close_vs_buy_gap_pct": payload.get("close_vs_buy_gap_pct"),
        "return_from_close_pct": payload.get("return_from_close_pct"),
        "failure_tags_json": _dumps(payload.get("failure_tags") or []),
        "llm_trend": payload.get("llm_trend"),
        "recommendation": payload.get("recommendation"),
        "evaluated_at_utc": datetime.utcnow(),
    }
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        row = existing
    else:
        row = PickLlmEvaluation(entry_id=entry_id, **fields)
        session.add(row)
    return row


def evaluate_llm_run(
    *,
    run_id: int | None = None,
    top_n: int = 10,
    horizon_days: int = 5,
    run_kind: str = "llm_top300",
) -> dict[str, Any]:
    """评估某次 LLM Top 运行（默认 Top10）。"""
    init_db()
    with SessionLocal() as session:
        if run_id is None:
            # 优先选已到目标交易日的 run（有行情数据可验证）
            today = latest_trade_date()
            if today:
                run = session.execute(
                    select(PickRun)
                    .where(
                        PickRun.run_kind == run_kind,
                        PickRun.llm_enabled.is_(True),
                        PickRun.trade_date.isnot(None),
                        PickRun.trade_date <= today,
                    )
                    .order_by(PickRun.trade_date.desc(), desc(PickRun.id))
                    .limit(1)
                ).scalar_one_or_none()
            else:
                run = None
            # 兜底：无 forward 数据时取 trade_date 最新 run，再不行取 id 最新
            if not run:
                run = session.execute(
                    select(PickRun)
                    .where(PickRun.run_kind == run_kind, PickRun.llm_enabled.is_(True))
                    .order_by(desc(PickRun.trade_date), desc(PickRun.id))
                    .limit(1)
                ).scalar_one_or_none()
            if not run:
                return {"ok": False, "message": f"无 {run_kind} 且 llm_enabled 的运行"}
            run_id = run.id
        else:
            run = session.get(PickRun, run_id)
            if not run:
                return {"ok": False, "message": f"run_id={run_id} 不存在"}

        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run_id)
            .order_by(PickEntry.rank_in_run, PickEntry.id)
            .limit(top_n)
        ).scalars().all()

        rows: list[dict[str, Any]] = []
        for entry in entries:
            backfill_entry_predictions(entry, session=session)
            diag = diagnose_entry(entry, run, horizon_days=horizon_days)
            upsert_llm_eval(session, diag)
            rows.append(diag)
        run_kind = run.run_kind
        trade_date_as_of = str(run.trade_date_as_of) if run.trade_date_as_of else None
        session.commit()

    tag_counts: dict[str, int] = {}
    for r in rows:
        for t in r.get("failure_tags") or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    fails = [r for r in rows if r.get("verdict") == "fail"]
    passes = [r for r in rows if r.get("verdict") == "pass"]
    pending = [r for r in rows if r.get("verdict") == "pending"]

    return {
        "ok": True,
        "run_id": run_id,
        "run_kind": run_kind,
        "trade_date_as_of": trade_date_as_of,
        "top_n": top_n,
        "horizon_days": horizon_days,
        "summary": {
            "total": len(rows),
            "fail": len(fails),
            "pass": len(passes),
            "pending": len(pending),
            "fail_rate": round(len(fails) / len(rows), 4) if rows else None,
        },
        "tag_counts": tag_counts,
        "entries": rows,
        "optimization": build_optimization_map(tag_counts, rows),
    }


def build_optimization_map(
    tag_counts: dict[str, int],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """根据失败标签汇总 → 该改 prompt / strategy / 规则引擎。"""
    prompt: list[str] = []
    strategy: list[str] = []
    rule_engine: list[str] = []

    n = len(rows) or 1
    if tag_counts.get("momentum_chase", 0) >= max(2, n * 0.3):
        prompt.append(
            "简评 prompt：必须引用 ret_20d；若 ret_20d>8% 须写追高风险，"
            "composite_score 不得高于规则分+3，recommendation 最高「观望」"
        )
        strategy.append("strategy.yaml filters.max_ret_20d: 12（规则层直接剔除过热标的）")
        rule_engine.append("scanner/llm_top300：ret_20d>8% 时 composite 上限 75")

    if tag_counts.get("score_inflation", 0) >= max(2, n * 0.3):
        prompt.append(
            "简评 prompt：composite_score 默认=规则分；仅当 signals 含具体突破/放量时最多+5；"
            "vs_rule_engine 须说明加分/减分理由，禁止「符合规则引擎高分」"
        )

    if tag_counts.get("generic_bullish", 0) >= max(2, n * 0.3):
        prompt.append(
            "简评 prompt：trend_one_liner 必须包含至少 1 个输入 JSON 中的具体字段"
            "（如 ret_20d 数值、KDJ、signals 原文），禁止「均线多头排列」等套话"
        )

    if tag_counts.get("buy_unreachable", 0) >= max(2, n * 0.3):
        strategy.append(
            "technical.suggested_price_levels：MA20 折扣由 0.98 调至 0.96，或增加「可成交价」字段"
        )
        prompt.append(
            "深度研报 prompt：buy_price 不得高于 close*0.99；须说明与 suggested_buy 关系"
        )

    if tag_counts.get("near_60d_high", 0) >= max(2, n * 0.3):
        prompt.append("简评 prompt：high_60d_pct>0.9 时须提示「接近阶段高点」，降分或观望")
        rule_engine.append("technical：high_60d_pct>0.90 时 rule_score -= 8")

    if tag_counts.get("over_recommendation", 0) >= max(2, n * 0.3):
        prompt.append(
            "简评 prompt：recommendation 与风险挂钩——存在 momentum_chase 或 buy_unreachable 时"
            "不得输出「推荐/积极关注」"
        )

    avg_delta = pd.Series([r.get("score_delta") for r in rows if r.get("score_delta") is not None])
    if not avg_delta.empty and float(avg_delta.mean()) > 6:
        prompt.append(
            f"当前 LLM 平均分比规则高 {avg_delta.mean():.1f} 分，prompt 加硬约束："
            "「你的 composite_score 中位数应接近规则分，勿集体抬到 90+」"
        )

    return {
        "where_to_change": {
            "prompt": prompt,
            "strategy_yaml": strategy,
            "rule_engine_code": rule_engine,
        },
        "priority": sorted(tag_counts.items(), key=lambda x: -x[1])[:5],
        "review_actions": _build_review_actions(tag_counts, rows, prompt, strategy, rule_engine),
    }


def _build_review_actions(
    tag_counts: dict[str, int],
    rows: list[dict[str, Any]],
    prompt: list[str],
    strategy: list[str],
    rule_engine: list[str],
) -> list[dict[str, str]]:
    """供人工 review 的结构化修改清单（含文件路径）。"""
    actions: list[dict[str, str]] = []
    idx = 1

    def add(layer: str, file: str, action: str, why: str) -> None:
        nonlocal idx
        actions.append(
            {"id": str(idx), "layer": layer, "file": file, "action": action, "why": why}
        )
        idx += 1

    if tag_counts.get("score_inflation") or tag_counts.get("generic_bullish"):
        add(
            "prompt",
            "zplan-选股/src/pick_agent/llm_research.py → _LLM_BRIEF_RULES",
            "收紧简评：默认分=规则分，禁止套话，有追高风险必须降 recommendation",
            f"score_inflation={tag_counts.get('score_inflation', 0)} generic={tag_counts.get('generic_bullish', 0)}",
        )
    if tag_counts.get("momentum_chase"):
        add(
            "strategy",
            "zplan-选股/config/strategy.yaml → filters.max_ret_20d",
            "启用或调低 max_ret_20d（如 10），减少过热股进入 Top300",
            f"momentum_chase={tag_counts.get('momentum_chase', 0)}",
        )
    if tag_counts.get("buy_unreachable"):
        add(
            "strategy",
            "zplan-选股/config/strategy.yaml → ranking",
            "试 ranking.mode=blend 且 llm_weight 0.8；或 prompt 要求 close_vs_buy_gap 大时降分",
            f"buy_unreachable={tag_counts.get('buy_unreachable', 0)}",
        )
        add(
            "rule_engine",
            "zplan-共享/src/zplan_shared/features.py → suggested_price_levels",
            "MA20 折扣 0.98→0.96，或增加 actionable_price≈close 供 LLM 参考",
            "建议买价系统性低于现价，导致「买不到」",
        )
    if tag_counts.get("over_recommendation"):
        add(
            "prompt",
            "zplan-选股/src/pick_agent/llm_research.py",
            "recommendation 与 risk 挂钩：有 buy_unreachable/momentum 时最高「观望」",
            f"over_recommendation={tag_counts.get('over_recommendation', 0)}",
        )

    add(
        "strategy",
        "zplan-选股/config/strategy.yaml → ranking",
        "确认 ranking.mode=llm_primary、resort_after_llm=true；回测后微调 llm_weight",
        "最终 Top 应由 LLM 分排序，规则只做候选池与约束",
    )
    add(
        "workflow",
        "重跑 llm-top → llm-eval",
        "改完配置/prompt 后必须重新 llm-top 生成新 run，再 llm-eval 对比 fail_rate",
        "旧 run 不会自动应用新 prompt",
    )

    return actions


def format_llm_eval_report(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return result.get("message", "评估失败")

    s = result["summary"]
    lines = [
        f"# LLM Top{result['top_n']} 回测诊断（run_id={result['run_id']}，"
        f"as_of={result.get('trade_date_as_of')}）",
        "",
        f"- 样本：**{s['total']}** | 失败 **{s['fail']}** | 通过 **{s['pass']}** | 待验证 **{s['pending']}**",
    ]
    if s.get("fail_rate") is not None:
        lines.append(f"- 失败率：**{s['fail_rate']:.0%}**")

    lines.extend(["", "## 💰 收益概览"])
    entries = result.get("entries") or []
    fwd_rets = [e.get("return_from_close_pct") for e in entries if e.get("return_from_close_pct") is not None]
    if fwd_rets:
        win_n = sum(1 for r in fwd_rets if r > 0)
        loss_n = sum(1 for r in fwd_rets if r < -0.5)
        avg_ret = sum(fwd_rets) / len(fwd_rets)
        lines.append(f"- {len(fwd_rets)} 只有数据：🟢 盈利 **{win_n}** | 🔴 亏损 **{loss_n}** | 均收益 **{avg_ret:+.2f}%**")
        lines.append(f"- 胜率 **{win_n/len(fwd_rets)*100:.0f}%**（收益>0 即算赢）")
    pending = [e for e in entries if e.get("return_from_close_pct") is None]
    if pending:
        lines.append(f"- ⏳ **{len(pending)}** 只尚无 forward 数据")

    lines.extend(["", "## 失败标签统计"])
    for tag, cnt in sorted((result.get("tag_counts") or {}).items(), key=lambda x: -x[1]):
        lines.append(f"- `{tag}`（{FAIL_TAG_LABELS.get(tag, tag)}）：**{cnt}** 次")

    # 按 forward 收益排序
    sorted_entries = sorted(entries, key=lambda e: -(e.get("return_from_close_pct") or -999))

    lines.extend(["", "## 逐只明细（按收益排序）"])
    for r in sorted_entries:
        tags = ", ".join(r.get("failure_tags") or []) or "—"
        close = r.get("close_at_pick")
        buy = r.get("predicted_buy")
        high60 = r.get("high_60d_pct")
        fwd = r.get("return_from_close_pct")
        latest_close = r.get("latest_close")
        latest_pct = r.get("latest_pct_chg")

        close_s = f"¥{close:.2f}" if close is not None else "?"
        buy_s = f"¥{buy:.2f}" if buy is not None else "?"
        high60_s = f"{float(high60):.1f}%" if high60 is not None else "?"

        # 买入折扣
        if close is not None and buy is not None and buy != 0:
            discount = (close - buy) / buy * 100
            discount_s = f"（折{discount:.1f}%）"
        else:
            discount_s = ""

        # 收益图标
        if fwd is not None:
            icon = "🟢" if fwd > 0 else ("🔴" if fwd < -0.5 else "⚪")
            fwd_s = f"{icon} {fwd:+.2f}%"
        else:
            fwd_s = "⏳ 待验"

        lines.append(
            f"- **#{r.get('rank')} {r.get('name')} ({r.get('ts_code')})** "
            f"{fwd_s} | 现价{close_s} 建议买{buy_s}{discount_s}"
        )
        lines.append(
            f"  - LLM{r.get('llm_score')}/规则{r.get('rule_score')} "
            f"| ret20={r.get('ret_20d_at_pick')}% | 60日高位={high60_s} "
            f"| gap={r.get('close_vs_buy_gap_pct')}%"
        )
        if latest_close is not None:
            latest_pct_s = f"（{latest_pct:+.1f}%）" if latest_pct is not None else ""
            lines.append(f"  - 最新价 ¥{latest_close:.2f}{latest_pct_s} · 日期 {r.get('latest_date', '?')}")
        lines.append(f"  - 推荐：{r.get('recommendation')} | 标签：{tags}")
        if r.get("llm_trend"):
            lines.append(f"  - LLM：{r.get('llm_trend')[:80]}")

    opt = result.get("optimization") or {}
    wc = opt.get("where_to_change") or {}
    if any(wc.values()):
        lines.extend(["", "## 后续优化（改哪里）"])
        if wc.get("prompt"):
            lines.append("### 1. Prompt（`llm_research.py` 简评/深度）— **优先**")
            for x in wc["prompt"]:
                lines.append(f"- {x}")
        if wc.get("strategy_yaml"):
            lines.append("### 2. strategy.yaml（规则过滤，不耗 API）")
            for x in wc["strategy_yaml"]:
                lines.append(f"- {x}")
        if wc.get("rule_engine_code"):
            lines.append("### 3. 规则引擎代码（`technical.py` / `scanner.py`）")
            for x in wc["rule_engine_code"]:
                lines.append(f"- {x}")

    actions = opt.get("review_actions") or []
    if actions:
        lines.extend(["", "## 请你 Review（可勾选后改配置）", ""])
        lines.append("| # | 层级 | 文件 | 建议动作 | 原因 |")
        lines.append("|---|------|------|----------|------|")
        for a in actions:
            lines.append(
                f"| {a.get('id')} | {a.get('layer')} | `{a.get('file')}` | {a.get('action')} | {a.get('why')} |"
            )

    lines.extend(
        [
            "",
            "## 设计说明（规则 vs LLM）",
            "",
            "- **init-rule**：全市场轻量技术分（`quick_technical_score`），偏动量/多头排列，**不含**财报/资讯。",
            "- **llm-top deepen**：对 Top300 才算完整规则分（技术+财务+资讯+行业）。",
            "- **你的目标**：规则只做「候选池 + 硬约束」，**排序与推荐以 LLM 为主**（`strategy.yaml` → `ranking`）。",
            "- **校正闭环**：改 prompt/权重 → 重跑 llm-top → `llm-eval` → 看 fail_rate 与上表。",
        ]
    )

    return "\n".join(lines)
