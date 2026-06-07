#!/usr/bin/env python3
"""
企微对话质量审查 + 循环优化智能体。

每周拉取 chat_history → LLM 多维度打分 → 对比历史 → 生成优化动作 → 存迭代记录。
形成 Plan（优化动作）→ Do（人工/自动应用）→ Check（下周审查验证）→ Act（迭代）闭环。

用法::

    # 审查最近 7 天对话（默认，适合周度运行）
    .venv/bin/python scripts/chat_quality_review.py

    # 审查最近 3 天
    .venv/bin/python scripts/chat_quality_review.py --days 3

    # 抽样 50 条（默认 30）
    .venv/bin/python scripts/chat_quality_review.py --limit 50

    # 仅审查特定意图
    .venv/bin/python scripts/chat_quality_review.py --intent stock_summary,deep_research

    # 输出到终端，不推送企微
    .venv/bin/python scripts/chat_quality_review.py --no-push

    # 查看优化历史
    .venv/bin/python scripts/chat_quality_review.py --history

流程：
    1. 加载上轮迭代记录 + 优化动作
    2. 从 chat_history 拉取近 N 天记录（去重、抽样）
    3. 分批送 LLM 多维度评分（准确性/完整性/简洁性/可操作性/意图匹配）
    4. LLM 结合历史优化动作，判断哪些生效、生成新的优化建议
    5. 对比上轮 → 计算改善幅度 → 标记动作有效性
    6. 存入 chat_iterations 迭代记录
    7. 生成 Markdown 报告 → 终端输出 / 企微推送
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_MONO = _HERE.parent.parent
_SHARED_SRC = _MONO / "zplan-共享" / "src"
_NEWS = _MONO / "zplan-资讯"

for _p in (str(_SHARED_SRC), str(_NEWS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("ZPLAN_ROOT", str(_NEWS))

from zplan_shared.config import CHAT_HISTORY_ENABLED, ZPLAN_ROOT
from zplan_shared.models import ChatHistory, SessionLocal, init_db
from zplan_shared.llm.deepseek import generate_json_with_deepseek, deepseek_available
from zplan_shared.chat_iterate_store import (
    append_chat_iteration,
    list_chat_iterations,
    load_chat_iteration,
    compare_chat_iterations,
    format_action_history,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("chat_quality_review")

# ── 常量 ──────────────────────────────────────────────
DEFAULT_DAYS = 7  # 默认周度审查
DEFAULT_LIMIT = 30
BATCH_SIZE = 5  # 每次 LLM 调用评估的记录数

# 评估维度
DIMENSIONS = {
    "accuracy": "准确性 — 数据是否真实、无幻觉，引用是否正确",
    "completeness": "完整性 — 是否覆盖了用户问题的核心方面",
    "conciseness": "简洁性 — 是否啰嗦冗余，信息密度是否够高",
    "actionability": "可操作性 — 是否给了用户下一步操作的指引或建议",
    "intent_match": "意图匹配 — 回复是否准确理解了用户的真实意图",
}

# ── JSON Schema（包含优化动作）────────────────────────
EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "integer"},
                    "scores": {
                        "type": "object",
                        "properties": {
                            "accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
                            "completeness": {"type": "integer", "minimum": 1, "maximum": 5},
                            "conciseness": {"type": "integer", "minimum": 1, "maximum": 5},
                            "actionability": {"type": "integer", "minimum": 1, "maximum": 5},
                            "intent_match": {"type": "integer", "minimum": 1, "maximum": 5},
                        },
                        "required": ["accuracy", "completeness", "conciseness", "actionability", "intent_match"],
                        "additionalProperties": False,
                    },
                    "overall": {"type": "integer", "minimum": 1, "maximum": 5},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "suggestion": {"type": "string"},
                },
                "required": ["record_id", "scores", "overall", "issues", "suggestion"],
                "additionalProperties": False,
            },
        },
        "summary_insights": {
            "type": "array",
            "items": {"type": "string"},
            "description": "系统性模式与洞察",
        },
        "optimization_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_dimension": {
                        "type": "string",
                        "enum": ["accuracy", "completeness", "conciseness", "actionability", "intent_match", "system"],
                        "description": "目标优化维度（system 表示系统性调整）",
                    },
                    "action": {
                        "type": "string",
                        "description": "具体的优化动作描述（可执行）",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "为什么这个动作能改善对应维度",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["prompt", "code", "workflow", "data", "config"],
                        "description": "改动层面",
                    },
                    "effort": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "实施工作量",
                    },
                    "expected_impact": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "预期改善幅度",
                    },
                },
                "required": ["target_dimension", "action", "reasoning", "layer", "effort", "expected_impact"],
                "additionalProperties": False,
            },
            "description": "本轮生成的具体优化动作（供后续执行和验证）",
        },
    },
    "required": ["evaluations", "summary_insights", "optimization_actions"],
    "additionalProperties": False,
}


# ── 数据拉取 ──────────────────────────────────────────
def fetch_records(
    *,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    intent_filter: list[str] | None = None,
    since_record_id: int | None = None,
) -> tuple[list[ChatHistory], int]:
    """拉取近 N 天的对话记录（排除 error，去重抽样）。

    Returns:
        (records, total_count_before_dedup)
    """
    init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    from sqlalchemy import desc, select, func

    with SessionLocal() as session:
        q = (
            select(ChatHistory)
            .where(
                ChatHistory.created_at_utc >= cutoff,
                ChatHistory.error.is_(None),
                ChatHistory.bot_reply.isnot(None),
                ChatHistory.bot_reply != "",
            )
            .order_by(desc(ChatHistory.created_at_utc))
        )

        if intent_filter:
            q = q.where(ChatHistory.bot_intent.in_(intent_filter))
        if since_record_id is not None:
            q = q.where(ChatHistory.id > since_record_id)

        total = session.execute(
            select(func.count()).select_from(q.subquery())
        ).scalar_one()

        records = list(session.execute(q.limit(limit * 2)).scalars().all())

    if not records:
        return [], total

    # 去重
    deduped: list[ChatHistory] = []
    seen_keys: set[tuple] = set()
    for r in records:
        key = (r.channel, r.user_id or "", r.user_message[:60])
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(r)
        if len(deduped) >= limit:
            break

    logger.info("拉取 %d 条（总数 %d，去重后 %d，限制 %d）", len(deduped), total, len(deduped), limit)
    return deduped, total


# ── LLM 评估 ──────────────────────────────────────────
def _build_eval_prompt(
    batch: list[ChatHistory],
    action_history_text: str,
) -> str:
    """构建单批评估 prompt（含历史优化上下文）。"""
    parts: list[str] = []
    for r in batch:
        msg = r.user_message[:300]
        reply = (r.bot_reply or "")[:800]
        parts.append(
            f"### 记录 {r.id}\n"
            f"- 通道: {r.channel} | 意图: {r.bot_intent or '未知'}\n"
            f"- 用户: {msg}\n"
            f"- 回复: {reply}\n"
        )

    records_text = "\n".join(parts)
    dims_text = "\n".join(f"- {k}: {v}" for k, v in DIMENSIONS.items())

    return f"""你是一个 AI 客服质量审核专家，负责评估企微股票助手 Z-Plan 的对话质量。

