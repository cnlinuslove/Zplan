"""
微信侧「发一句话 → 返回回复」的轻量意图解析。

设计给 OpenClaw / 中间件调用：微信收消息入口仍在编排层，本模块负责
根据用户文本生成回复（`reply_text` / `reply_markdown`）+ 可选的模板卡片按钮。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))

from agents.info_query import answer_info_question
from agents.news_agent import get_history_payload
from chat_session import add_message, session_active, touch_session, expire_session
from claude_tasks import queue as claude_task_queue
from config import CHAT_HISTORY_ENABLED
from models import init_db
from pick_wechat import try_handle_pick
from topic_admin import list_topics
from wechat_limits import WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8
from zplan_shared.models import (
    ChatHistory,
    DailyFeature,
    DailyPrice,
    DailySnapshot,
    MarketForecast,
    PickEntry,
    PickRun,
    SessionLocal,
    StockConceptMember,
    StockList,
)

# ── 股票名快速识别（用于直接路由，绕过 Brain 减少 LLM 往返）───
_STOCK_CODE_RE = re.compile(r"^[0368]\d{5}$")
_QUESTION_MARKERS = re.compile(
    r"(怎么|如何|为什么|为何|什么|哪|吗|？|\?|最近|会不会|能不能|多少|是否|展望|影响)"
)
_IGNORED_SIMPLE = frozenset({
    "帮助", "最新", "7天", "列表", "退出", "结束", "help", "latest",
})

# 批量分析检测：识别 "分析 XXX · 分析 YYY" 或 "分析 XXX 分析 YYY" 等多票请求
_BATCH_PICK_RE = re.compile(
    r"(?:分析|选股|研报|打分)\s*[：:\s]*([一-鿿]{2,6}|[0368]\d{5})",
    re.IGNORECASE,
)
_BATCH_SEPARATOR = re.compile(r"\s*[·•,，、\n]+\s*")


def _split_batch_queries(text: str) -> list[str]:
    """识别批量分析请求，返回各个股票名/代码列表。至少 2 个才算批量。"""
    raw = text.strip()

    # 方法1: 先尝试匹配所有 "指令词 + 股票名" 对
    matches = list(_BATCH_PICK_RE.finditer(raw))
    if len(matches) >= 2:
        return [m.group(1) for m in matches]

    # 方法2: 如果包含分隔符，按分隔符拆分后看每段是否像股票名
    if _BATCH_SEPARATOR.search(raw):
        parts = _BATCH_SEPARATOR.split(raw)
        queries = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 剥离可能的指令词前缀
            sub_m = _BATCH_PICK_RE.search(part)
            if sub_m:
                queries.append(sub_m.group(1))
            else:
                code_m = _STOCK_CODE_RE.match(part)
                name_m = re.match(r"^[一-鿿]{2,6}$", part)
                if code_m or name_m:
                    queries.append(part)
        if len(queries) >= 2:
            return queries

    return []


def _looks_like_stock_query(text: str) -> bool:
    """简单股票名/代码判定，误判会由 try_handle_pick 返回 None 兜底。"""
    s = text.strip()
    if not s or len(s) > 16 or s in _IGNORED_SIMPLE or _QUESTION_MARKERS.search(s):
        return False
    return bool(_STOCK_CODE_RE.match(s) or re.match(r"^[一-鿿]{2,8}$", s))


HELP_TEXT = """【Z-Plan 功能说明】

━━━ 📊 选股分析 ━━━
• 爱普股份 → 直接发名称或代码，LLM 深度分析
• 分析 爱普股份 → 强制重跑完整选股（含 PDF 研报）
• 603987 新闻 → 个股关联快讯
• 生成爱普股份分析报告 → 深度研究报告

━━━ 📋 选股清单 ━━━
• 选股清单 / 今日推荐 → TOP10 表格（评分·涨跌·PE·距高·买入价）
  ┗ 含操作建议 + 一键研报快捷指令

━━━ 📈 回测验证 ━━━
• 回测结果 / 选股表现 → 买入价触及率 · 目标价命中 · 失败率
  ┗ 含校准数据 + 最近迭代记录 + 优化建议

━━━ 🔮 大盘预测 ━━━
• 大盘预测 / 市场预测 → 多空研判 + 多空对照 + 指数全景 + 板块方向

━━━ 📦 批量分析 ━━━
• 分析 爱普股份 · 分析 平安银行 · 分析 品高股份
  ┗ 每只独立生成 PDF 研报，逐只推送到群

━━━ ⭐ 自选管理 ━━━
• 加入自选 爱普股份 → 添加关注
• 我的自选 → 查看自选列表（含行情快照）
• 移除自选 爱普股份 → 从清单删除

━━━ 💼 持仓管理 ━━━
• 买入 爱普股份 1000股 12.50 → 记录成本
• 卖出 爱普股份 → 移除持仓
• 我的持仓 → 全部仓位 + 盈亏估算

━━━ ⚖️ 股票对比 ━━━
• 对比 爱普股份 和 平安银行 → 并排比较评分·PE·涨跌·概念·风险

━━━ 🤖 Claude 远程任务 ━━━
• claude 优化选股报告格式 → 提交远程任务
  ┗ Claude 出方案 → 推送审批 → 回复「可以」执行 → 完成推送
• 可以 / 好的 / go → 批准方案（可附带反馈）
• 取消 / 算了 → 取消任务
• 换个方案 + 新要求 → 重新出方案

━━━ 📰 资讯查询 ━━━
• 最新 / 7天 → 近期快讯摘要
• 北向资金最近走势？ → 直接提问
• 筛选 脑机接口 → 题材成份股

