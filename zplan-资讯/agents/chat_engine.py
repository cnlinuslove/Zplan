"""LLM 驱动的统一对话引擎。

架构：用户消息 → 预加载数据 → LLM 理解意图 + 生成回复。
不再用正则路由，LLM 自己决定怎么回答。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from config import INFO_QUERY_LIVE_FETCH
from llm.gemini_client import (
    _chat_completion,
    _effective_max_tokens,
    _extract_text_and_check,
    deepseek_available,
)
from models import SessionLocal, init_db
from sqlalchemy import desc, select, text
from zplan_shared.fundamentals import get_financials
from zplan_shared.market import get_bars
from zplan_shared.models import DailyPrice, StockList, StockRuleScore
from zplan_shared.news_linker import get_linked_news_for_stock

logger = logging.getLogger(__name__)

# ── 系统提示词 ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Z-Plan，一名 A 股上市公司研究报告员。你基于系统预加载的实时数据，
优先引用数据中的信息，数据不足时明确说明并建议获取途径。

## 你的工具箱（系统已预加载）
- 实时股价与 K 线数据
- 规则系统技术评分（含信号、排名）
- 财报指标（PE/PB/ROE/营收/净利）
- 概念板块与行业分类
- 近 7 天关联新闻（库内）
- 实时资讯搜索结果（Google News / 东财快讯）

## 对话风格
- 专业、客观、直接——不许寒暄（"好的"、"收到"、"作为 AI"）
- 每条结论必须引用具体数字或来源，不能空泛
- 数据不足时写「暂无该数据，建议查阅公司公告/官网/东财个股页」
- 不编造数据，不猜测没有依据的结论
- 结尾可提一个简短后续建议

## 模式判断（由你根据用户消息自行决定）
1. **闲聊/简单问答** → 直接回答，2-5 句即可
2. **个股分析**（用户只发名称或代码）→ 深度分析：技术面 + 财务 + 资讯 + 风险评估 + 操作建议，200-500 字
3. **问答讨论**（"XX 怎么样""怎么看 XX"）→ 技术面 + 基本面评估 + 风险提示，200-400 字
4. **深度研报**（"生成报告""研报""深度研究"）→ 按下述 8 模块完整输出

若已预加载数据中包含 LLM 分析结果，优先引用 LLM 分析结论而非仅展示原始数据。

---

## 深度研报格式（仅报告模式，严格按此结构）

### 一、公司基本信息
- 公司定位分类：根据市值判断（小型 <50亿 / 中型 50-200亿 / 大型 200-1000亿 / 头部 >1000亿）
- 所属行业与概念板块
- 上市日期与历程
- 若无详细资料，标注「数据来源有限，建议查阅公司官网」

### 二、核心产品与业务（核心）
- 主营产品/服务介绍
- 营收构成（如数据可得）
- 产品竞争力与市场地位
- 若无产品销售数据，明确标注并建议查阅年报

### 三、股价与技术分析（核心）
- 近期走势：引用具体日期和价格，与所属板块对比
- 均线排列、支撑/阻力位、量价关系
- 规则系统技术评分及含义
- 技术指标综合判断

### 四、财务分析（核心）
- 近三年营收/净利润趋势（列表对比）
- 最新 PE/PB/ROE 及行业对比
- 负债与现金流（如数据可得）
- 各项打分（百分制）：
  | 指标 | 评分 | 说明 |
  |------|------|------|
  | 盈利能力 | XX/100 | ... |
  | 成长性 | XX/100 | ... |
  | 估值合理性 | XX/100 | ... |
  | 财务健康度 | XX/100 | ... |

### 五、股东与机构持仓
- 大股东持股情况（如数据可得）
- 机构持仓变化
- 近期增减持动态
- 无数据时标注并建议查阅东财/同花顺

### 六、创始人与管理团队
- 创始人/CEO 背景（如数据可得）
- 核心管理层稳定性
- 无数据时标注「暂无团队背景数据，建议查阅公司年报/官网」

### 七、风险分析（核心）
尽可能全面，每条标注风险等级（高/中/低）：
- ⚠️ 市场风险（竞争、需求变化）
- ⚠️ 政策风险（监管、行业政策）
- ⚠️ 财务风险（负债、现金流、商誉）
- ⚠️ 经营风险（产品集中度、供应链）
- ⚠️ 法律风险（诉讼、合规）

### 八、核心竞争力与机遇
- 护城河分析（技术/品牌/规模/牌照）
- 政策扶持
- 外部合作与战略布局
- 主要竞争对手对比

### 九、投资建议
- 综合评分（百分制），说明理由
- 操作建议：强烈关注 / 关注 / 观望 / 谨慎 / 回避
- 建议买入区间、目标价、止损位
- 三种情景推演（乐观/中性/悲观），各附概率和触发条件

---

## 全局约束
- 报告模式总字数 1000-1800 字
- 每条观点必须引用来源（系统数据 / 实时资讯 / 公开信息）
- 数据缺口必须诚实标注
- 使用企微兼容 markdown：**粗体**、| 表格、- 列表、### 标题
- ### 标题必须从「一」开始编号"""