## 评估维度（每个维度 1-5 分）

{dims_text}

评分标准：
- 5: 优秀 — 无显著问题
- 4: 良好 — 有小瑕疵但不影响使用
- 3: 及格 — 基本可用但有明显不足
- 2: 较差 — 存在明显错误或缺失
- 1: 很差 — 答非所问或有害信息

{action_history_text}

## 本周对话记录

{records_text}

## 输出要求

1. **evaluations**: 对每条记录打分，issues 列出具体问题，suggestion 给出改进建议
2. **summary_insights**: 系统性模式（如"XX 意图普遍缺少 XX"）
3. **optimization_actions**: 基于评估结果 + 历史优化效果，生成 2-4 个具体的优化动作：
   - 优先针对最弱维度
   - 如果历史有已验证有效的动作，考虑深化
   - 如果历史有无效动作，避免重复或调整方向
   - 每个动作需包含 target_dimension、action、reasoning、layer、effort、expected_impact
   - layer 可选: prompt（改 system prompt）、code（改代码逻辑）、workflow（改流程）、data（补数据）、config（调配置）

所有文本使用中文。"""


def evaluate_batch(
    batch: list[ChatHistory],
    action_history_text: str = "",
) -> dict[str, Any] | None:
    """调用 LLM 评估一批对话记录。"""
    if not deepseek_available():
        logger.error("DeepSeek API 不可用，跳过 LLM 评估")
        return None

    prompt = _build_eval_prompt(batch, action_history_text)
    try:
        result = generate_json_with_deepseek(
            prompt=prompt,
            response_schema=EVAL_SCHEMA,
            temperature=0.2,
            max_output_tokens=4096,
        )
        return result
    except Exception as exc:
        logger.warning("评估批次失败: %s", exc)
        return None


def evaluate_all(
    records: list[ChatHistory],
    action_history_text: str = "",
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """分批评估，返回 (评估条目, 洞察, 优化动作)。"""
    all_evals: list[dict[str, Any]] = []
    all_insights: list[str] = []
    all_actions: list[dict[str, Any]] = []

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        logger.info(
            "评估批次 %d/%d（记录 %d-%d）...",
            i // BATCH_SIZE + 1,
            (len(records) + BATCH_SIZE - 1) // BATCH_SIZE,
            batch[0].id if batch else 0,
            batch[-1].id if batch else 0,
        )
        result = evaluate_batch(batch, action_history_text)
        if result:
            all_evals.extend(result.get("evaluations") or [])
            all_insights.extend(result.get("summary_insights") or [])
            all_actions.extend(result.get("optimization_actions") or [])
        else:
            for r in batch:
                all_evals.append({
                    "record_id": r.id,
                    "scores": {k: 0 for k in DIMENSIONS},
                    "overall": 0,
                    "issues": ["LLM 评估不可用"],
                    "suggestion": "",
                })

    return all_evals, all_insights, all_actions


# ── 统计 ──────────────────────────────────────────────
def _build_id_map(records: list[ChatHistory]) -> dict[int, ChatHistory]:
    return {r.id: r for r in records}


def _compute_stats(
    evals: list[dict[str, Any]], records: list[ChatHistory]
) -> dict[str, Any]:
    """汇总统计。"""
    id_map = _build_id_map(records)
    n = len(evals)

    if n == 0:
        return {
            "total": 0, "mean_overall": 0, "dim_means": {},
            "bad_cases": [], "by_intent": {},
        }

    overalls = [e.get("overall", 0) for e in evals if e.get("overall", 0) > 0]
    mean_overall = sum(overalls) / len(overalls) if overalls else 0

    dim_sums: dict[str, float] = {k: 0.0 for k in DIMENSIONS}
    dim_counts: dict[str, int] = {k: 0 for k in DIMENSIONS}
    for e in evals:
        scores = e.get("scores") or {}
        for dim in DIMENSIONS:
            v = scores.get(dim, 0)
            if v and v > 0:
                dim_sums[dim] += v
                dim_counts[dim] += 1
    dim_means = {
        k: round(dim_sums[k] / dim_counts[k], 2) if dim_counts[k] > 0 else 0
        for k in DIMENSIONS
    }

    bad_cases = sorted(
        [e for e in evals if 1 <= e.get("overall", 0) <= 2],
        key=lambda e: e.get("overall", 5),
    )

    by_intent: dict[str, dict[str, Any]] = {}
    for e in evals:
        rec = id_map.get(e.get("record_id", 0))
        intent = rec.bot_intent if rec else "unknown"
        if intent not in by_intent:
            by_intent[intent] = {"count": 0, "overall_sum": 0, "overall_n": 0}
        by_intent[intent]["count"] += 1
        ov = e.get("overall", 0)
        if ov > 0:
            by_intent[intent]["overall_sum"] += ov
            by_intent[intent]["overall_n"] += 1

    return {
        "total": n,
        "mean_overall": round(mean_overall, 2),
        "dim_means": dim_means,
        "bad_cases": bad_cases,
        "by_intent": by_intent,
    }


# ── 报告生成 ──────────────────────────────────────────
def _star_bar(score: float, max_stars: int = 5) -> str:
    full = round(score)
    return "★" * full + "☆" * (max_stars - full)


def _trend_icon(delta: float | None) -> str:
    if delta is None:
        return ""
    if delta > 0.1:
        return " 📈"
    if delta < -0.1:
        return " 📉"
    return " ➡️"


def build_report(
    stats: dict[str, Any],
    insights: list[str],
    optimization_actions: list[dict[str, Any]],
    comparison: dict[str, Any] | None,
    days: int,
    limit: int,
) -> str:
    """生成完整的 Markdown 报告（含优化闭环）。"""
    now_str = datetime.now().strftime("%m-%d %H:%M")
    parts = [
        f"# 💬 对话质量周报 · {now_str}",
        f"范围: 近 {days} 天 · 抽样 {stats['total']} 条",
        "",
    ]

    # ── 总览 + 趋势 ──
    mean = stats["mean_overall"]
    delta_str = ""
    if comparison:
        deltas = comparison.get("deltas") or {}
        delta_mean = deltas.get("mean_overall")
        if delta_mean is not None:
            delta_str = f"（{'↑' if delta_mean > 0 else '↓' if delta_mean < 0 else '→'}{abs(delta_mean):.1f} vs 上轮）"
        improved = "✅ 改善中" if comparison.get("improved") else "⚠️ 待改善"
        parts.append(f"**趋势**: {improved} | 上轮对比: {comparison.get('older_id', '?')[:12]}...")
        parts.append("")

    parts.append("## 总览")
    parts.append(f"综合均分 **{mean:.1f}** {_star_bar(mean)} {delta_str}（{stats['total']} 条）")
    parts.append("")

    # ── 各维度 ──
    parts.append("| 维度 | 均分 | 评级 | 变化 |")
    parts.append("|------|------|------|------|")
    prev_dims = (comparison.get("deltas") or {}).get("dim_means") or {} if comparison else {}
    for dim, label in DIMENSIONS.items():
        s = stats["dim_means"].get(dim, 0)
        d = prev_dims.get(dim, 0) if prev_dims else 0
        delta_cell = f"↑{d:+.1f}" if d > 0 else f"↓{d:+.1f}" if d < 0 else "—"
        parts.append(f"| {label} | {s:.1f} | {_star_bar(s)} | {delta_cell} |")
    parts.append("")

    # 最弱维度
    dim_sorted = sorted(stats["dim_means"].items(), key=lambda x: x[1])
    if dim_sorted:
        worst_dim, worst_score = dim_sorted[0]
        improving = prev_dims.get(worst_dim, 0) > 0 if prev_dims else False
        hint = "（但正在改善 ↑）" if improving else "，优先改进"
        parts.append(f"⚠️ 最弱维度：**{DIMENSIONS[worst_dim]}**（{worst_score:.1f}）{hint}")
        parts.append("")

    # ── 按意图 ──
    by_intent = stats.get("by_intent") or {}
    if by_intent:
        parts.append("## 按意图")
        parts.append("| 意图 | 数量 | 均分 |")
        parts.append("|------|------|------|")
        for intent, info in sorted(by_intent.items(), key=lambda x: -x[1]["count"]):
            avg = info["overall_sum"] / info["overall_n"] if info["overall_n"] > 0 else 0
            parts.append(f"| `{intent}` | {info['count']} | {avg:.1f} {_star_bar(avg)} |")
        parts.append("")

    # ── Bad cases ──
    bad_cases = stats.get("bad_cases") or []
    if bad_cases:
        parts.append(f"## 🔴 待改进（{len(bad_cases)} 条）")
        for bc in bad_cases[:5]:
            rid = bc.get("record_id", "?")
            ov = bc.get("overall", "?")
            issues = bc.get("issues") or []
            suggestion = bc.get("suggestion") or ""
            issues_str = "；".join(issues[:3]) if issues else "无具体问题"
            parts.append(f"- **#{rid}** {ov}/5：{issues_str}")
            if suggestion:
                parts.append(f"  > 💡 {suggestion[:120]}")
        parts.append("")

    # ── 系统性洞察 ──
    if insights:
        parts.append("## 💡 系统性洞察")
        for ins in insights[:6]:
            parts.append(f"- {ins}")
        parts.append("")

    # ── 优化动作（核心：闭环） ──
    if optimization_actions:
        parts.append("## 🎯 优化动作（PDCA 闭环）")
        parts.append("| 维度 | 动作 | 层面 | 工作量 | 预期效果 |")
        parts.append("|------|------|------|--------|----------|")
        for a in optimization_actions[:6]:
            dim = a.get("target_dimension", "?")
            act = a.get("action", "")[:60]
            layer = a.get("layer", "?")
            effort = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(a.get("effort", ""), "?")
            impact = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(a.get("expected_impact", ""), "?")
            parts.append(f"| {dim} | {act} | {layer} | {effort} | {impact} |")
        parts.append("")

        parts.append("### 动作详情")
        for a in optimization_actions[:4]:
            parts.append(
                f"- **[{a.get('target_dimension', '?')}]** {a.get('action', '')}\n"
                f"  > 理由: {a.get('reasoning', '')[:150]}\n"
                f"  > 层面: `{a.get('layer', '?')}` | 工作量: {a.get('effort', '?')} | 预期: {a.get('expected_impact', '?')}"
            )
        parts.append("")

    # ── 历史动作验证 ──
    if comparison:
        action_results = comparison.get("action_results") or []
        if action_results:
            parts.append("## 🔬 上轮动作验证")
            for ar in action_results:
                eff = "✅ 有效" if ar.get("effective") else "❌ 无效"
                d = ar.get("dim_delta", 0)
                parts.append(
                    f"- {eff}: {ar.get('action', '')[:80]}\n"
                    f"  > 维度变化: {d:+.2f}"
                )
            parts.append("")

    parts.append("---")
    parts.append(f"*报告由 chat_quality_review.py 自动生成 · 迭代记录: zplan-资讯/backtest_review/chat_iterations/*")
    return "\n".join(parts)


# ── 企微推送 ──────────────────────────────────────────
def _push_report(markdown: str) -> bool:
    try:
        from wechat_push import push_wechat_markdown
        return push_wechat_markdown(markdown)
    except ImportError:
        from wechat_push import push_wechat_text
        text = markdown.replace("*", "").replace("#", "").replace(">", "  ")
        return push_wechat_text(text)
    except Exception as exc:
        logger.warning("企微推送失败: %s", exc)
        return False


# ── 主流程：PDCA 闭环审查 ─────────────────────────────
def review(
    *,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    intent_filter: list[str] | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """执行对话质量审查 + 优化闭环。

    1. 加载上轮迭代 → 获取历史优化动作
    2. 拉取近期对话 → LLM 多维度评估
    3. 对比上轮 → 计算改善幅度 → 验证动作有效性
    4. 生成新优化动作 → 存入迭代记录
    5. 输出报告
    """
    init_db()

    if not CHAT_HISTORY_ENABLED:
        logger.warning("CHAT_HISTORY_ENABLED=false，chat_history 可能为空")

    # ── Step 1: 加载上轮迭代 ──
    prev_iterations = list_chat_iterations(limit=1)
    prev_iteration = None
    if prev_iterations:
        prev_iid = prev_iterations[0].get("iteration_id", "")
        prev_iteration = load_chat_iteration(prev_iid)
        if prev_iteration:
            logger.info("加载上轮迭代: %s (综合 %.1f)", prev_iid[:12], prev_iteration.get("metrics", {}).get("mean_overall", 0))

    action_history_text = format_action_history(limit=3)

    # ── Step 2: 拉取记录 ──
    records, total_available = fetch_records(
        days=days, limit=limit, intent_filter=intent_filter,
    )

    if not records:
        logger.info("近 %d 天无对话记录", days)
        return {
            "iteration_id": None,
            "total": 0,
            "mean_overall": 0,
            "bad_case_count": 0,
            "dim_means": {},
            "insights_count": 0,
            "actions_count": 0,
            "improved": None,
            "report": f"# 💬 对话质量周报\n\n近 {days} 天无对话记录，跳过。",
            "pushed": False,
        }

    # ── Step 3: LLM 评估 ──
    evals, insights, optimization_actions = evaluate_all(records, action_history_text)

    # ── Step 4: 统计 + 对比 ──
    stats = _compute_stats(evals, records)
    max_record_id = max(r.id for r in records)

    comparison = None
    if prev_iteration:
        prev_metrics = prev_iteration.get("metrics") or {}
        cur_metrics = {
            "mean_overall": stats["mean_overall"],
            "dim_means": stats["dim_means"],
            "bad_case_count": len(stats.get("bad_cases") or []),
        }
        comparison = compare_chat_iterations(
            {"iteration_id": prev_iteration.get("iteration_id", ""), "metrics": prev_metrics,
             "optimization_actions": prev_iteration.get("optimization_actions") or []},
            {"iteration_id": "current", "metrics": cur_metrics,
             "optimization_actions": optimization_actions},
        )

    # ── Step 5: 保存迭代记录 ──
    now = datetime.now(timezone.utc)
    iid = now.strftime("%Y%m%dT%H%M%SZ")
    record = {
        "iteration_id": iid,
        "created_at_utc": now.isoformat().replace("+00:00", "Z"),
        "review_days": days,
        "sample_count": stats["total"],
        "total_available": total_available,
        "max_record_id": max_record_id,
        "metrics": {
            "mean_overall": stats["mean_overall"],
            "dim_means": stats["dim_means"],
            "bad_case_count": len(stats.get("bad_cases") or []),
            "insights_count": len(insights),
            "actions_count": len(optimization_actions),
        },
        "insights": insights,
        "optimization_actions": [
            {**a, "status": "pending", "generated_at": iid}
            for a in optimization_actions
        ],
        "comparison_with_previous": comparison,
        "bad_case_sample": [
            {"record_id": bc.get("record_id"), "overall": bc.get("overall"),
             "issues": bc.get("issues"), "suggestion": bc.get("suggestion")}
            for bc in (stats.get("bad_cases") or [])[:5]
        ],
    }
    append_chat_iteration(record)

    # ── Step 6: 生成报告 ──
    report_md = build_report(stats, insights, optimization_actions, comparison, days, limit)

    # ── Step 7: 推送 ──
    pushed = False
    if push:
        pushed = _push_report(report_md)
        logger.info("企微推送: %s", "✅" if pushed else "❌")

    return {
        "iteration_id": iid,
        "total": stats["total"],
        "mean_overall": stats["mean_overall"],
        "bad_case_count": len(stats.get("bad_cases") or []),
        "dim_means": stats["dim_means"],
        "insights_count": len(insights),
        "actions_count": len(optimization_actions),
        "improved": comparison.get("improved") if comparison else None,
        "report": report_md,
        "pushed": pushed,
    }


# ── CLI ───────────────────────────────────────────────
def _print_history(limit: int = 5) -> None:
    """打印优化历史。"""
    iterations = list_chat_iterations(limit=limit)
    if not iterations:
        print("暂无优化历史记录")
        return

    print(f"# 对话质量优化历史（最近 {len(iterations)} 轮）\n")
    for it in iterations:
        iid = it.get("iteration_id", "?")
        m = it.get("metrics") or {}
        mean = m.get("mean_overall", "?")
        dims = m.get("dim_means") or {}
        print(f"## {iid}")
        print(f"综合: {mean} | 抽样: {it.get('sample_count', '?')} 条 | Bad: {m.get('bad_case_count', '?')}")
        print(f"维度: " + " | ".join(f"{k}:{v}" for k, v in dims.items()))
        print()

        full = load_chat_iteration(iid)
        if full:
            actions = full.get("optimization_actions") or []
            if actions:
                print("优化动作:")
                for a in actions:
                    print(f"  - [{a.get('target_dimension', '?')}] {a.get('action', '')[:80]}")
                print()

            comp = full.get("comparison_with_previous")
            if comp:
                improved = "✅ 改善" if comp.get("improved") else "⚠️ 未改善"
                print(f"对比上轮: {improved}")
                for ar in comp.get("action_results") or []:
                    eff = "✅" if ar.get("effective") else "❌"
                    print(f"  {eff} {ar.get('action', '')[:60]} (Δ{ar.get('dim_delta', 0):+.1f})")
                print()
        print("---")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="对话质量审查 + 循环优化智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"审查最近 N 天（默认 {DEFAULT_DAYS}，周度）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"抽样条数（默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--intent", type=str, default=None, help="仅审查特定意图（逗号分隔）")
    parser.add_argument("--no-push", action="store_true", help="不推送到企微")
    parser.add_argument("--history", action="store_true", help="仅查看优化历史，不执行审查")
    args = parser.parse_args()

    if args.history:
        _print_history()
        return

    intent_filter = None
    if args.intent:
        intent_filter = [s.strip() for s in args.intent.split(",") if s.strip()]

    result = review(
        days=args.days,
        limit=args.limit,
        intent_filter=intent_filter,
        push=not args.no_push,
    )

    # 终端输出
    print(result["report"])
    print(f"\n--- 统计 ---")
    print(f"迭代 ID: {result.get('iteration_id', '?')}")
    print(f"评估条数: {result['total']}")
    print(f"综合均分: {result['mean_overall']}")
    print(f"Bad cases: {result['bad_case_count']}")
    print(f"洞察: {result['insights_count']} 条")
    print(f"优化动作: {result['actions_count']} 条")
    if result.get("improved") is not None:
        print(f"趋势: {'✅ 改善' if result['improved'] else '⚠️ 待改善'}")
    print(f"企微推送: {'✅' if result['pushed'] else '❌'}")


if __name__ == "__main__":
    main()