━━━ 🔧 其它 ━━━
• 列表 → 全部资讯 Topic
• 帮助 → 本说明
• 退出 → 结束当前会话窗口"""

HELP_MARKDOWN = f"### Z-Plan\n> {HELP_TEXT.replace(chr(10), chr(10) + '> ')}"

# ── 混合路由：活跃会话中保持规则优先的明确指令 ──────────────
# 匹配这些模式的消息即使在活跃会话中也走规则路由（不交给 Brain）
_SESSION_RULE_FIRST = re.compile(
    r"^(帮助|help|\?|？|退出|exit|quit|结束|结束会话|关闭|"
    r"买入\s+|卖出\s+|"
    r"加入自选\s+|添加自选\s+|关注\s+|"
    r"移除自选\s+|删除自选\s+|取消关注\s+|"
    r"claude[\s,]|@claude[\s,]|"
    r"选股\s*[：:\s]|分析\s*[：:\s]|打分\s*[：:\s]|研报\s*[：:\s]|评股\s*[：:\s]|查股\s*[：:\s]|评分\s*[：:\s]|"
    r"筛选\s*[：:\s]|题材\s*[：:\s]|概念\s*[：:\s]"
    r")",
    re.IGNORECASE,
)

# 非 @ 消息且无活跃会话时的提示
SESSION_REQUIRED_TEXT = (
    "请 @我 发起对话，之后 2 小时内可直接发消息，无需再次 @。\n"
    "发送「帮助」查看功能列表。"
)

# 会话窗口激活后的提示（仅首次，追加在回复末尾）
SESSION_ACTIVE_HINT = "\n\n💡 接下来 {} 分钟内，直接发消息即可，无需 @我。"


# ── Claude 方案审批 ──────────────────────────────────────

# 审批词根（按长度降序，优先匹配长词如 "go ahead" > "go"）
_APPROVAL_ROOTS: tuple[str, ...] = tuple(sorted([
    "go ahead", "do it", "没问题", "approve", "okay",
    "可以", "ok", "行", "好", "yes", "y", "执行", "go", "批准",
    "通过", "同意", "确认", "开始", "搞", "干", "动手", "run",
    "好的", "好滴", "好嘞", "准", "批", "可",
], key=len, reverse=True))

# 取消词
_CANCEL_WORDS: frozenset[str] = frozenset({
    "取消", "cancel", "算了", "不要", "放弃", "abort", "撤销", "作废", "撤回",
    "不做了", "别做了", "不搞了", "stop", "abort",
})

# 改方案前缀（按长度降序）
_REVISE_PREFIXES: tuple[str, ...] = tuple(sorted([
    "重新设计方案", "不要这个方案", "重新出方案", "换个方案", "修改方案",
    "重新考虑", "换个思路", "方案不好", "改方案", "方案改", "重新想",
    "重出", "再想",
], key=len, reverse=True))


def _try_handle_approval(
    raw: str,
    low: str,
    *,
    chat_id: str = "",
) -> dict[str, Any] | None:
    """检测用户是否在审批 Claude 方案。

    识别逻辑：
      1. 必须有 plan_ready 状态的任务等待审批
      2. 消息以审批词根开头 → 审批通过，剩余部分为反馈
      3. 消息精确匹配取消词 → 取消任务
      4. 消息以改方案前缀开头 → 取消旧任务，创建新任务

    返回 reply_payload 表示已处理（拦截后续路由），返回 None 表示不是审批消息。
    """
    if not chat_id:
        return None

    # ── 1. 查找待审批任务 ──
    plan_task = claude_task_queue.get_latest_plan_ready(chat_id=chat_id)
    if plan_task is None:
        return None

    task_id = plan_task["id"]
    tid_short = task_id[:8]

    # ── 2. 取消 ──
    if low.strip() in _CANCEL_WORDS:
        claude_task_queue.cancel_task(task_id, "用户取消")
        return _reply_payload(
            "claude_approval",
            f"❌ 任务已取消\n\nID: `{tid_short}…`\n"
            f"原任务: {plan_task['text'][:100]}",
        )

    # ── 3. 改方案（回到 pending 重新出方案）──
    for prefix in _REVISE_PREFIXES:
        if low.startswith(prefix):
            new_requirement = raw[len(prefix):].strip().lstrip("：:。.！!，, ")
            new_text = plan_task["text"]
            if new_requirement:
                new_text = f"{new_text}（补充要求：{new_requirement}）"
            claude_task_queue.cancel_task(
                task_id,
                f"用户要求改方案: {new_requirement}" if new_requirement else "用户要求改方案",
            )
            new_task = claude_task_queue.create_task(
                text=new_text,
                user_id=plan_task.get("user_id", ""),
                chat_id=chat_id,
            )
            return _reply_payload(
                "claude_approval",
                f"🔄 已重新入队，按新要求生成方案\n\n"
                f"新任务 ID: `{new_task['id'][:8]}…`\n"
                f"内容: {new_text[:120]}",
            )

    # ── 4. 审批通过（匹配审批词根，剩余部分为反馈）──
    approval_root: str | None = None
    for root in _APPROVAL_ROOTS:
        if low.startswith(root):
            approval_root = root
            break

    if approval_root is None:
        return None

    # 提取反馈（审批词根之后的内容，去除前导标点/空格）
    rest = raw[len(approval_root):].strip()
    # 去除前导标点符号和语气词
    rest = rest.lstrip("，,。.；;：:！!、 \t\n\r")
    # 去除常见的无意义语气词前缀
    for filler in ("的", "滴", "嘞", "吧", "啦", "哦", "嗯"):
        if rest.startswith(filler):
            rest = rest[1:].lstrip("，,。.；;：:！!、 \t\n\r")

    feedback = rest if rest else ""
    claude_task_queue.approve_task(task_id, feedback)

    if feedback:
        return _reply_payload(
            "claude_approval",
            f"✅ 方案已批准（附带反馈）\n\n"
            f"ID: `{tid_short}…`\n"
            f"反馈: {feedback[:150]}\n\n"
            f"⏳ Claude 将在 {_next_poll_eta()} 内开始执行",
        )
    else:
        return _reply_payload(
            "claude_approval",
            f"✅ 方案已批准，开始执行\n\n"
            f"ID: `{tid_short}…`\n\n"
            f"⏳ Claude 将在 {_next_poll_eta()} 内开始执行\n"
            f"📊 完成后企微推送结果",
        )


# ── 模板卡片 ──────────────────────────────────────────────

def _make_card(
    title: str,
    desc: str,
    buttons: list[dict[str, Any]],
) -> dict[str, Any]:
    """构建 button_interaction 模板卡片。"""
    return {
        "card_type": "button_interaction",
        "main_title": {"title": title, "desc": desc},
        "task_id": f"card_{int(time.time() * 1000)}_{id(buttons)}",
        "button_list": buttons,
    }


def _parse_llm_brief(analysis_json: str | None) -> str:
    """从 analysis_process_json 中提取 LLM 简评趋势段，做安全兜底。"""
    if not analysis_json:
        return ""
    try:
        data = json.loads(analysis_json)
        brief = data.get("llm_brief", {})
        if isinstance(brief, dict):
            trend = brief.get("trend", "")
            return str(trend).strip()
        return ""
    except (json.JSONDecodeError, TypeError, KeyError):
        return ""


def _parse_llm_brief_rich(analysis_json: str | None) -> dict[str, Any]:
    """提取 LLM 简评的全部结构化字段：trend / recommendation / risk_flags / positive_flags。"""
    if not analysis_json:
        return {}
    try:
        data = json.loads(analysis_json)
        brief = data.get("llm_brief", {})
        if isinstance(brief, dict):
            return {
                "trend": str(brief.get("trend", "")).strip(),
                "recommendation": brief.get("recommendation"),
                "risk_flags": brief.get("risk_flags", []),
                "positive_flags": brief.get("positive_flags", []),
                "confidence_adjustment": brief.get("confidence_adjustment"),
            }
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _pick_top_concepts(session, ts_code: str, limit: int = 3) -> str:
    """取某股票的前几个概念标签，用 · 分隔。需传入已有 session。"""
    rows = session.execute(
        select(StockConceptMember.concept_name)
        .where(StockConceptMember.ts_code == ts_code)
        .limit(limit * 2)  # 多取一点用于过滤
    ).scalars().all()

    if not rows:
        return ""

    # 过滤掉过于泛化的标签
    skip = {
        "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
        "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
        "央国企改革", "黑龙江", "深圳特区", "机械设备", "通信", "电子", "计算机",
        "公用事业", "电力", "基础化工", "化学制品", "元件", "通信技术", "通信设备",
    }
    concepts = [r for r in rows if r not in skip]
    return " · ".join(concepts[:limit])


# ── 大盘预测 ──────────────────────────────────────────────

def get_latest_forecast() -> dict[str, Any]:
    """最新大盘预测：综合方向 + 多空对照 + 选股参考。"""
    init_db()
    with SessionLocal() as session:
        mf = session.execute(
            select(MarketForecast)
            .order_by(desc(MarketForecast.created_at_utc))
            .limit(1)
        ).scalars().first()

        if not mf:
            return _reply_payload(
                "forecast",
                "暂无大盘预测数据。\n请先运行 market_forecast.py。",
            )

        try:
            f = json.loads(mf.forecast_json) if isinstance(mf.forecast_json, str) else mf.forecast_json
        except (json.JSONDecodeError, TypeError):
            return _reply_payload("forecast", "预测数据格式错误，请稍后重试。")

        md = f.get("market_direction", {})
        direction_map = {"bullish": "🟢 看涨", "bearish": "🔴 看跌", "range-bound": "🟡 震荡"}
        direction_label = direction_map.get(md.get("direction", ""), md.get("direction", "?"))

        lines = [
            f"🔮 大盘预测 · {mf.as_of_date}",
            "",
            f"**综合判断: {direction_label}**（置信度 {md.get('confidence', '?')}%）",
            f"> {md.get('reasoning', '')}",
            "",
        ]

        # 多空对照
        evidence = md.get("evidence") or []
        counter = md.get("counter_evidence") or []
        if evidence or counter:
            lines.append("**⚖️ 多空对照**")
            for e in evidence:
                lines.append(f"🔺 [{e.get('type', '')}] {e.get('signal', '')}: {e.get('value', '')}")
            for c in counter:
                lines.append(f"🔻 {c}")
            lines.append("")

        # 指数全景
        ix_forecasts = f.get("index_forecasts") or []
        if ix_forecasts:
            bullish_ix = [ix for ix in ix_forecasts if ix.get("direction") == "偏多"]
            bearish_ix = [ix for ix in ix_forecasts if ix.get("direction") == "偏空"]
            neutral_ix = [ix for ix in ix_forecasts if ix.get("direction") == "震荡"]
            lines.append(
                f"🏛️ 指数: 🔺偏多{len(bullish_ix)}只 ➖震荡{len(neutral_ix)}只 🔻偏空{len(bearish_ix)}只"
            )
            for ix in ix_forecasts:
                emoji = {"偏多": "🔺", "偏空": "🔻", "震荡": "➖"}.get(ix.get("direction", ""), "❓")
                sp = ix.get("similar_patterns_verdict", "")
                sp_str = f" · 历史相似: {sp}" if sp else ""
                lines.append(f"  {emoji} {ix.get('name', '')}: {ix.get('direction', '')}（置信{ix.get('confidence', '?')}%）{sp_str}")
            lines.append("")

        # 板块
        sectors = f.get("sector_calls") or []
        if sectors:
            lines.append("🏭 板块判断:")
            for s in sectors:
                emoji = {"看多": "🟢", "看淡": "🔴", "中性": "⚪"}.get(s.get("direction", ""), "")
                lines.append(f"  {emoji} {s.get('sector', '')}: {s.get('reasoning', '')[:60]}")
            lines.append("")

        # 选股参考
        dir_signal = md.get("direction", "")
        pick_guide = {
            "bullish": "🟢 偏多 → 可积极选股，重点看偏多指数对应标的",
            "bearish": "🔴 偏空 → 降低仓位防守，关注逆势板块",
            "range-bound": "🟡 震荡 → 控制仓位精选个股，不追高",
        }.get(dir_signal, "等待更明确信号")
        lines.append(f"📋 选股参考: {pick_guide}")
        lines.append("")
        lines.append("💡 发送「选股清单」查看今日推荐")

        return _reply_payload("forecast", "\n".join(lines))


# ── 选股清单 ──────────────────────────────────────────────

# ── 格式化辅助 ──────────────────────────────────────────────

def _format_compact_table(entries_data: list[dict]) -> list[str]:
    """Part 1: 紧凑型表格（评分 / 今% / 20日% / PE / 收盘 / 距高% / 买入 / 稳定）。"""
    lines = ["| # | 股票 | 评分 | 今% | 20日% | PE | 收盘 | 距高% | 买入 | 稳定 |"]
    lines.append("|---|------|------|------|-------|----|------|-------|------|------|")
    for i, item in enumerate(entries_data):
        name = (item.get("name") or item.get("ts_code", "?"))[:8]
        code = item.get("ts_code", "?")
        score = item.get("score")
        score_s = f"{score:.0f}" if score is not None else "--"
        pct = item.get("pct_chg")
        pct_s = f"{pct:+.1f}%" if pct is not None else "--"
        r20 = item.get("ret_20d")
        r20_s = f"{r20:+.1f}%" if r20 is not None else "--"
        pe = item.get("pe_ttm")
        pe_s = f"{pe:.0f}" if (pe is not None and pe > 0) else "--"
        close = item.get("close")
        close_s = f"¥{close:.2f}" if close is not None else "--"
        ph = item.get("pct_from_high")
        ph_s = f"{ph:+.0f}%" if ph is not None else "--"
        buy = item.get("predicted_buy")
        buy_s = f"¥{buy:.2f}" if (buy is not None and buy > 0) else "--"
        stab = item.get("stability_emoji", "")
        lines.append(
            f"| {i + 1} | {name}({code}) | {score_s} | {pct_s} | {r20_s} | {pe_s} | {close_s} | {ph_s} | {buy_s} | {stab} |"
        )
    lines.append("")
    return lines


def _format_detail_lines(entries_data: list[dict]) -> list[str]:
    """Part 2: 逐只详情行（💡简评 · 🏷概念 · 📉距高 · 动量提示）。"""
    lines = []
    for i, item in enumerate(entries_data):
        parts = []
        # 推荐理由（从 LLM brief 提取）
        reason = item.get("reason", "")
        if reason:
            parts.append(f"💡{reason}")
        # 概念标签
        concepts = item.get("concepts", [])
        if concepts:
            parts.append("🏷 " + "·".join(concepts[:2]))
        # 距高提示
        ph = item.get("pct_from_high")
        if ph is not None and ph < -15:
            parts.append(f"📉距高{ph:.0f}%")
        # 20日动量提示
        r20 = item.get("ret_20d")
        if r20 is not None and abs(r20) > 10:
            emoji = "🔥" if r20 > 0 else "❄️"
            parts.append(f"{emoji}20日{r20:+.0f}%")
        # vol_ratio20 提示
        vol_ratio = item.get("vol_ratio20")
        if vol_ratio is not None and vol_ratio > 1.5:
            parts.append(f"📊量比{vol_ratio:.1f}")
        # 稳定性警告
        stab_std = item.get("stability_std")
        stab_label = item.get("stability_label")
        stab_slope = item.get("stability_slope")
        if stab_std is not None and stab_std >= 12:
            slope_note = f"趋势↓{stab_slope:.0f}/d" if stab_slope is not None and stab_slope < -1 else ""
            parts.append(f"⚠️分数波动({stab_label}{' ' + slope_note if slope_note else ''})")

        nm = item.get("name") or item.get("ts_code", "?")
        if parts:
            lines.append(f"{i + 1}. {nm} {' · '.join(parts)}")
        else:
            industry = item.get("industry", "")
            lines.append(f"{i + 1}. {nm} 📌{industry}" if industry else f"{i + 1}. {nm}")

    return lines


def _format_operation_advice(entries_data: list[dict], direction: str) -> list[str]:
    """Part 3: 操作建议（大盘方向 + 低PE防御 + 今日逆势 + 止损）。"""
    direction_map = {
        "bullish": ("🟢 大盘偏多", "可以积极选股，优先强势板块", "建议买入价: 昨收×0.98"),
        "bearish": ("🔴 大盘偏空", "⚠️ 建议观望，等止跌信号", "若要入场: 买入价收紧到昨收×0.96"),
        "range-bound": ("🟡 大盘震荡", "仓位≤5成，等回调不追高", "建议买入价: 昨收×0.99"),
    }
    d_info = direction_map.get(direction, ("❓ 方向不明", "等待更明确信号", ""))

    lines = ["", "💡 操作建议", ""]
    lines.append(f"**{d_info[0]}** → {d_info[1]}")
    lines.append(f"- {d_info[2]}")

    # 低PE防御
    low_pe = [e for e in entries_data if e.get("pe_ttm") and e["pe_ttm"] > 0 and e["pe_ttm"] < 30]
    if low_pe:
        names = [f"{e['name']}(PE{e['pe_ttm']:.0f})" for e in low_pe[:3]]
        lines.append(f"- 低PE防御: {', '.join(names)}")

    # 今日逆势（涨幅为正的个股）
    up_stocks = [e for e in entries_data if e.get("pct_chg") is not None and e["pct_chg"] > 0]
    if up_stocks:
        names = [f"{e['name']}(+{e['pct_chg']:.1f}%)" for e in up_stocks[:3]]
        lines.append(f"- 今日逆势: {', '.join(names)}")

    lines.append("- 止损: 个股支撑位下方 1-2%")
    return lines


def _format_report_links(entries_data: list[dict]) -> list[str]:
    """Part 4: 一键深度研报快捷指令。"""
    lines = ["", "---", "📊 一键深度研报", ""]
    cmd_parts = [f"分析 {e.get('name', '?')}" for e in entries_data]
    for i in range(0, len(cmd_parts), 5):
        lines.append("> " + " · ".join(cmd_parts[i : i + 5]))
    return lines


def _format_picks_markdown(
    entries_data: list[dict],
    as_of: str,
    rule_version: str,
    llm_enabled: bool = False,
    direction: str = "",
) -> str:
    """编排四段式 TOP10 报告。"""
    today_str = datetime.now(BEIJING_TZ).strftime("%m-%d")
    lines = [f"【今日选股 TOP10】{today_str}"]
    llm_tag = " · LLM" if llm_enabled else ""
    lines.append(f"数据截止 {as_of}  |  规则 {rule_version}{llm_tag}")
    lines.append("")

    if not entries_data:
        lines.append("⚠️ 暂无选股数据")
        return "\n".join(lines)

    # Part 1: 紧凑表格
    lines.extend(_format_compact_table(entries_data))

    # Part 2: 逐只详情
    lines.extend(_format_detail_lines(entries_data))

    # Part 3: 操作建议
    lines.extend(_format_operation_advice(entries_data, direction))

    # Part 4: 一键研报
    lines.extend(_format_report_links(entries_data))

    return "\n".join(lines)


# ── 主入口 ──────────────────────────────────────────────

def get_latest_picks() -> dict[str, Any]:
    """最新选股清单：紧凑表格 + 详情行 + 操作建议 + 一键研报。"""
    init_db()
    with SessionLocal() as session:
        run = session.execute(
            select(PickRun)
            .where(PickRun.run_kind.in_(["scan", "llm_top300"]))
            .order_by(desc(PickRun.created_at_utc))
            .limit(1)
        ).scalars().first()

        if not run:
            return _reply_payload(
                "picks_list",
                "暂无选股运行记录。\n请先运行选股扫描。",
            )

        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run.id)
            .order_by(
                PickEntry.rank_in_run,
                PickEntry.final_composite_score.desc().nullslast(),
            )
            .limit(10)
        ).scalars().all()

        if not entries:
            return _reply_payload(
                "picks_list",
                "选股记录存在但无有效条目。",
            )

        codes = [e.ts_code for e in entries]
        as_of_date = run.trade_date_as_of

        # ── 行业 ──
        industry_map: dict[str, str] = {}
        if codes:
            rows = session.execute(
                select(StockList.ts_code, StockList.industry)
                .where(StockList.ts_code.in_(codes))
            ).all()
            industry_map = {r.ts_code: (r.industry or "") for r in rows}

        # ── 当日行情（涨跌幅、收盘价）──
        pct_map: dict[str, float | None] = {}
        close_map: dict[str, float | None] = {}
        if codes and as_of_date:
            rows = session.execute(
                select(DailyPrice.ts_code, DailyPrice.close, DailyPrice.pct_chg)
                .where(
                    DailyPrice.ts_code.in_(codes),
                    DailyPrice.trade_date == as_of_date,
                )
            ).all()
            pct_map = {r.ts_code: r.pct_chg for r in rows}
            close_map = {r.ts_code: r.close for r in rows}

        # ── 20日涨跌 + 距高% + 量比（DailyFeature）──
        ret_20d_map: dict[str, float | None] = {}
        high_dist_map: dict[str, float | None] = {}
        vol_ratio_map: dict[str, float | None] = {}
        if codes and as_of_date:
            feat_rows = session.execute(
                select(
                    DailyFeature.ts_code,
                    DailyFeature.ret_20d,
                    DailyFeature.high_60d_pct,
                    DailyFeature.vol_ratio20,
                )
                .where(
                    DailyFeature.ts_code.in_(codes),
                    DailyFeature.trade_date == as_of_date,
                )
            ).all()
            for r in feat_rows:
                ret_20d_map[r[0]] = r[1]
                # high_60d_pct 是 close/high_60*100，距高 = pct - 100
                if r[2] is not None:
                    high_dist_map[r[0]] = r[2] - 100
                vol_ratio_map[r[0]] = r[3]

        # ── PE（DailySnapshot 最新日期）──
        pe_map: dict[str, float | None] = {}
        if codes:
            from sqlalchemy import func as sqlfunc
            latest_snap = session.execute(
                select(sqlfunc.max(DailySnapshot.trade_date))
            ).scalar()
            if latest_snap:
                snap_rows = session.execute(
                    select(DailySnapshot.ts_code, DailySnapshot.pe_ttm)
                    .where(
                        DailySnapshot.ts_code.in_(codes),
                        DailySnapshot.trade_date == latest_snap,
                    )
                ).all()
                pe_map = {r[0]: r[1] for r in snap_rows}

        # ── 概念 ──
        concept_map: dict[str, list[str]] = {}
        if codes:
            _SKIP_CONCEPTS = {
                "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
                "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
                "央国企改革", "黑龙江", "深圳特区", "机械设备", "通信", "电子", "计算机",
                "公用事业", "电力", "基础化工", "化学制品", "元件", "通信技术", "通信设备",
            }
            c_rows = session.execute(
                select(StockConceptMember.ts_code, StockConceptMember.concept_name)
                .where(StockConceptMember.ts_code.in_(codes))
            ).all()
            for cr in c_rows:
                if cr[1] not in _SKIP_CONCEPTS:
                    concept_map.setdefault(cr[0], []).append(cr[1])

        # ── 分数稳定性 ──
        stability_map: dict[str, dict[str, Any]] = {}
        try:
            from zplan_shared.models import ScoreStabilitySnapshot
            if codes and as_of_date:
                stab_rows = session.execute(
                    select(ScoreStabilitySnapshot).where(
                        ScoreStabilitySnapshot.ts_code.in_(codes),
                        ScoreStabilitySnapshot.trade_date == as_of_date,
                        ScoreStabilitySnapshot.lookback_days == 10,
                    )
                ).scalars().all()
                for sr in stab_rows:
                    from pick_agent.stability import classify_stability
                    tier = classify_stability(sr.score_std_10d)
                    stability_map[sr.ts_code] = {
                        "std_10d": sr.score_std_10d,
                        "slope_5d": sr.score_slope_5d,
                        "slope_10d": sr.score_slope_10d,
                        "range_10d": sr.score_range_10d,
                        "flips": sr.score_direction_flips,
                        "rank_std": sr.rank_stability_10d,
                        "emoji": tier["emoji"],
                        "label": tier["label"],
                    }
        except ImportError:
            pass  # stability 模块未安装

        # ── 大盘方向 ──
        direction = ""
        mf = session.execute(
            select(MarketForecast)
            .order_by(desc(MarketForecast.created_at_utc))
            .limit(1)
        ).scalars().first()
        if mf:
            direction = mf.market_direction or ""

        # ── 组装 entries_data ──
        entries_data: list[dict[str, Any]] = []
        for e in entries:
            code = e.ts_code
            # 提取 LLM 简评（结构化）
            brief = _parse_llm_brief_rich(e.analysis_process_json)
            reason = brief.get("trend", "")
            # 如果 trend 为空，尝试提取第一句作为简评
            if not reason:
                reason = _parse_llm_brief(e.analysis_process_json)

            stab = stability_map.get(code, {})
            entries_data.append({
                "ts_code": code,
                "name": e.name or code,
                "score": e.final_composite_score or e.rule_composite_score,
                "close": close_map.get(code) or e.close_price,
                "pct_chg": pct_map.get(code),
                "ret_20d": ret_20d_map.get(code),
                "pe_ttm": pe_map.get(code),
                "pct_from_high": high_dist_map.get(code),
                "vol_ratio20": vol_ratio_map.get(code),
                "predicted_buy": e.predicted_buy_price,
                "industry": industry_map.get(code, ""),
                "concepts": concept_map.get(code, [])[:2],
                "reason": reason,
                # 稳定性
                "stability_emoji": stab.get("emoji", ""),
                "stability_label": stab.get("label", ""),
                "stability_std": stab.get("std_10d"),
                "stability_slope": stab.get("slope_5d"),
                "stability_range": stab.get("range_10d"),
            })

    as_of = (
        run.trade_date_as_of.strftime("%m-%d")
        if run.trade_date_as_of
        else run.created_at_utc.strftime("%m-%d %H:%M")
    )

    markdown = _format_picks_markdown(
        entries_data,
        as_of=as_of,
        rule_version=run.rule_version or "",
        llm_enabled=bool(run.llm_enabled),
        direction=direction,
    )

    today_str = datetime.now(BEIJING_TZ).strftime("%m-%d")
    card = _make_card(
        title=f"今日选股 TOP10 · {today_str}",
        desc=f"数据截止 {as_of} · Top {len(entries_data)} · 规则 {run.rule_version}",
        buttons=[
            {"text": "刷新清单", "style": 1, "key": "picklist"},
            {"text": "分析某股", "style": 0, "key": "picklist_analyze"},
        ],
    )
    return _reply_payload("picks_list", markdown, card=card)


# ── 回测结果 ──────────────────────────────────────────────

def get_latest_backtest() -> dict[str, Any]:
    """最新回测验证：选股命中率 / 买入价触及率 / 迭代优化建议。"""
    init_db()
    try:
        from zplan_shared.pick_predictions import calibration_summary
        from zplan_shared.pick_iterate_store import list_iterations

        cal = calibration_summary(horizon_days=10)
        iters = list_iterations(limit=5)

        today_str = datetime.now(BEIJING_TZ).strftime("%m-%d")
        lines = [f"【选股回测验证】{today_str}", ""]

        # ── Part 1: 校准摘要 ──
        if cal.get("count", 0) > 0:
            lines.append("━━━ 📐 预测校准（10日窗口）━━━")
            lines.append(f"• 验证样本: {cal['count']} 条")
            touch_rate = cal.get("touch_rate", 0)
            touch_emoji = "✅" if touch_rate >= 0.6 else ("⚠️" if touch_rate >= 0.4 else "🔴")
            lines.append(
                f"• 买入价触及率: {touch_emoji} {touch_rate:.1%}  "
                f"（{4 - int(touch_rate * 5)}/5 — 期内最低价达到建议买入价的比例）"
            )
            mean_gap = cal.get("mean_buy_gap_pct")
            if mean_gap is not None:
                gap_label = "偏高" if mean_gap > 1 else ("偏低" if mean_gap < -2 else "适中")
                lines.append(f"• 买价均价差: {mean_gap:+.2f}%（{gap_label}，正=买价低于最低价→买不到）")
            target_rate = cal.get("target_hit_rate", 0)
            if target_rate is not None:
                lines.append(f"• 目标价命中: {target_rate:.1%}（达到止盈线的比例）")
            stop_rate = cal.get("stop_hit_rate", 0)
            if stop_rate is not None:
                lines.append(f"• 止损触发: {stop_rate:.1%}（跌破止损线的比例）")
            mean_ret = cal.get("mean_return_from_buy_pct")
            if mean_ret is not None:
                ret_emoji = "🟢" if mean_ret > 0 else "🔴"
                lines.append(f"• 均收益（从买价）: {ret_emoji} {mean_ret:+.2f}%")
            lines.append("")

            # 按价格来源分拆
            by_source = cal.get("by_price_source") or []
            if by_source:
                lines.append("按买入价来源:")
                for s in by_source[:4]:
                    label = {"rule": "规则计算", "stored": "存储值", "rule_recomputed": "规则重算"}.get(
                        s.get("price_source", ""), s.get("price_source", "?")
                    )
                    tr = s.get("touch_rate", 0)
                    mg = s.get("mean_gap_pct")
                    lines.append(
                        f"  {label}: {s['n']}条 触及{tr:.0%} "
                        f"均价差{mg:+.1f}%" if mg is not None else f"  {label}: {s['n']}条 触及{tr:.0%}"
                    )
                lines.append("")

        else:
            lines.append("⚠️ 暂无校准数据，请先运行回测验证")
            lines.append("")

        # ── Part 2: 最近迭代 ──
        if iters:
            lines.append("━━━ 🔄 最近迭代记录 ━━━")
            for it in iters[:3]:
                iter_id = it.get("iteration_id", "")[:16]
                fail_rate = it.get("fail_rate")
                metrics = it.get("metrics", {})
                if isinstance(metrics, str):
                    try:
                        import ast
                        metrics = ast.literal_eval(metrics)
                    except Exception:
                        metrics = {}
                fail_count = metrics.get("fail_count", metrics.get("fail_rate"))
                pass_count = metrics.get("pass_count")
                mean_rule = metrics.get("mean_rule")
                mean_llm = metrics.get("mean_llm")
                mean_fwd = metrics.get("mean_fwd_return")

                fr_str = f"{fail_rate:.0%}" if isinstance(fail_rate, (int, float)) else str(fail_rate)
                status_emoji = "✅" if (isinstance(fail_rate, (int, float)) and fail_rate < 0.5) else "⚠️"
                lines.append(
                    f"{status_emoji} {iter_id}  "
                    f"失败率 {fr_str}"
                )
                if pass_count is not None and fail_count is not None:
                    lines.append(f"   通过 {pass_count} / 失败 {fail_count}")
                if mean_rule is not None and mean_llm is not None:
                    delta = mean_llm - mean_rule
                    lines.append(f"   规则均分 {mean_rule:.1f}  LLM均分 {mean_llm:.1f}  (Δ{delta:+.1f})")
                if mean_fwd is not None:
                    lines.append(f"   前向收益 {mean_fwd:+.2f}%")

                tags = it.get("top_failure_tags") or []
                if tags:
                    lines.append(f"   🏷 失败模式: {', '.join(str(t) for t in tags[:3])}")
                lines.append("")

        # ── Part 3: 优化建议 ──
        hints = cal.get("optimization_hints") or []
        if hints:
            lines.append("━━━ 💡 优化建议 ━━━")
            for h in hints:
                lines.append(f"• {h}")
            lines.append("")

        lines.append("💡 发送「选股清单」查看最新推荐 | 「大盘预测」看方向")

        return _reply_payload("backtest", "\n".join(lines))

    except Exception as e:
        logger.exception("获取回测数据失败")
        return _reply_payload("backtest", f"回测数据获取失败: {e}")


# ── 路由核心 ──────────────────────────────────────────────

def _normalize_user_text(message: str) -> str:
    """去掉企微 @机器人 前缀。"""
    raw = (message or "").strip()
    if not raw:
        return raw
    raw = re.sub(r"^@\S+\s*", "", raw, count=1).strip()
    return raw or (message or "").strip()


_POLL_INTERVAL_SECONDS = 60


def _next_poll_eta() -> str:
    """返回轮询器下次检查的预估时间（人性化）。"""
    return f"最多 {_POLL_INTERVAL_SECONDS} 秒"


# ── 上下文功能提示 ──────────────────────────────────────────

_HINT_MAP: dict[str, str] = {
    "pick": "💡 试试：「加入自选」收藏 | 「对比 A 和 B」横比 | 「买入 1000股 价格」记录持仓",
    "pick_symbol": "💡 试试：「加入自选」收藏 | 「对比 A 和 B」横比 | 「买入 1000股 价格」记录持仓",
    "pick_screen": "💡 试试：「选股 名称」个股分析 | 「对比 A 和 B」并排比较 | 「筛选 题材」选标的",
    "picks_list": "💡 试试：「选股 名称」个股分析 | 「对比 A 和 B」横比 | 点击下方按钮快捷操作",
    "forecast": "💡 试试：「选股清单」看推荐 | 发股票名深度分析 | 「筛选 题材名」选标的",
    "watchlist": "💡 试试：「选股 名称」分析自选 | 「我的持仓」仓位 | 「对比 A B」比较两股",
    "watch_add": "💡 试试：「选股 名称」分析该股 | 「我的持仓」记录买入 | 「我的自选」查看全部",
    "positions": "💡 试试：「选股 名称」分析持仓 | 「对比 A B」横比 | 「加入自选 XX」扩展清单",
    "buy": "💡 试试：「选股 名称」跟踪分析 | 「对比」比较 | 「我的持仓」查看全部",
    "sell": "💡 试试：「选股 名称」发掘新标的 | 「我的自选」管理清单 | 「帮助」看全部功能",
    "compare": "💡 试试：「加入自选 XX」收藏 | 「买入 XX 1000股 价格」记录持仓 | 「选股清单」看推荐",
    "brain_chat": "💡 试试：「选股 名称」深度分析 | 「帮助」看全部功能 | 选股后可追问「目标价合理吗？」",
    "help": "💡 选股报告后可追问「目标价合理吗？」| 分析完自动支持多轮对话",
    "history_latest": "💡 试试：「选股清单」看推荐 | 直接提问「北向资金最近走势如何」",
    "topic_list": "💡 试试：「查 北向资金」按 topic 搜索 | 「最新」看今日快讯",
    "claude_task": "💡 方案生成后会推送审批 → 回复「可以」执行 → 完成后推送结果",
    "claude_approval": "💡 回复「可以」继续执行 | 「换个方案 新要求」重新出 | 「取消」放弃",
    "batch_pick": "💡 每只股票独立生成 PDF 研报，向上翻看即可 | 也可单独「选股 名称」分析",
    "backtest": "💡 试试：「选股清单」看推荐 | 「大盘预测」看方向 | 追问「失败率怎么降」可获优化建议",
}

# 不追加提示的意图（报错、会话管理等）
_HINT_SKIP = frozenset({
    "empty", "session_required", "session_end", "pick_error",
    "pick_timeout", "pick_skip", "watch_error", "watchlist_error",
    "positions_error", "buy_error", "sell_error", "compare_error",
    "button_unknown", "picklist_analyze", "info_query",
})


def _hint_for(intent: str, name: str = "") -> str:
    """根据意图返回单行功能提示，空串表示无提示。"""
    if intent in _HINT_SKIP:
        return ""
    return _HINT_MAP.get(intent, "")


def _reply_payload(
    intent: str, text: str, *, card: dict[str, Any] | None = None,
    hint_name: str = "", **extra: Any
) -> dict[str, Any]:
    hint = _hint_for(intent, name=hint_name)
    if hint and hint not in text:
        text = text + "\n\n" + hint
    result: dict[str, Any] = {
        "ok": True,
        "intent": intent,
        "reply_text": text,
        "reply_markdown": text if intent == "help" else f"### 资讯回复\n{text}",
        **extra,
    }
    if card:
        result["reply_template_card"] = card
    return result


def _capture_pick_context(chat_id: str | None, result: dict[str, Any]) -> None:
    """记录选股结果到会话上下文，供后续多轮追问使用。"""
    if not chat_id:
        return
    ts = result.get("ts_code")
    name = result.get("name")
    if ts:
        from chat_session import set_current_stock, set_last_intent

        set_current_stock(chat_id, ts, name or ts)
        set_last_intent(chat_id, result.get("intent", "pick"))


def _resolve_watch_symbol(query: str) -> tuple[str, str]:
    """解析自选股票名 → (ts_code, name)。"""
    from agents.user_position import _resolve_symbol
    return _resolve_symbol(query)


def _handle_button_click(key: str) -> dict[str, Any]:
    """处理模板卡片按钮点击。key 格式: action|code|name 或 action。"""
    parts = key.split("|")
    action = parts[0]
    code = parts[1] if len(parts) >= 2 else ""
    name = parts[2] if len(parts) >= 3 else code

    if action == "analyze" and code:
        pick = try_handle_pick(f"选股 {name or code}")
        if pick and pick.get("reply_text"):
            return _pick_reply(pick, str(pick["reply_text"]))
        return _reply_payload("pick_error", f"分析 {name or code} 暂不可用，请稍后重试。")

    if action == "news" and code:
        from agents.info_query import answer_info_question
        try:
            result = answer_info_question(f"{name} {code} 最近新闻")
            return _reply_payload("info_query", result["text"],
                                  keywords=result.get("keywords"),
                                  hit_count=result.get("count"))
        except Exception:
            return _reply_payload("info_query", f"查询 {name or code} 资讯失败，请稍后重试。")

    if action == "watch" and code:
        try:
            from zplan_shared.pick_watchlist import add_watch
            result = add_watch(name or code)
            return _reply_payload(
                "watch_add", f"✅ 已添加 {result['name']}({result['ts_code']}) 到自选清单。\n发送「我的自选」查看全部。",
                hint_name=result.get("name", ""),
            )
        except Exception as e:
            return _reply_payload("watch_error", f"添加自选失败: {e}")

    if action == "picklist":
        return get_latest_picks()

    if action == "picklist_analyze":
        return _reply_payload("picklist_analyze", "请回复股票名或代码进行分析，如「选股 爱普股份」。")

    return _reply_payload("button_unknown", f"按钮操作「{action}」暂不支持。")


def _pick_reply(pick: dict[str, Any], text: str) -> dict[str, Any]:
    """构建选股回复，PDF 报告通过群机器人 webhook 推送。

    文本回复使用 reply_markdown（企微 4096 字节限制），
    便于嵌入可点击的资讯链接；reply_text 保留短版兜底。
    """
    pdf_path = pick.get("pdf_path")

    # PDF 报告通过群机器人 webhook 推送（不再单独推送 K 线图，图表已嵌入 PDF）
    if pdf_path:
        try:
            from wechat_push import push_wechat_file
            push_wechat_file(pdf_path)
        except Exception:
            logger.warning("PDF 推送失败", exc_info=True)

    # 构建可点击链接的 markdown 版（资讯 URL 使用 [标题](url) 格式）
    md_text = _to_pick_markdown(text)
    short_text = truncate_wechat_utf8(text, WECHAT_TEXT_MAX_BYTES)

    # 追加上下文功能提示
    pick_intent = str(pick.get("intent") or "pick")
    stock_name = pick.get("name", "")
    hint = _hint_for(pick_intent, name=stock_name)
    if hint:
        short_text = (short_text + "\n\n" + hint) if short_text else short_text
        md_text = md_text + "\n\n" + hint

    result: dict[str, Any] = {
        "ok": True,
        "intent": pick_intent,
        "reply_text": short_text if len(short_text.encode("utf-8")) <= WECHAT_TEXT_MAX_BYTES else "",
        "reply_markdown": md_text,
        "ts_code": pick.get("ts_code"),
        "name": pick.get("name"),
        "run_id": pick.get("run_id"),
        "chart_path": pick.get("chart_path"),
        "pdf_path": pdf_path,
    }
    return result


def _handle_batch_pick(
    queries: list[str],
    *,
    chat_id: str | None = None,
) -> dict[str, Any] | None:
    """批量分析多只股票：每只单独生成研报（PDF + 回复），汇总提示。

    每个 query 会走完整的 try_handle_pick → _pick_reply 路径，
    PDF 通过 webhook 推送，文本回复仅返回汇总提示避免刷屏。
    """
    if not queries or len(queries) < 2:
        return None

    import concurrent.futures

    names: list[str] = []
    errors: list[str] = []

    def _run_one(q: str) -> tuple[str, str | None]:
        """返回 (query, error_or_None)。"""
        pick = try_handle_pick(f"分析 {q}")
        if pick and pick.get("reply_text"):
            # _pick_reply 会推送 PDF，我们只需要知道成功
            _pick_reply(pick, str(pick["reply_text"]))
            name = pick.get("name") or q
            return (name, None)
        elif pick and pick.get("intent") == "pick_error":
            return (q, pick.get("reply_text", "分析失败"))
        else:
            return (q, "无法识别该股票")

    # 顺序执行（避免并发导致 LLM API 限流和 DB 锁竞争）
    for q in queries:
        name, err = _run_one(q.strip())
        if err:
            errors.append(f"{q}: {err[:60]}")
        else:
            names.append(name)

    if not names and errors:
        return _reply_payload(
            "batch_pick",
            f"批量分析全部失败：\n" + "\n".join(errors[:5]),
        )

    lines = [
        f"📊 批量研报生成中（{len(names)}/{len(queries)} 只）",
        "",
        f"✅ 已生成: {'、'.join(names[:8])}",
    ]
    if errors:
        lines.append(f"⚠️ 失败: {'、'.join(errors[:3])}")

    lines.append("")
    lines.append("💡 每只股票的完整研报 PDF 正在推送中，请向上翻看")
    lines.append("> 也可单独发送「分析 股票名」获取单只研报")

    return _reply_payload("batch_pick", "\n".join(lines))


def _to_pick_markdown(plain: str) -> str:
    """将选股纯文本转为企微 markdown：纯文本 URL → [📎阅读原文](url) 可点击。"""
    import re as _re
    lines = plain.split("\n")
    out: list[str] = []
    prev_title: str | None = None  # 上一行标题（用于 URL 标签）
    url_pattern = _re.compile(r"^( {2,})(https?://\S+)\s*$")
    title_pattern = _re.compile(r"^· (.+)$")
    for line in lines:
        m = url_pattern.match(line)
        if m:
            url = m.group(2)
            # 优先用上一行的标题文本，否则用"阅读原文"
            label = (prev_title or "阅读原文")[:30]
            out.append(f"[📎{label}]({url})")
            prev_title = None
            continue
        # 记录标题行，供下一行 URL 使用
        tm = title_pattern.match(line)
        prev_title = tm.group(1)[:30] if tm else None
        out.append(line)
    return "\n".join(out)


def _route_to_brain(
    raw: str,
    *,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Brain 优先路由：携带完整会话上下文（history + current_stock + last_intent）。

    Brain 失败 → chat_engine（传上下文）→ info_query（最终回退）。
    """
    try:
        from agents.brain import get_brain
        from chat_session import get_history, get_current_stock, get_last_intent

        brain = get_brain()
        hist = get_history(chat_id) if chat_id else None
        cur_stock = get_current_stock(chat_id) if chat_id else None
        last_intent = get_last_intent(chat_id) if chat_id else None
        chat_result = brain.ask(raw, history=hist, current_stock=cur_stock, last_intent=last_intent)

        reply_text = chat_result.get("reply_markdown") or chat_result.get("reply_text", "")

        # 若 Brain 返回了具体股票结果，更新会话上下文
        if chat_id and chat_result.get("ts_code"):
            from chat_session import set_current_stock, set_last_intent

            set_current_stock(chat_id, chat_result["ts_code"],
                            chat_result.get("name", chat_result["ts_code"]))
            set_last_intent(chat_id, chat_result.get("intent", "brain_chat"))

        return _reply_payload(
            chat_result.get("intent", "brain_chat"),
            reply_text,
            ts_code=chat_result.get("ts_code"),
            card=chat_result.get("reply_template_card"),
            elapsed_s=chat_result.get("elapsed_s"),
        )
    except Exception as exc:
        logger.warning("Brain 失败，回退 chat_engine: %s", exc)
        try:
            from agents.chat_engine import llm_driven_chat
            from chat_session import get_history, get_current_stock

            hist = get_history(chat_id) if chat_id else None
            cur_stock = get_current_stock(chat_id) if chat_id else None
            chat_result = llm_driven_chat(raw, history=hist, current_stock=cur_stock)
            return _reply_payload(
                chat_result.get("intent", "llm_chat"),
                chat_result.get("reply_markdown", chat_result.get("reply_text", "")),
                ts_code=chat_result.get("ts_code"),
                card=chat_result.get("reply_template_card"),
                elapsed_s=chat_result.get("elapsed_s"),
                data_sources=chat_result.get("data_sources"),
            )
        except Exception as exc2:
            logger.warning("chat_engine 也失败，回退 info_query: %s", exc2)
            result = answer_info_question(raw)
            return _reply_payload(
                "info_query",
                result["text"],
                keywords=result["keywords"],
                hit_count=result["count"],
                hits=result["hits"],
            )


