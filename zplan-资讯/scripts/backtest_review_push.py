#!/usr/bin/env python3
"""盘后回测验证推送：TOP10 多窗口 forward 收益 + 峰值 + 失败标签诊断 + 优化建议。

对比 evening_review.py（同日涨跌复盘），本脚本关注：
- 多窗口 forward 验证（5/10/20 日 + 峰值收益 + 最大回撤）
- LLM 失败标签（momentum_chase / buy_unreachable / score_inflation 等）
- 数据完整性门禁
- 策略优化方向

用法:
    cd zplan-资讯 && .venv/bin/python scripts/backtest_review_push.py
    cd zplan-资讯 && .venv/bin/python scripts/backtest_review_push.py --dry-run
    cd zplan-资讯 && .venv/bin/python scripts/backtest_review_push.py --run-id 42
    cd zplan-资讯 && .venv/bin/python scripts/backtest_review_push.py --top 10

调度: 盘后管道完成后自动调用（run_full_pipeline.sh 最后一步）。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NEWS_ROOT))

from sqlalchemy import text

from zplan_shared.models import SessionLocal, init_db
from zplan_shared.pick_llm_eval import (
    FAIL_TAG_LABELS,
    evaluate_llm_run,
)
from zplan_shared.pick_predictions import validate_entries
from wechat_push import push_wechat_markdown

BEIJING_TZ = timezone(timedelta(hours=8))
WECHAT_SAFE_LIMIT = 3800

# 默认验证窗口
DEFAULT_HORIZONS = [5, 10, 20]


def _check_data_ready(
    as_of: date,
    min_symbols: int = 4000,
    min_forward_days: int = 5,
) -> tuple[bool, str, dict[str, Any]]:
    """数据质量门禁：验证 as_of 日截面完整 + forward K 线充足。

    Returns:
        (ready, reason, info) — info 含各窗口数据状态
    """
    init_db()
    info: dict[str, Any] = {"as_of": str(as_of), "checked_at": datetime.now(BEIJING_TZ).isoformat()}

    with SessionLocal() as s:
        # 1. as_of 日截面
        r = s.execute(
            text("SELECT COUNT(DISTINCT ts_code) FROM daily_prices WHERE trade_date = :d"),
            {"d": str(as_of)},
        ).fetchone()
        as_of_count = int(r[0]) if r else 0
        info["as_of_symbols"] = as_of_count

        if as_of_count < min_symbols:
            return False, f"as_of={as_of} 截面仅 {as_of_count} 只（需 ≥{min_symbols}），数据未补全", info

        # 2. forward 交易日
        fwd_dates = s.execute(
            text(
                "SELECT DISTINCT trade_date FROM daily_prices "
                "WHERE trade_date > :d ORDER BY trade_date"
            ),
            {"d": str(as_of)},
        ).fetchall()
        fwd_dates = [row[0] for row in fwd_dates]
        info["forward_trading_days"] = len(fwd_dates)

        if len(fwd_dates) < min_forward_days:
            return False, f"as_of={as_of} 后仅 {len(fwd_dates)} 个交易日（需 ≥{min_forward_days}）", info

        # 3. 每个 forward 窗口的截面完整性
        for h in DEFAULT_HORIZONS:
            if len(fwd_dates) >= h:
                d = str(fwd_dates[h - 1])
                cnt = s.execute(
                    text("SELECT COUNT(DISTINCT ts_code) FROM daily_prices WHERE trade_date = :d"),
                    {"d": d},
                ).fetchone()[0]
                info[f"fwd_{h}d_symbols"] = cnt
                if cnt < min_symbols:
                    info[f"fwd_{h}d_warning"] = f"仅 {cnt} 只"
            else:
                info[f"fwd_{h}d_symbols"] = 0
                info[f"fwd_{h}d_warning"] = "交易日不足"

    return True, "ok", info


def _suggest_action(tags: list[str], verdict: str) -> str:
    """根据失败标签组合给出后续操作建议。"""
    if not tags:
        if verdict == "pass":
            return "按建议价持有，跟踪止盈"
        return "继续观察"

    actions = []
    if "forward_loss" in tags:
        actions.append("观望等企稳")
    if "momentum_chase" in tags:
        actions.append("追高回避，等回调")
    if "buy_unreachable" in tags:
        actions.append("等回落至买入区")
    if "near_60d_high" in tags:
        actions.append("注意高位风险，轻仓试探")
    if "score_inflation" in tags:
        actions.append("评分偏高，需深入研判")
    if "generic_bullish" in tags:
        actions.append("理由不充分，需深度研报")
    if "over_recommendation" in tags:
        actions.append("推荐过激，降档至观望")
    if "no_forward_data" in tags:
        actions.append("数据不足，继续观察")
    if "forward_flat" in tags:
        actions.append("横盘待方向选择")

    if not actions:
        return "继续观察"

    return "；".join(actions[:3])  # 最多 3 条


def _build_compact_markdown(
    llm_eval: dict[str, Any],
    outcomes_by_horizon: dict[int, list[dict[str, Any]]],
    top_n: int,
    data_health: dict[str, Any] | None = None,
) -> str:
    """构建压缩版企微 Markdown（≤3800 字节），含多窗口收益 + 峰值。"""
    s = llm_eval.get("summary") or {}
    total = s.get("total", 0)
    fail_n = s.get("fail", 0)
    pass_n = s.get("pass", 0)
    pending_n = s.get("pending", 0)
    fail_rate = s.get("fail_rate") or 0
    run_id = llm_eval.get("run_id", "?")
    as_of = llm_eval.get("trade_date_as_of") or "?"
    run_kind = llm_eval.get("run_kind", "?")

    beijing_now = datetime.now(BEIJING_TZ)
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    # 数据状态
    data_icon = "✅" if (data_health or {}).get("as_of_symbols", 0) >= 4000 else "⚠️"
    as_of_count = (data_health or {}).get("as_of_symbols", "?")
    fwd_days = (data_health or {}).get("forward_trading_days", "?")

    # pick 距今
    try:
        pick_d = date.fromisoformat(str(as_of))
        gap = (beijing_now.date() - pick_d).days
        age_note = f" · {gap}天前" if gap > 3 else ""
    except (ValueError, TypeError):
        age_note = ""

    entries = llm_eval.get("entries") or []

    # ── 构建消息 ──
    lines = [
        f"## 🔬 TOP10 回测 · run={run_id}",
        f"> {beijing_now:%m-%d} {weekday} · as_of={as_of}{age_note} · kind={run_kind}",
        f"> 数据: {data_icon} as_of={as_of_count}只 · 后续{fwd_days}交易日",
        "",
    ]

    # ── 多窗口收益面板 ──
    lines.append("### 📊 多窗口收益")
    lines.append("")
    lines.append("| 窗口 | 均收益 | 胜率 | 峰值均 | 触及率 |")
    lines.append("|------|--------|------|--------|--------|")

    for h in DEFAULT_HORIZONS:
        outcomes = outcomes_by_horizon.get(h, [])
        if not outcomes:
            lines.append(f"| T+{h} | — | — | — | — |")
            continue

        complete = [o for o in outcomes if o.get("status") in ("complete", "partial")]
        if not complete:
            lines.append(f"| T+{h} | 数据不足 | — | — | — |")
            continue

        rets = [o.get("return_from_close_pct") for o in complete if o.get("return_from_close_pct") is not None]
        peaks = [o.get("peak_return_pct") for o in complete if o.get("peak_return_pct") is not None]
        touches = [o for o in complete if o.get("buy_touched")]

        avg_ret = sum(rets) / len(rets) if rets else None
        avg_peak = sum(peaks) / len(peaks) if peaks else None
        win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100 if rets else 0
        touch_rate = len(touches) / len(complete) * 100 if complete else 0

        ret_s = f"{avg_ret:+.1f}%" if avg_ret is not None else "—"
        peak_s = f"{avg_peak:+.1f}%" if avg_peak is not None else "—"
        win_s = f"{win_rate:.0f}%"
        touch_s = f"{touch_rate:.0f}%"

        lines.append(f"| T+{h} | {ret_s} | {win_s} | {peak_s} | {touch_s} |")

    lines.append("")

    # ── 总览面板 ──
    lines.append("### 📊 诊断总览")
    lines.append(f"- 样本 **{total}** | ❌ 失败 **{fail_n}** | ✅ 通过 **{pass_n}** | ⏳ 待验证 **{pending_n}**")

    if fail_rate > 0:
        lines.append(f"- 失败率 **{fail_rate:.0%}**")

    # 风险提示（near_60d_high 不算 fail）
    risk_warned = sum(
        1 for e in entries
        if "near_60d_high" in (e.get("failure_tags") or [])
        and e.get("verdict") not in ("fail",)
    )
    if risk_warned > 0:
        lines.append(f"- ⚠️ 风险提示（不判fail）: **{risk_warned}** 只 near_60d_high")

    # ── 失败标签 ──
    tag_counts: dict[str, int] = llm_eval.get("tag_counts") or {}
    if tag_counts:
        lines.extend(["", "### 🏷️ 标签分布"])
        for tag, cnt in sorted(tag_counts.items(), key=lambda x: -x[1])[:5]:
            label = FAIL_TAG_LABELS.get(tag, tag)
            lines.append(f"- `{tag}`: **{cnt}** — {label}")

    # ── 逐只明细表格（维持原始排名，紧凑 11 列）──
    lines.extend(["", "### 📋 逐只明细"])
    lines.append("")

    header = ["#", "股票", "评分 规则/LLM", "选股收盘", "建议买入",
              "最新收盘", "最新收益", "期间最高", "最高收益", "风险提示", "后续建议"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    # 保持原始排名
    for entry in sorted(entries, key=lambda e: e.get("rank") or 999):
        rank = entry.get("rank", "?")
        name = entry.get("name") or "?"
        code = entry.get("ts_code", "?")
        tags = entry.get("failure_tags") or []
        entry_id = entry.get("entry_id")
        close = entry.get("close_at_pick")
        buy = entry.get("predicted_buy")
        rule_score = entry.get("rule_score")
        llm_score = entry.get("llm_score")

        stock_label = f"{name} {code}"
        close_str = f"¥{close:.2f}" if close is not None else "—"

        # 评分
        rule_s = f"{rule_score:.0f}" if rule_score is not None else "—"
        llm_s = f"{llm_score:.0f}" if llm_score is not None else "—"
        score_str = f"{rule_s} / {llm_s}"

        # 建议买入价
        buy_str = f"¥{buy:.2f}" if (buy is not None and buy > 0) else "—"

        # 最新收盘 + 收益：取最远可用窗口
        latest_close = None
        latest_fwd = None
        all_peaks: list[tuple[float, float]] = []
        for h in [20, 10, 5]:
            outcomes = outcomes_by_horizon.get(h, [])
            match = [o for o in outcomes if o.get("entry_id") == entry_id]
            if match and match[0].get("return_from_close_pct") is not None:
                o = match[0]
                latest_close = o.get("close_at_horizon")
                latest_fwd = o.get("return_from_close_pct")
                max_high = o.get("max_high")
                peak = o.get("peak_return_pct")
                if max_high is not None and peak is not None:
                    all_peaks.append((max_high, peak))
                break
        if latest_fwd is None:
            latest_fwd = entry.get("return_from_close_pct")
        if latest_close is None:
            latest_close = entry.get("latest_close")

        latest_close_str = f"¥{latest_close:.2f}" if latest_close is not None else "—"
        latest_fwd_str = f"{latest_fwd:+.2f}%" if latest_fwd is not None else "—"

        # 期间最高 + 最高收益
        if all_peaks:
            best_peak = max(all_peaks, key=lambda x: x[1])
            peak_price_str = f"¥{best_peak[0]:.2f}"
            peak_ret_str = f"{best_peak[1]:+.2f}%"
        else:
            peak_price_str = "—"
            peak_ret_str = "—"

        # 风险 + 建议
        tag_labels = [FAIL_TAG_LABELS.get(t, t) for t in tags] if tags else []
        tag_str = ", ".join(tag_labels) if tag_labels else "—"
        action = _suggest_action(tags, entry.get("verdict", ""))

        row = [str(rank), stock_label, score_str, close_str, buy_str,
               latest_close_str, latest_fwd_str,
               peak_price_str, peak_ret_str,
               tag_str, action]
        lines.append("| " + " | ".join(row) + " |")

    # ── 优先优化建议 ──
    opt = llm_eval.get("optimization") or {}
    priority = opt.get("priority") or []
    wc = opt.get("where_to_change") or {}
    prompt_hints = wc.get("prompt") or []
    strategy_hints = wc.get("strategy_yaml") or []

    if priority or prompt_hints:
        lines.extend(["", "### 🔧 优先优化"])
        shown = 0
        for tag, cnt in priority[:3]:
            label = FAIL_TAG_LABELS.get(tag, tag)
            lines.append(f"- [{tag}] {label}（{cnt}次）")
            shown += 1
        if shown == 0 and prompt_hints:
            lines.append(f"- {prompt_hints[0][:80]}")
        if len(strategy_hints) > 0:
            lines.append(f"- {strategy_hints[0][:80]}")

    lines.extend([
        "",
        "---",
        "💡 发送「**选股清单**」查看最新推荐 · `iterate verify` 看完整报告",
    ])

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv

    # 解析参数
    run_id: int | None = None
    top_n: int = 10
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--run-id" and i + 1 < len(args):
            run_id = int(args[i + 1])
            i += 2
        elif args[i] == "--top" and i + 1 < len(args):
            top_n = int(args[i + 1])
            i += 2
        else:
            i += 1

    init_db()

    # 1. LLM 失败标签诊断 + forward 收益（用最长的 horizon 做诊断）
    max_horizon = max(DEFAULT_HORIZONS)
    llm_eval = evaluate_llm_run(run_id=run_id, top_n=top_n, horizon_days=max_horizon)
    if not llm_eval.get("ok"):
        msg = llm_eval.get("message", "评估失败")
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ⚠️ {msg}")
        if dry_run:
            print(f"[DRY RUN] 无法生成报告: {msg}")
        return

    resolved_run_id = llm_eval["run_id"]
    as_of_str = llm_eval.get("trade_date_as_of")
    as_of = date.fromisoformat(str(as_of_str)) if as_of_str else None

    # 2. 数据质量门禁
    data_ready, data_reason, data_health = True, "ok", {}
    if as_of:
        data_ready, data_reason, data_health = _check_data_ready(as_of)

    # 3. 多窗口预测验证
    outcomes_by_horizon: dict[int, list[dict[str, Any]]] = {}
    for h in DEFAULT_HORIZONS:
        validation = validate_entries(
            run_id=resolved_run_id, horizons=[h], limit=top_n * 3
        )
        # 从 outcomes 表读结果
        from zplan_shared.pick_predictions import list_outcomes

        outcomes = list_outcomes(limit=top_n * 3, horizon_days=h)
        # 只保留当前 run 的
        outcomes = [o for o in outcomes if o.get("run_id") == resolved_run_id][:top_n]
        outcomes_by_horizon[h] = outcomes

    # 4. 构建推送消息
    markdown = _build_compact_markdown(
        llm_eval, outcomes_by_horizon, top_n,
        data_health=data_health if not data_ready else data_health,
    )

    # 数据质量警告
    if not data_ready:
        warning = f"\n⚠️ **数据完整性警告**: {data_reason}\n"
        markdown = warning + markdown

    if dry_run:
        print("=" * 50)
        print("[DRY RUN] 企微推送内容预览:")
        print("=" * 50)
        print(markdown)
        print("=" * 50)
        byte_count = len(markdown.encode("utf-8"))
        print(f"字节数: {byte_count} / {WECHAT_SAFE_LIMIT}")
        if byte_count > WECHAT_SAFE_LIMIT:
            print(f"⚠️ 超出企微上限 {byte_count - WECHAT_SAFE_LIMIT} 字节！")
        if not data_ready:
            print(f"\n⚠️ 数据质量: {data_reason}")
        print(f"\n数据健康: {data_health}")
        print(f"\n完整 LLM Eval 报告可运行：")
        print(f"  cd zplan-回测 && .venv/bin/python main.py llm-eval --run-id {resolved_run_id} --top {top_n}")
        return

    # 5. 推送到企业微信
    ok = push_wechat_markdown(markdown)
    if ok:
        s = llm_eval.get("summary") or {}
        print(
            f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ✅ 回测验证推送成功 "
            f"(run={resolved_run_id}, fail={s.get('fail', 0)}/{s.get('total', 0)})"
        )
    else:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ❌ 回测验证推送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