# ── 数据预加载 ──────────────────────────────────────────────────────────

from agents.shared import find_stocks_in_text


def _compact_bars(code: str) -> str:
    """紧凑 K 线摘要（最近 5 日 + 关键统计）。"""
    end = date.today()
    start = end - timedelta(days=90)
    bars = get_bars(code, start=start, end=end)
    if bars.empty:
        return "无数据"
    tail = bars.tail(30)
    if tail.empty:
        return "无数据"

    latest = tail.iloc[-1]
    lines = [
        f"最新({str(latest.name)[:10]}): 收{latest['close']:.2f} "
        f"{latest.get('pct_chg', 0):+.2f}% 量{latest.get('volume', 0):.0f}"
    ]

    # 近 5 日
    last5 = tail.tail(5)
    if len(last5) >= 3:
        closes = [f"{r['close']:.2f}" for _, r in last5.iterrows()]
        chg5 = (last5.iloc[-1]['close'] / last5.iloc[0]['close'] - 1) * 100
        lines.append(f"近5日: {' → '.join(closes)} ({chg5:+.2f}%)")

    # 极值
    high60 = tail['high'].max()
    low60 = tail['low'].min()
    avg_vol = tail['volume'].mean()
    lines.append(f"60日区间: {low60:.2f}-{high60:.2f} 均量{avg_vol:.0f}")

    return " | ".join(lines)


def _compact_news(code: str, name: str, limit: int = 5) -> str:
    """紧凑新闻摘要。"""
    items = get_linked_news_for_stock(code, hours=168, limit=limit)
    if not items:
        return "库内暂无"
    parts = []
    for item in items[:limit]:
        title = " ".join(str(item.get("title") or "").split())[:60]
        src = str(item.get("source_label") or "")[:8]
        t = str(item.get("published_at_utc") or "")[:10]
        parts.append(f"· {title}（{src} {t}）")
    return "\n".join(parts) if parts else "库内暂无"


def _compact_financials(code: str) -> str:
    """紧凑财报摘要（近 3 期，用于报告模式）。"""
    try:
        df = get_financials(code, limit=4)
        if df.empty:
            return "无财报数据"
        lines = ["报告期 | 营收(亿) | 净利(亿) | PE | PB | ROE%"]
        for _, r in df.iterrows():
            rd = str(r.get("report_date", ""))[:10]
            rev = f"{r['revenue']/1e8:.2f}" if pd.notna(r.get("revenue")) else "-"
            np_ = f"{r['net_profit']/1e8:.2f}" if pd.notna(r.get("net_profit")) else "-"
            pe = f"{r['pe_ttm']:.1f}" if pd.notna(r.get("pe_ttm")) else "-"
            pb = f"{r['pb']:.2f}" if pd.notna(r.get("pb")) else "-"
            roe = f"{r['roe']:.2f}" if pd.notna(r.get("roe")) else "-"
            lines.append(f"{rd} | {rev:>6} | {np_:>6} | {pe:>6} | {pb:>5} | {roe:>5}")
        return "\n".join(lines)
    except Exception:
        return "财报数据获取失败"


def gather_chat_context(user_message: str) -> dict[str, Any]:
    """预加载用户消息涉及的所有相关数据。"""
    stocks = find_stocks_in_text(user_message)
    stock_data: list[dict[str, Any]] = []
    live_news: list[str] = []

    if stocks:
        # 并行收集每只股票的关键数据
        def _fetch_one(code: str, name: str) -> dict[str, Any]:
            bars = _compact_bars(code)
            concepts = _concept_list(code)
            score = _rule_score(code)
            news = _compact_news(code, name)
            fin = _compact_financials(code)
            return {
                "code": code,
                "name": name,
                "meta": _stock_meta(code),
                "bars": bars,
                "concepts": concepts,
                "score": score,
                "news": news,
                "financials": fin,
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(stocks), 4)) as pool:
            futs = {pool.submit(_fetch_one, c, n): (c, n) for c, n in stocks}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    stock_data.append(fut.result())
                except Exception as exc:
                    logger.warning("预加载股票数据失败: %s", exc)

        # 实时资讯搜索
        if INFO_QUERY_LIVE_FETCH:
            try:
                from agents.info_query import fetch_live_hits

                kws = [user_message[:40]]
                for _, name in stocks:
                    kws.append(name)
                hits, _ = fetch_live_hits(user_message, kws[:3], limit=6)
                for h in hits[:6]:
                    live_news.append(f"· {h.title}（{h.source_label}）")
            except Exception:
                pass

    return {
        "stocks": stock_data,
        "live_news": live_news,
    }


def _stock_meta(code: str) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.execute(
            select(StockList).where(StockList.ts_code == code)
        ).scalar_one_or_none()
    if not row:
        return {"industry": "未知", "listing_date": None}
    return {
        "industry": row.industry or "未知",
        "listing_date": str(row.listing_date) if row.listing_date else None,
    }


def _concept_list(code: str) -> list[str]:
    with SessionLocal() as session:
        rows = session.execute(
            text("SELECT concept_name FROM stock_concept_members WHERE ts_code=:c LIMIT 5"),
            {"c": code},
        ).all()
    return [str(r[0]) for r in rows]