def _save_chat_history(
    *,
    channel: str,
    user_id: str | None,
    chat_id: str | None,
    user_message: str,
    bot_intent: str | None,
    bot_reply: str | None,
    error: str | None,
    elapsed_ms: int,
) -> None:
    """持久化一条对话记录（受 CHAT_HISTORY_ENABLED 开关控制）。"""
    if not CHAT_HISTORY_ENABLED:
        return
    try:
        with SessionLocal() as session:
            session.add(
                ChatHistory(
                    channel=channel,
                    user_id=user_id,
                    chat_id=chat_id,
                    user_message=user_message,
                    bot_intent=bot_intent,
                    bot_reply=bot_reply,
                    error=error,
                    elapsed_ms=elapsed_ms,
                )
            )
            session.commit()
    except Exception:
        logger.exception("保存 chat_history 失败（不影响主流程）")


def handle_inbound_text(
    message: str,
    *,
    user_id: str | None = None,
    channel: str = "unknown",
    chat_id: str | None = None,
    mentioned: bool = False,
) -> dict[str, Any]:
    """解析用户消息并返回回复，同时持久化对话记录。

    新增可选参数（向后兼容）：
    - user_id: 企微用户 OpenID
    - channel: 通道标识（wecom_bot / wework_app / http_bridge / cli）
    - chat_id: 群聊 ID
    - mentioned: 消息是否显式 @了机器人（用于会话窗口判断）
    """
    t0 = time.time()
    result: dict[str, Any] | None = None
    error: str | None = None
    is_new_session = False
    try:
        # @ 消息：刷新会话窗口（在 impl 之前，以便 impl 内 session_active 检查生效）
        if mentioned and chat_id:
            is_new_session = touch_session(chat_id)
        result = _handle_inbound_text_impl(message, mentioned=mentioned, chat_id=chat_id, user_id=user_id)
        # ── 统一保存对话历史到会话窗口（供 Brain 多轮推理）──
        if chat_id and result and result.get("reply_text"):
            add_message(chat_id, "user", message)
            add_message(chat_id, "assistant", result["reply_text"])
        # 新会话追加有效时长提示（智能机器人渠道除外，因平台要求必须 @）
        if is_new_session and result and result.get("reply_text") and channel != "wecom_bot":
            from chat_session import get_session_store
            ttl_min = get_session_store().ttl_seconds // 60
            hint = SESSION_ACTIVE_HINT.format(ttl_min)
            result["reply_text"] += hint
            if result.get("reply_markdown"):
                result["reply_markdown"] += hint
        return result
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        raise
    finally:
        elapsed_ms = int((time.time() - t0) * 1000)
        _save_chat_history(
            channel=channel,
            user_id=user_id,
            chat_id=chat_id,
            user_message=message,
            bot_intent=result.get("intent") if result else None,
            bot_reply=result.get("reply_text") if result else None,
            error=error,
            elapsed_ms=elapsed_ms,
        )


