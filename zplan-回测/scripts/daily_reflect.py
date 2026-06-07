#!/usr/bin/env python3
"""
每日反思报告：选股 Top10 + 回测结果 + 优化思路 → 企微推送。

用法::

    # 仅生成并推送今日报告（默认）
    .venv/bin/python scripts/daily_reflect.py

    # 跳过选股 pipeline（假设已通过 cron 跑过）
    .venv/bin/python scripts/daily_reflect.py --skip-pick

    # 仅输出到终端，不推送企微
    .venv/bin/python scripts/daily_reflect.py --no-push

    # 指定回测 horizon
    .venv/bin/python scripts/daily_reflect.py --horizon 10

流程：
    1. 取今日 llm_top300 run（若无则触发 pipeline）
    2. 取今日 Top10 选股结果
    3. 找 3~7 天前有 forward 数据的旧 run → evaluate_llm_run
    4. 生成优化映射 + 迭代对比
    5. 拼 Markdown 报告 → 企微推送
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_BACKTEST = _HERE.parent
_MONO = _BACKTEST.parent
_SHARED_SRC = _MONO / "zplan-共享" / "src"
_NEWS = _MONO / "zplan-资讯"
_PICK = _MONO / "zplan-选股"

for _p in (str(_SHARED_SRC), str(_NEWS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("ZPLAN_ROOT", str(_NEWS))

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.models import (
    PickEntry,
    PickLlmEvaluation,
    PickRun,
    SessionLocal,
    init_db,
)
from zplan_shared.pick_llm_eval import (
    evaluate_llm_run,
    FAIL_TAG_LABELS,
    build_optimization_map,
)
from zplan_shared.pick_iterate_store import (
    append_iteration,
    compare_iterations,
    list_iterations,
)
from sqlalchemy import desc, select, func

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("daily_reflect")

# ── 常量 ──────────────────────────────────────────────
REFLECT_MIN_DAYS = 3      # 旧 run 至少距今几天才回测
REFLECT_MAX_DAYS = 7      # 旧 run 最多距今几天（太旧跳过）
DEFAULT_TOP_N = 10
DEFAULT_HORIZON = 5

FAIL_ICONS = {
    "momentum_chase": "🔥",
    "near_60d_high": "📈",
    "score_inflation": "🎈",
    "generic_bullish": "🦜",
    "buy_unreachable": "💸",
    "forward_loss": "📉",
    "forward_flat": "➡️",
    "over_recommendation": "📢",
}


# ── 选股 pipeline ─────────────────────────────────────
def _run_pick_pipeline(top: int = 300) -> bool:
    """触发 init-rule → llm-top，返回是否成功。"""
    py = _PICK / ".venv" / "bin" / "python"
    if not py.is_file():
        logger.error("缺少选股 venv: %s", py)
        return False

    env = os.environ.copy()
    env["ZPLAN_ROOT"] = str(_NEWS)

    logger.info("触发选股 pipeline (Top%s)...", top)
    proc = subprocess.run(
        [str(py), str(_PICK / "main.py"), "pipeline", "--top", str(top)],
        cwd=str(_PICK),
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if proc.returncode != 0:
        logger.error("pipeline 失败: %s", proc.stderr[-500:])
        return False
    return True


# ── 数据查询 ──────────────────────────────────────────
def _today() -> date:
    return date.today()


def _latest_llm_run(min_age_days: int = 0) -> PickRun | None:
    """最新的 llm_top300 run（可选最小天数：选距今≥N天的旧 run 来回测）。"""
    init_db()
    with SessionLocal() as session:
        q = (
            select(PickRun)
            .where(PickRun.run_kind == "llm_top300", PickRun.llm_enabled.is_(True))
            .order_by(desc(PickRun.id))
        )
        if min_age_days > 0:
            cutoff = _today() - timedelta(days=min_age_days)
            q = q.where(PickRun.trade_date_as_of <= cutoff)
        run = session.execute(q.limit(1)).scalar_one_or_none()
    return run


def _top_entries(run_id: int, top_n: int = DEFAULT_TOP_N) -> list[PickEntry]:
    init_db()
    with SessionLocal() as session:
        return list(
            session.execute(
                select(PickEntry)
                .where(PickEntry.run_id == run_id)
                .order_by(PickEntry.rank_in_run, PickEntry.id)
                .limit(top_n)
            ).scalars().all()
        )


def _run_has_evals(run_id: int) -> bool:
    init_db()
    with SessionLocal() as session:
        cnt = session.execute(
            select(func.count(PickLlmEvaluation.id)).where(
                PickLlmEvaluation.run_id == run_id
            )
        ).scalar_one()
    return cnt > 0


def _load_existing_eval(run_id: int, top_n: int = DEFAULT_TOP_N) -> dict[str, Any] | None:
    """加载已有评估数据，重组为与 evaluate_llm_run 相同的结构。"""
    import json

    init_db()
    with SessionLocal() as session:
        evals = session.execute(
            select(PickLlmEvaluation)
            .where(PickLlmEvaluation.run_id == run_id)
            .order_by(PickLlmEvaluation.rank_in_run)
            .limit(top_n)
        ).scalars().all()

        run = session.get(PickRun, run_id)
        if not evals or not run:
            return None

        # 补充股票名称
        name_map: dict[str, str] = {}
        for pe in session.execute(
            select(PickEntry.ts_code, PickEntry.name).where(PickEntry.run_id == run_id)
        ).all():
            name_map[pe[0]] = pe[1] or pe[0]

        entries = []
        tag_counts: dict[str, int] = {}
        for ev in evals:
            tags = json.loads(ev.failure_tags_json or "[]")
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
            entries.append({
                "entry_id": ev.entry_id,
                "run_id": ev.run_id,
                "rank": ev.rank_in_run,
                "ts_code": ev.ts_code,
                "name": name_map.get(ev.ts_code, ev.ts_code),
                "verdict": ev.verdict,
                "llm_score": ev.llm_score,
                "rule_score": ev.rule_score,
                "score_delta": ev.score_delta,
                "ret_20d_at_pick": ev.ret_20d_at_pick,
                "close_vs_buy_gap_pct": ev.close_vs_buy_gap_pct,
                "return_from_close_pct": ev.return_from_close_pct,
                "failure_tags": tags,
                "recommendation": ev.recommendation,
            })


        fails = [e for e in entries if e.get("verdict") == "fail"]
        passes = [e for e in entries if e.get("verdict") == "pass"]
        pending = [e for e in entries if e.get("verdict") == "pending"]

        return {
            "ok": True,
            "run_id": run_id,
            "run_kind": run.run_kind,
            "trade_date_as_of": str(run.trade_date_as_of) if run.trade_date_as_of else None,
            "top_n": len(entries),
            "horizon_days": evals[0].horizon_days if evals else 5,
            "summary": {
                "total": len(entries),
                "fail": len(fails),
                "pass": len(passes),
                "pending": len(pending),
                "fail_rate": round(len(fails) / len(entries), 4) if entries else None,
            },
            "tag_counts": tag_counts,
            "entries": entries,
            "optimization": build_optimization_map(tag_counts, entries),
        }


# ── 报告生成 ──────────────────────────────────────────
def _build_today_section(entries: list[PickEntry]) -> str:
    if not entries:
        return "> ⚠️ 今日尚无选股结果，请运行 pipeline"

    lines = ["**今日 Top10 选股**"]
    for e in entries:
        name = e.name or e.ts_code
        llm_s = f"{e.llm_composite_score:.0f}" if e.llm_composite_score else "-"
        rule_s = f"{e.rule_composite_score:.0f}" if e.rule_composite_score else "-"
        rec = e.recommendation or "-"
        buy = f"{e.predicted_buy_price:.2f}" if e.predicted_buy_price else "-"
        target = f"{e.predicted_target_price:.2f}" if e.predicted_target_price else "-"
        lines.append(
            f"{e.rank_in_run}. **{name}**({e.ts_code}) "
            f"LLM{llm_s}/规则{rule_s} | {rec} | "
            f"买{buy} 目标{target}"
        )
    return "\n".join(lines)


def _build_backtest_section(eval_result: dict[str, Any]) -> str:
    if not eval_result.get("ok"):
        return ""

    summary = eval_result["summary"]
    tag_counts = eval_result.get("tag_counts") or {}
    entries = eval_result.get("entries") or []

    total = summary["total"]
    fail_n = summary["fail"]
    pass_n = summary["pass"]
    fail_rate = summary.get("fail_rate") or 0

    lines = [
        f"**回测诊断** (run={eval_result['run_id']}, as_of={eval_result.get('trade_date_as_of')})",
        f"失败率 **{fail_rate:.0%}** ({fail_n}/{total}) | 通过 {pass_n} | "
        f"fwd 均值 {_mean_fwd(entries):+.1f}% | LLMΔ {_mean_delta(entries):+.1f}",
    ]

    # 标签
    if tag_counts:
        tags_str = " ".join(
            f"{FAIL_ICONS.get(t,'▪️')}`{t}`×{c}"
            for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])[:5]
        )
        lines.append(f"标签: {tags_str}")

    # 逐只（失败 + 通过各取部分）
    fails = [e for e in entries if e.get("verdict") == "fail"]
    passes = [e for e in entries if e.get("verdict") == "pass"]
    if fails:
        lines.append("")
        lines.append("❌ 失败:")
        for f_ in fails[:5]:
            tags = ",".join(f_.get("failure_tags") or [])[:50]
            fwd = f_.get("return_from_close_pct")
            fwd_s = f"{fwd:+.1f}%" if fwd is not None else "?"
            lines.append(
                f"> #{f_.get('rank')} {f_.get('name','?')}({f_.get('ts_code')}) "
                f"fwd{fwd_s} | {tags}"
            )
    if passes:
        lines.append("")
        lines.append("✅ 通过:")
        for p_ in passes[:3]:
            fwd = p_.get("return_from_close_pct")
            fwd_s = f"{fwd:+.1f}%" if fwd is not None else "?"
            lines.append(f"> #{p_.get('rank')} {p_.get('name','?')}({p_.get('ts_code')}) fwd{fwd_s}")

    # 优化建议
    opt = eval_result.get("optimization") or {}
    actions = opt.get("review_actions") or []
    if actions:
        lines.append("")
        lines.append("**优化建议**")
        for a in actions[:3]:
            lines.append(f"> {a.get('layer','')}: {a.get('action','')[:80]}")

    return "\n".join(lines)


def _build_reflection_section(actions: list[dict], prev_metrics: dict | None) -> str:
    lines = ["**反思思路**"]

    if prev_metrics:
        fr = prev_metrics.get("fail_rate") or 0
        delta = prev_metrics.get("mean_delta") or 0
        lines.append(f"上轮 fail_rate={fr:.0%} LLMΔ={delta:+.1f}")

    if not actions:
        lines.append("> 暂无明显优化方向，继续观察")
        return "\n".join(lines)

    for a in actions[:3]:
        icon = {"prompt": "💬", "strategy": "⚙️", "rule_engine": "🔧", "workflow": "🔄"}.get(
            a.get("layer", ""), "▪️"
        )
        lines.append(f"> {icon} {a.get('action','')[:100]}")

    return "\n".join(lines)


def _build_iteration_curve() -> str:
    rows = list_iterations(limit=5)
    if len(rows) < 2:
        return ""
    curve = []
    for r in reversed(rows):
        m = r.get("metrics") or {}
        fr = m.get("fail_rate")
        rid = r.get("pick_run_id", "?")
        if fr is not None:
            curve.append(f"run{rid}:{fr:.0%}")
    return " → ".join(curve)


def _mean_fwd(entries: list[dict]) -> float:
    vals = [e.get("return_from_close_pct") for e in entries if e.get("return_from_close_pct") is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _mean_delta(entries: list[dict]) -> float:
    vals = [e.get("score_delta") for e in entries if e.get("score_delta") is not None]
    return sum(vals) / len(vals) if vals else 0.0


def build_report(
    today_entries: list[PickEntry],
    today_run: PickRun | None,
    backtest_result: dict[str, Any] | None,
    reflection_actions: list[dict],
    prev_metrics: dict | None,
) -> str:
    today_str = (_today() - timedelta(days=0)).isoformat()
    rule_ver = today_run.rule_version if today_run else "?"
    curve = _build_iteration_curve()

    parts = [
        f"# 📊 Z-Plan 每日选股 {today_str}",
        f"规则: `{rule_ver}`",
        "",
        _build_today_section(today_entries),
    ]

    if backtest_result:
        parts.append("")
        parts.append(_build_backtest_section(backtest_result))
        parts.append("")
        parts.append(_build_reflection_section(reflection_actions, prev_metrics))

    if curve:
        parts.append("")
        parts.append(f"**迭代曲线**: {curve}")

    return "\n".join(parts)


# ── 企微推送 ──────────────────────────────────────────
def _push_report(markdown: str) -> bool:
    try:
        from wechat_push import push_wechat_markdown

        return push_wechat_markdown(markdown)
    except ImportError:
        from wechat_push import push_wechat_text

        # 降级：markdown → 纯文本
        text = markdown.replace("*", "").replace("#", "").replace(">", "  ")
        return push_wechat_text(text)
    except Exception as exc:
        logger.warning("企微推送失败: %s", exc)
        return False


# ── 主流程 ────────────────────────────────────────────
def reflect(
    *,
    skip_pick: bool = False,
    push: bool = True,
    top_n: int = DEFAULT_TOP_N,
    horizon: int = DEFAULT_HORIZON,
) -> dict[str, Any]:
    """执行每日反思并生成报告。"""
    init_db()
    today = _today()

    # ── Step 1: 确保今日选股 ──
    today_run = _latest_llm_run(min_age_days=0)
    need_pick = today_run is None or (
        today_run.trade_date_as_of and today_run.trade_date_as_of < today
    )

    if need_pick and not skip_pick:
        logger.info("今日 (%s) 尚无 llm_top300，触发 pipeline...", today)
        ok = _run_pick_pipeline(top=300)
        if ok:
            today_run = _latest_llm_run(min_age_days=0)
        else:
            logger.error("pipeline 失败")
    elif need_pick:
        logger.info("今日尚无选股，且 --skip-pick，使用最新可用 run")

    today_entries = _top_entries(today_run.id, top_n) if today_run else []

    # ── Step 2: 找旧 run 做回测 ──
    backtest_result = None
    reflection_actions: list[dict] = []
    prev_metrics = None

    # 优先找距今 3-7 天、与今日不同的 run
    old_run = _latest_llm_run(min_age_days=REFLECT_MIN_DAYS)
    if old_run and old_run.id == (today_run.id if today_run else 0):
        # 回退：找更早的 run（不要求 llm_enabled）
        with SessionLocal() as session:
            earlier = session.execute(
                select(PickRun)
                .where(
                    PickRun.run_kind == "llm_top300",
                    PickRun.id < (today_run.id if today_run else 999),
                    PickRun.trade_date_as_of <= today - timedelta(days=REFLECT_MIN_DAYS),
                )
                .order_by(desc(PickRun.id))
                .limit(1)
            ).scalar_one_or_none()
        if earlier:
            old_run = earlier
        # 否则 old_run 仍是 today_run（同一个），回测它也可以

    if old_run:
        age = (today - (old_run.trade_date_as_of or today)).days
        if age >= REFLECT_MIN_DAYS:
            if not _run_has_evals(old_run.id):
                logger.info("回测旧 run %s (as_of=%s, 距今 %s 天)...", old_run.id, old_run.trade_date_as_of, age)
                result = evaluate_llm_run(run_id=old_run.id, top_n=top_n, horizon_days=horizon)
                if result.get("ok"):
                    backtest_result = result
            else:
                # 已评估过：直接加载已有评估数据
                logger.info("run %s 已有评估，加载历史数据...", old_run.id)
                backtest_result = _load_existing_eval(old_run.id, top_n)

            if backtest_result:
                opt = backtest_result.get("optimization") or {}
                reflection_actions = opt.get("review_actions") or []

                # 持久化迭代记录
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                s = backtest_result.get("summary") or {}
                entries = backtest_result.get("entries") or []
                record = {
                    "iteration_id": ts,
                    "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "phase": "auto_reflect",
                    "pick_run_id": old_run.id,
                    "rule_version": old_run.rule_version or "",
                    "horizon_days": horizon,
                    "top_n": top_n,
                    "metrics": {
                        "pick_run_id": old_run.id,
                        "fail_rate": s.get("fail_rate"),
                        "fail_count": s.get("fail"),
                        "pass_count": s.get("pass"),
                        "pending_count": s.get("pending"),
                        "mean_delta": _mean_delta(entries),
                        "mean_fwd_return": _mean_fwd(entries),
                        "tag_counts": backtest_result.get("tag_counts") or {},
                    },
                    "review_actions": reflection_actions,
                    "report_path": "",
                }
                append_iteration(record)

                # 对比上一轮
                history = list_iterations(limit=2)
                if len(history) >= 2:
                    cmp = compare_iterations(history[1], history[0])
                    prev_metrics = record["metrics"]
                    logger.info("对比上一轮: %s", cmp.get("deltas"))

    # ── Step 3: 生成报告 ──
    report_md = build_report(
        today_entries=today_entries,
        today_run=today_run,
        backtest_result=backtest_result,
        reflection_actions=reflection_actions,
        prev_metrics=prev_metrics,
    )

    # ── Step 4: 推送 ──
    if push:
        ok = _push_report(report_md)
        logger.info("企微推送: %s", "✅" if ok else "❌")

    return {
        "today_run_id": today_run.id if today_run else None,
        "today_top_n": len(today_entries),
        "backtest_run_id": old_run.id if old_run else None,
        "backtest_ok": backtest_result is not None,
        "reflection_actions_count": len(reflection_actions),
        "report": report_md,
        "pushed": push,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="每日反思报告 — 选股 + 回测 + 优化思路",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--skip-pick", action="store_true", help="跳过选股 pipeline")
    parser.add_argument("--no-push", action="store_true", help="不推送到企微")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help=f"Top N (默认 {DEFAULT_TOP_N})")
    parser.add_argument(
        "--horizon", type=int, default=DEFAULT_HORIZON, help=f"回测 horizon 天数 (默认 {DEFAULT_HORIZON})"
    )
    parser.add_argument("--force-eval", type=int, default=None, help="强制评估指定 run_id")
    args = parser.parse_args()

    result = reflect(
        skip_pick=args.skip_pick,
        push=not args.no_push,
        top_n=args.top,
        horizon=args.horizon,
    )

    # 终端输出
    print(result["report"])
    print(f"\n--- 统计 ---")
    print(f"今日 run: {result['today_run_id']}")
    print(f"回测 run: {result['backtest_run_id']} ({'已评估' if result['backtest_ok'] else '跳过'})")
    print(f"反思建议: {result['reflection_actions_count']} 条")
    print(f"企微推送: {'✅' if result['pushed'] else '❌'}")


if __name__ == "__main__":
    main()
