#!/usr/bin/env python3
"""选股跨期对比追踪：榜单重叠率 + 打分漂移 + 趋势曲线。

追踪最近 N 期 TOP10 的质量变化，发现策略迭代是否在改善。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/pick_tracker.py
    cd zplan-资讯 && .venv/bin/python scripts/pick_tracker.py --dry-run
    cd zplan-资讯 && .venv/bin/python scripts/pick_tracker.py --periods 5

调度: 盘后管道完成后自动调用（run_full_pipeline.sh）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEWS_ROOT))

from sqlalchemy import desc, select, text

from zplan_shared.models import (
    PickEntry,
    PickLlmEvaluation,
    PickRun,
    SessionLocal,
    init_db,
)
from wechat_push import push_wechat_markdown

BEIJING_TZ = timezone(timedelta(hours=8))
WECHAT_SAFE_LIMIT = 3800


def _get_pick_runs(session, limit: int = 10) -> list[dict[str, Any]]:
    """获取最近的 pick runs（scan/llm_top300 类型，有 TOP10）。"""
    rows = session.execute(
        select(PickRun)
        .where(
            PickRun.run_kind.in_(["scan", "llm_top300"]),
            PickRun.llm_enabled.is_(True),
        )
        .order_by(desc(PickRun.trade_date_as_of), desc(PickRun.id))
        .limit(limit * 10)  # 宽取以覆盖同 as_of 多 run 的情况
    ).scalars().all()

    # 每个 as_of 只保留最佳 run（llm_top300 > scan，id 大的优先）
    seen_dates: set[str] = set()
    runs: list[dict[str, Any]] = []
    for r in rows:
        as_of = str(r.trade_date_as_of) if r.trade_date_as_of else ""
        if as_of in seen_dates:
            continue

        # 取 TOP10
        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == r.id)
            .order_by(PickEntry.rank_in_run, PickEntry.id)
            .limit(10)
        ).scalars().all()

        if len(entries) < 3:
            continue  # 不够 TOP 的跳过

        seen_dates.add(as_of)
        runs.append({
            "run_id": r.id,
            "run_kind": r.run_kind,
            "as_of": as_of,
            "rule_version": r.rule_version or "?",
            "entries": [
                {
                    "entry_id": e.id,
                    "ts_code": e.ts_code,
                    "name": e.name,
                    "rank": e.rank_in_run,
                    "rule_score": e.rule_composite_score,
                    "llm_score": e.llm_composite_score,
                }
                for e in entries
            ],
        })

        if len(runs) >= limit:
            break

    return runs


def _get_eval_summary(session, run_id: int) -> dict[str, Any] | None:
    """获取某 run 的评估摘要。"""
    evals = session.execute(
        select(PickLlmEvaluation)
        .where(PickLlmEvaluation.run_id == run_id)
        .order_by(PickLlmEvaluation.rank_in_run)
        .limit(10)
    ).scalars().all()

    if not evals:
        return None

    fails = [e for e in evals if e.verdict == "fail"]
    passes = [e for e in evals if e.verdict == "pass"]
    fwd_rets = [e.return_from_close_pct for e in evals if e.return_from_close_pct is not None]

    return {
        "total": len(evals),
        "fail": len(fails),
        "pass": len(passes),
        "fail_rate": len(fails) / len(evals) if evals else 0,
        "avg_fwd": round(sum(fwd_rets) / len(fwd_rets), 2) if fwd_rets else None,
        "avg_llm": round(sum(e.llm_score for e in evals if e.llm_score) / max(1, len([e for e in evals if e.llm_score])), 1),
        "avg_rule": round(sum(e.rule_score for e in evals if e.rule_score) / max(1, len([e for e in evals if e.rule_score])), 1),
    }


def _build_tracker_markdown(
    runs: list[dict[str, Any]],
    eval_summaries: dict[int, dict[str, Any]],
) -> str:
    """构建跨期对比追踪 Markdown。"""
    beijing_now = datetime.now(BEIJING_TZ)
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    lines = [
        f"## 📈 选股趋势追踪",
        f"> {beijing_now:%m-%d} {weekday} · 最近 {len(runs)} 期",
        "",
    ]

    # ── 关键指标走势 ──
    lines.append("### 关键指标走势")
    lines.append("")
    lines.append("| 期 | as_of | kind | 失败率 | 均Fwd | LLM均 | 规则均 |")
    lines.append("|---|------|------|--------|-------|-------|--------|")

    for i, r in enumerate(runs):
        rid = r["run_id"]
        ev = eval_summaries.get(rid) or {}
        fr = ev.get("fail_rate")
        fwd = ev.get("avg_fwd")
        llm = ev.get("avg_llm")
        rule = ev.get("avg_rule")

        fr_s = f"{fr:.0%}" if fr is not None else "—"
        fwd_s = f"{fwd:+.1f}%" if fwd is not None else "—"
        llm_s = f"{llm:.0f}" if (llm is not None and llm > 0) else "—"
        rule_s = f"{rule:.0f}" if (rule is not None and rule > 0) else "—"

        # 趋势箭头
        if i > 0:
            prev = eval_summaries.get(runs[i - 1]["run_id"]) or {}
            prev_fr = prev.get("fail_rate")
            if fr is not None and prev_fr is not None:
                if fr < prev_fr - 0.05:
                    fr_s += "🔻"
                elif fr > prev_fr + 0.05:
                    fr_s += "🔺"
            prev_fwd = prev.get("avg_fwd")
            if fwd is not None and prev_fwd is not None:
                if fwd > prev_fwd + 1:
                    fwd_s += "🔺"
                elif fwd < prev_fwd - 1:
                    fwd_s += "🔻"

        kind_short = "LLM" if r["run_kind"] == "llm_top300" else "scan"
        lines.append(
            f"| {i+1} | {r['as_of']} | {kind_short} | {fr_s} | {fwd_s} | {llm_s} | {rule_s} |"
        )

    lines.append("")

    # ── 榜单连续性 ──
    if len(runs) >= 2:
        lines.append("### 榜单连续性")
        lines.append("")
        curr = runs[0]
        prev = runs[1]
        curr_codes = {e["ts_code"] for e in curr["entries"]}
        prev_codes = {e["ts_code"] for e in prev["entries"]}
        overlap = curr_codes & prev_codes
        overlap_rate = len(overlap) / max(len(curr_codes), len(prev_codes)) * 100

        try:
            from datetime import date
            gap = (date.fromisoformat(curr["as_of"]) - date.fromisoformat(prev["as_of"])).days
        except (ValueError, TypeError):
            gap = 0

        lines.append(f"- {prev['as_of']} → {curr['as_of']}（间隔{gap}天）")
        lines.append(f"- TOP10 重叠: **{len(overlap)}/{min(len(curr_codes), len(prev_codes))}** ({overlap_rate:.0f}%)")
        if overlap:
            overlap_names = [
                f"{e['name']}({e['ts_code']})"
                for r in [curr, prev]
                for e in r["entries"]
                if e["ts_code"] in overlap and e["ts_code"] not in {
                    n.split("(")[-1].rstrip(")") for n in (lines[-1].split(": ")[-1].split(", ") if len(lines) > 1 else [])
                }
            ]
            unique_names = list(dict.fromkeys(
                e["name"] for r in [curr] for e in r["entries"] if e["ts_code"] in overlap
            ))
            lines.append(f"- 重叠票: {', '.join(unique_names[:5])}")
        else:
            lines.append("- ⚠️ 完全换榜，无重复票")

        # 规则版本变化
        if curr["rule_version"] != prev["rule_version"]:
            lines.append(f"- 规则版本: `{prev['rule_version']}` → `{curr['rule_version']}` ⚠️")
        else:
            lines.append(f"- 规则版本: `{curr['rule_version']}`（不变）")

        lines.append("")

    # ── 打分漂移 ──
    if len(runs) >= 2:
        lines.append("### 打分漂移")
        lines.append("")
        # 找重叠票的打分变化
        curr = runs[0]
        prev = runs[1]
        curr_map = {e["ts_code"]: e for e in curr["entries"]}
        prev_map = {e["ts_code"]: e for e in prev["entries"]}
        drift_items = []
        for code in curr_codes & prev_codes:
            ce = curr_map[code]
            pe = prev_map[code]
            rule_d = (ce.get("rule_score") or 0) - (pe.get("rule_score") or 0)
            llm_d = (ce.get("llm_score") or 0) - (pe.get("llm_score") or 0)
            drift_items.append((ce["name"], code, rule_d, llm_d))

        if drift_items:
            for name, code, rd, ld in drift_items[:5]:
                r_arrow = "🔺" if rd > 0 else ("🔻" if rd < 0 else "➖")
                l_arrow = "🔺" if ld > 0 else ("🔻" if ld < 0 else "➖")
                lines.append(f"- {name}({code}): 规则{r_arrow}{rd:+.0f} LLM{l_arrow}{ld:+.0f}")
        else:
            lines.append("- 无重叠票可对比")

        lines.append("")

    lines.extend([
        "---",
        "💡 趋势向好=策略迭代有效 · 持续恶化=需 review prompt/权重",
    ])

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv
    periods = 5
    for i, arg in enumerate(sys.argv):
        if arg == "--periods" and i + 1 < len(sys.argv):
            periods = int(sys.argv[i + 1])
            break

    init_db()

    with SessionLocal() as session:
        runs = _get_pick_runs(session, limit=periods)
        if len(runs) < 2:
            print("⚠️ 至少需要 2 期有 TOP10 的 pick runs 才能做对比追踪")
            return

        eval_summaries: dict[int, dict[str, Any]] = {}
        for r in runs:
            es = _get_eval_summary(session, r["run_id"])
            if es:
                eval_summaries[r["run_id"]] = es

    markdown = _build_tracker_markdown(runs, eval_summaries)

    if dry_run:
        print("=" * 50)
        print("[DRY RUN] 跨期追踪推送预览:")
        print("=" * 50)
        print(markdown)
        print("=" * 50)
        print(f"字节数: {len(markdown.encode('utf-8'))} / {WECHAT_SAFE_LIMIT}")
        return

    ok = push_wechat_markdown(markdown)
    if ok:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ✅ 跨期追踪推送成功 ({len(runs)} 期)")
    else:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ❌ 跨期追踪推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