def _handle_inbound_text_impl(
    message: str,
    *,
    mentioned: bool = False,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    init_db()
    raw = _normalize_user_text(message)

    # ── 会话窗口检查 ──
    # 非 @ 消息：需要活跃会话窗口，否则引导用户 @ 机器人
    if not mentioned and chat_id and not session_active(chat_id):
        # 排除无内容消息（空消息不应触发提示）
        if not raw:
            return _reply_payload("empty", "")
        return _reply_payload("session_required", SESSION_REQUIRED_TEXT)

    if not raw or raw.lower() in ("help", "帮助", "?", "？"):
        return _reply_payload("help", HELP_TEXT)

    low = raw.lower()

    # ── 模板卡片按钮点击 ──
    if raw.startswith("__btn__"):
        return _handle_button_click(raw[7:])

    # ── 退出会话窗口 ──
    if low in ("退出", "exit", "quit", "结束", "结束会话", "关闭"):
        if chat_id:
            expire_session(chat_id)
            return _reply_payload("session_end", "已结束当前会话窗口。再次发送消息时请 @我。")
        return _reply_payload("help", HELP_TEXT)

    # ── Claude 方案审批 ──
    approval_result = _try_handle_approval(raw, low, chat_id=chat_id or "")
    if approval_result is not None:
        return approval_result

    # ── Claude Code 远程任务 ──
    if low.startswith("claude ") or low.startswith("@claude ") or low.startswith("claude,") or low == "claude":
        task_text = re.sub(r"^(?:@?claude[,\s]+)", "", raw, flags=re.IGNORECASE).strip()
        if not task_text:
            return _reply_payload(
                "claude_task",
                "请描述需要 Claude 处理的任务。\n"
                "示例：「claude 修改选股报告格式，把风险提示放在最前面」",
            )
        task = claude_task_queue.create_task(
            text=task_text,
            user_id=user_id or "",
            chat_id=chat_id or "",
        )
        tid_short = task["id"][:8]
        text_preview = task_text[:150] + ("…" if len(task_text) > 150 else "")
        return _reply_payload(
            "claude_task",
            f"📋 任务已入队\n\n"
            f"ID: `{tid_short}…`\n"
            f"内容: {text_preview}\n\n"
            f"⏳ Claude 将在 {_next_poll_eta()} 内生成方案\n"
            f"📝 方案生成后会推送给你审批\n"
            f"✅ 回复「**可以**」即可自动执行",
        )

    # ── 混合路由：活跃会话时模糊消息 → Brain 优先 ──
    # 明确指令（_SESSION_RULE_FIRST 匹配）保持规则路由，其余交给 Brain 做自然语言理解
    if chat_id and session_active(chat_id) and not _SESSION_RULE_FIRST.match(raw):
        return _route_to_brain(raw, chat_id=chat_id, user_id=user_id)

    # ── Topic 摘要 ──
    if low in ("最新", "latest", "摘要"):
        payload = get_history_payload("latest", None)
        text = f"【最新 X 摘要】\n{payload['wechat_text']}"
        return _reply_payload("history_latest", text, count=payload["count"])

    if low in ("7天", "7d", "一周") or raw in ("最近7天",):
        payload = get_history_payload("7d", None)
        text = f"【最近 7 天】\n{payload['wechat_text']}"
        return _reply_payload("history_7d", text, count=payload["count"])

    if low in ("列表", "topics", "topic"):
        topics = list_topics(echo=False)
        lines = [
            f"- {t['topic_key']} · {t['display_name']} · {'开' if t['enabled'] else '关'}"
            for t in topics
        ]
        body = "\n".join(lines) if lines else "(暂无 topic)"
        return _reply_payload("topic_list", f"【Topic 列表】\n{body}")

    # ── 自选管理 ──
    if re.match(r"^(加入自选|添加自选|关注)\s+", raw):
        symbol = re.sub(r"^(加入自选|添加自选|关注)\s+", "", raw).strip()
        try:
            from zplan_shared.pick_watchlist import add_watch
            result = add_watch(symbol)
            return _reply_payload(
                "watch_add", f"✅ 已添加 {result['name']}({result['ts_code']}) 到自选清单。\n发送「我的自选」查看全部。",
                hint_name=result.get("name", ""),
            )
        except LookupError as e:
            return _reply_payload("watch_error", f"❌ {e}")
        except Exception as e:
            return _reply_payload("watch_error", f"添加失败: {e}")

    if re.match(r"^(移除自选|删除自选|取消关注)\s+", raw):
        symbol = re.sub(r"^(移除自选|删除自选|取消关注)\s+", "", raw).strip()
        try:
            from zplan_shared.pick_watchlist import remove_watch
            # resolve first to get name
            code, name = _resolve_watch_symbol(symbol)
            ok = remove_watch(code)
            if ok:
                return _reply_payload("watch_remove", f"✅ 已从自选移除 {name or code}({code})。")
            return _reply_payload("watch_remove", f"「{symbol}」不在自选清单中。")
        except LookupError as e:
            return _reply_payload("watch_error", f"❌ {e}")

    if low in ("我的自选", "自选列表", "自选", "关注列表"):
        try:
            from agents.user_position import format_watchlist_text
            return _reply_payload("watchlist", format_watchlist_text())
        except Exception as e:
            return _reply_payload("watchlist_error", f"获取自选失败: {e}")

    # ── 持仓管理 ──
    if re.match(r"^(我的持仓|持仓|持仓情况|仓位|我的仓位)$", raw):
        if not user_id:
            return _reply_payload("positions", "持仓功能需要用户身份。请在企微中 @我 使用。")
        try:
            from agents.user_position import format_positions_text
            return _reply_payload("positions", format_positions_text(user_id))
        except Exception as e:
            return _reply_payload("positions_error", f"获取持仓失败: {e}")

    buy_parsed = None
    try:
        from agents.user_position import parse_buy_command
        buy_parsed = parse_buy_command(raw)
    except ImportError:
        pass
    if buy_parsed:
        if not user_id:
            return _reply_payload("positions", "持仓功能需要用户身份。请在企微中 @我 使用。")
        try:
            from agents.user_position import add_position
            result = add_position(
                user_id, buy_parsed["symbol"],
                buy_parsed["shares"], buy_parsed["price"],
                notes=buy_parsed.get("notes"),
            )
            act = "已更新" if result.get("action") == "updated" else "已记录"
            price_str = f" @¥{result['buy_price']:.2f}" if result.get("buy_price") else ""
            return _reply_payload(
                "buy", f"✅ {act} {result['name']}({result['ts_code']}) "
                f"{result['shares']}股{price_str}。\n发送「我的持仓」查看。",
                hint_name=result.get("name", ""),
            )
        except LookupError as e:
            return _reply_payload("buy_error", f"❌ {e}")
        except Exception as e:
            return _reply_payload("buy_error", f"买入记录失败: {e}")

    sell_symbol = None
    try:
        from agents.user_position import parse_sell_command
        sell_symbol = parse_sell_command(raw)
    except ImportError:
        pass
    if sell_symbol:
        if not user_id:
            return _reply_payload("positions", "持仓功能需要用户身份。请在企微中 @我 使用。")
        try:
            from agents.user_position import remove_position
            info = remove_position(user_id, sell_symbol)
            if info:
                return _reply_payload(
                    "sell", f"✅ 已移除 {info['name']}({info['ts_code']}) {info['shares']}股。",
                    hint_name=info.get("name", ""),
                )
            return _reply_payload("sell", f"「{sell_symbol}」不在你的持仓中。")
        except LookupError as e:
            return _reply_payload("sell_error", f"❌ {e}")
        except Exception as e:
            return _reply_payload("sell_error", f"卖出记录失败: {e}")

    # ── 大盘预测 ──
    if low in ("大盘预测", "大盘", "预测", "市场预测", "forecast", "market forecast"):
        return get_latest_forecast()

    # ── 选股清单 ──
    if low in ("选股清单", "最新选股", "今日推荐", "top picks", "top picks!", "推荐"):
        return get_latest_picks()

    # ── 回测结果 ──
    if low in ("回测结果", "回测", "选股表现", "backtest", "表现"):
        return get_latest_backtest()

    # ── 概念筛选 ──
    if re.match(r"^(筛选|题材|概念)\s*[：:\s]*(.+)", raw, re.IGNORECASE):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            result = _pick_reply(pick, str(pick["reply_text"]))
            _capture_pick_context(chat_id, result)
            return result

    # ── Topic 查询 ──
    # 增加最小长度限制：key 至少 2 个中文字符或 3 个 ASCII 字符，避免贪婪匹配单字
    m = re.match(r"查\s*(\S+)", raw)
    key = m.group(1) if m else ""
    if key and not (len(key.encode("utf-8")) >= 6 or len(key) >= 3):
        key = ""  # key 太短，不匹配（如"查一下""查""查a"）
    if key:
        topics = list_topics(echo=False)
        for t in topics:
            if key == t["topic_key"] or key == t["display_name"]:
                payload = get_history_payload("latest", t["topic_key"])
                text = f"【{t['display_name']}】\n{payload['wechat_text']}"
                return _reply_payload(
                    "history_topic",
                    text,
                    topic_key=t["topic_key"],
                    count=payload["count"],
                )

    # ── 股票对比 ──
    compare_pair = None
    try:
        from agents.compare import parse_compare_command
        compare_pair = parse_compare_command(raw)
    except ImportError:
        pass
    if compare_pair:
        try:
            from agents.compare import compare_two
            result = compare_two(compare_pair[0], compare_pair[1])
            return _reply_payload(
                result.get("intent", "compare"),
                result["reply_text"],
            )
        except Exception as e:
            return _reply_payload("compare_error", f"对比失败: {e}")

    # ── 批量分析：识别 "分析 A · 分析 B · 分析 C" ──
    batch_queries = _split_batch_queries(raw)
    if batch_queries:
        result = _handle_batch_pick(batch_queries, chat_id=chat_id)
        if result:
            return result

    # ── 选股分析（直接路由，绕过 Brain 减少一次 LLM 往返）───
    # 显式选股前缀：选股/分析/打分/研报/查股/评分 + 标的
    if re.match(r"^(选股|打分|分析|研报|评股|查股|评分)\s*[：:\s]", raw, re.IGNORECASE):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            result = _pick_reply(pick, str(pick["reply_text"]))
            _capture_pick_context(chat_id, result)
            return result

    # 简单股票名/代码 → 直接深度分析（误判由 try_handle_pick 返回 None 兜底）
    # 注意：有活跃会话时跳过，交给 Brain 做自然语言理解（避免"它""这个"等代词误判）
    if not (chat_id and session_active(chat_id)) and _looks_like_stock_query(raw):
        pick = try_handle_pick(raw)
        if pick and pick.get("reply_text"):
            result = _pick_reply(pick, str(pick["reply_text"]))
            _capture_pick_context(chat_id, result)
            return result

    # ── Brain 驱动的统一对话（最终回退）──
    # 所有未匹配规则的消息最终交给 Brain，携带完整会话上下文
    return _route_to_brain(raw, chat_id=chat_id, user_id=user_id)