def _rule_score(code: str) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.execute(
            select(StockRuleScore)
            .where(StockRuleScore.ts_code == code)
            .order_by(desc(StockRuleScore.trade_date_as_of), desc(StockRuleScore.rule_version))
            .limit(1)
        ).scalars().first()
    if not row:
        return {"composite_score": None, "tech_score": None, "verdict": None}
    return {
        "composite_score": row.composite_score,
        "tech_score": row.tech_score,
        "verdict": row.verdict,
    }


# ── LLM 对话 ────────────────────────────────────────────────────────────


def _build_chat_prompt(user_message: str, context: dict[str, Any]) -> str:
    """组装发送给 LLM 的完整 prompt（数据 + 用户消息）。"""
    parts = ["## 系统已预加载的实时数据\n"]
    today_str = date.today().isoformat()

    stock_data = context.get("stocks", [])
    if stock_data:
        for sd in stock_data:
            parts.append(f"### {sd['name']}({sd['code']})")
            parts.append(f"行业: {sd['meta'].get('industry', '?')}  |  "
                         f"上市: {sd['meta'].get('listing_date', '?')}")
            parts.append(f"概念: {', '.join(sd.get('concepts', []))}")
            s = sd.get("score", {})
            parts.append(f"规则评分: {s.get('composite_score', '?')}  |  "
                         f"技术: {s.get('tech_score', '?')}  |  判定: {s.get('verdict', '?')}")
            parts.append(f"K线: {sd.get('bars', '无')}")
            fin_str = sd.get("financials", "")
            if fin_str and fin_str not in ("无财报数据", "财报数据获取失败"):
                parts.append(f"财报:\n{fin_str}")
            news_str = sd.get("news", "")
            if news_str and news_str != "库内暂无":
                parts.append(f"库内资讯:\n{news_str}")
            parts.append("")
    else:
        parts.append("（未检测到具体股票引用，用户可能在问大盘/行业/闲聊）\n")

    live = context.get("live_news", [])
    if live:
        parts.append(f"### 实时搜索结果（{len(live)} 条）")
        parts.extend(live[:6])
        parts.append("")

    parts.append(f"## 用户消息（{today_str}）")
    parts.append(user_message)

    return "\n".join(parts)


def llm_driven_chat(user_message: str) -> dict[str, Any]:
    """LLM 驱动的统一对话入口。

    替代原有正则路由。预加载数据 → LLM 理解意图 + 生成回复。
    """
    t0 = time.time()

    if not deepseek_available():
        return {
            "ok": False,
            "intent": "error",
            "reply_text": "LLM 服务未配置（DEEPSEEK_API_KEY），无法进行智能对话。请发送「帮助」查看离线命令。",
            "reply_markdown": "LLM 服务未配置",
        }

    # 1. 预加载数据
    logger.info("ChatEngine 开始预加载数据")
    context = gather_chat_context(user_message)
    t1 = time.time()
    logger.info(
        "ChatEngine 数据预加载完成: %.1fs (stocks=%d, live_news=%d)",
        t1 - t0,
        len(context.get("stocks", [])),
        len(context.get("live_news", [])),
    )

    # 2. 组装 prompt
    prompt = _build_chat_prompt(user_message, context)
    logger.info("ChatEngine prompt: %d chars", len(prompt))

    # 3. LLM 调用
    resp = _chat_completion(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=min(4096, _effective_max_tokens(2048)),
    )
    reply = _extract_text_and_check(resp)
    t2 = time.time()
    logger.info(
        "ChatEngine LLM 完成: %.1fs, reply=%d chars",
        t2 - t1,
        len(reply),
    )

    # 4. 格式化
    stocks = context.get("stocks", [])
    if stocks:
        # 带股票卡片
        primary = stocks[0]
        card = {
            "card_type": "button_interaction",
            "main_title": {
                "title": f"{primary['name']}({primary['code']})",
                "desc": "继续了解",
            },
            "task_id": f"chat_{int(time.time() * 1000)}",
            "button_list": [
                {"text": "完整打分", "style": 1, "key": f"analyze|{primary['code']}|{primary['name']}"},
                {"text": "最新快讯", "style": 0, "key": f"news|{primary['code']}|{primary['name']}"},
                {"text": "深度研报", "style": 0, "key": f"analyze|{primary['code']}|{primary['name']}"},
            ],
        }
    else:
        card = None

    # 截断到企微限制
    encoded = reply.encode("utf-8")
    if len(encoded) > 3900:
        result = ""
        current = 0
        for ch in reply:
            ch_bytes = len(ch.encode("utf-8"))
            if current + ch_bytes > 3800:
                result += "\n\n…（已截断）"
                break
            result += ch
            current += ch_bytes
        reply = result

    return {
        "ok": True,
        "intent": "llm_chat",
        "reply_markdown": reply,
        "reply_text": reply,
        "reply_template_card": card,
        "ts_code": stocks[0]["code"] if stocks else None,
        "elapsed_s": t2 - t0,
        "data_sources": {
            "stocks": len(stocks),
            "live_news": len(context.get("live_news", [])),
        },
    }
