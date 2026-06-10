#!/usr/bin/env python3
"""盘后大盘+板块预测：历史相似形态搜索 + LLM 解读 + 证据链。

流程：
  1. 拉取 7 大指数 K 线 + 市场宽度 + 北向资金 + 行业板块表现
  2. 对每个指数做历史相似形态搜索（pattern_similarity）
  3. 生成指数 K 线图 + 板块热力图（chart_viz）
  4. LLM 结构化预测（含证据链）
  5. 落库 market_forecasts + 企微推送

用法:
    cd zplan-资讯 && .venv/bin/python scripts/market_forecast.py
    cd zplan-资讯 && .venv/bin/python scripts/market_forecast.py --dry-run
    cd zplan-资讯 && .venv/bin/python scripts/market_forecast.py --no-push
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

from sqlalchemy import desc, select, text

from zplan_shared.config import DEEPSEEK_MODEL, GEMINI_MODEL, LLM_MODEL
from zplan_shared.llm.gemini import generate_json, llm_available, pop_usage
from zplan_shared.market import get_index_bars, get_index_panel, latest_index_trade_date
from zplan_shared.models import (
    DailyIndex, MarketForecast, PickEntry, PickRun, SessionLocal,
    StockConceptMember, StockList, init_db,
)

BEIJING_TZ = timezone(timedelta(hours=8))
_LLM_MODEL = LLM_MODEL or DEEPSEEK_MODEL or GEMINI_MODEL

logger = logging.getLogger(__name__)

# ── 指数配置 ──
_A_INDEX_ORDER = ["000001", "399001", "399006", "000688", "000300", "000905", "000852"]
_GLOBAL_INDEX_ORDER = [".INX", ".IXIC", ".DJI", "HSI"]
_INDEX_NAMES = {
    "000001": "上证指数", "399001": "深证成指", "399006": "创业板指",
    "000688": "科创50", "000300": "沪深300", "000905": "中证500", "000852": "中证1000",
    ".INX": "标普500", ".IXIC": "纳斯达克", ".DJI": "道琼斯", "HSI": "恒生指数",
}

# ── LLM Schema ──
_FORECAST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "market_direction": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["bullish", "bearish", "range-bound"]},
                "confidence": {
                    "type": "number",
                    "description": "置信度 0-100。规则：差2-4→45-55%；差5-7→55-65%；差≥8→65-75%。看跌门槛更低(bear_w ≥ bull_w+2即可看跌)，看涨门槛更高(bull_w ≥ bear_w+5才看涨)。"
                },
                "reasoning": {"type": "string", "description": "综合判断理由，80-150字。必须提到多空信号对比。"},
                "bullish_signals": {
                    "type": "array",
                    "description": "看涨信号列表（数据驱动的看多理由）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "description": "信号类别：技术面/资金面/历史形态/外盘/政策/市场宽度"},
                            "signal": {"type": "string"},
                            "value": {"type": "string"},
                            "weight": {"type": "integer", "description": "信号强度 1=弱 2=中等 3=强"},
                        },
                        "required": ["type", "signal", "value", "weight"],
                    },
                },
                "bearish_signals": {
                    "type": "array",
                    "description": "看跌信号列表（数据驱动的看空理由）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "signal": {"type": "string"},
                            "value": {"type": "string"},
                            "weight": {"type": "integer"},
                        },
                        "required": ["type", "signal", "value", "weight"],
                    },
                },
                "key_uncertainty": {"type": "string", "description": "最大的不确定性因素（1 句话）"},
            },
            "required": ["direction", "confidence", "reasoning", "bullish_signals", "bearish_signals", "key_uncertainty"],
        },
        "index_forecasts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "direction": {"type": "string", "enum": ["偏多", "偏空", "震荡"]},
                    "confidence": {"type": "number"},
                    "similar_patterns_verdict": {"type": "string"},
                    "key_levels": {
                        "type": "object",
                        "properties": {
                            "support": {"type": "number"},
                            "resistance": {"type": "number"},
                        },
                    },
                },
                "required": ["code", "name", "direction", "confidence"],
            },
        },
        "sector_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sector": {"type": "string"},
                    "direction": {"type": "string", "enum": ["看多", "看淡", "中性"]},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["sector", "direction", "confidence", "reasoning"],
            },
        },
        "concept_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept": {"type": "string"},
                    "direction": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
            },
        },
        "risk_factors": {"type": "array", "items": {"type": "string"}},
        "next_day_scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario": {"type": "string"},
                    "probability": {"type": "number"},
                    "trigger": {"type": "string"},
                },
            },
        },
    },
    "required": ["market_direction", "index_forecasts", "risk_factors", "next_day_scenarios"],
}


# ═══ 数据拉取 ══════════════════════════════════════════════════════


def _get_market_breadth(changes: dict[str, float]) -> dict[str, Any]:
    """全市场宽度：涨跌家数 + 中位数涨跌。"""
    import numpy as np
    if not changes:
        return {"total": 0, "up_n": 0, "down_n": 0, "median_pct": 0.0}
    vals = list(changes.values())
    up_n = sum(1 for v in vals if v > 0)
    down_n = sum(1 for v in vals if v < 0)
    median = round(float(np.median(vals)), 2) if vals else 0.0
    return {"total": len(vals), "up_n": up_n, "down_n": down_n, "median_pct": median}


def _get_daily_changes(session, trade_date: date) -> dict[str, float]:
    """返回 {ts_code: pct_chg} 映射，pct_chg 为 NULL 时从 close 推算。"""
    import pandas as pd

    d = trade_date.strftime("%Y-%m-%d")
    rows = session.execute(
        text(
            "SELECT a.ts_code, a.close, a.pct_chg, b.close AS prev_close "
            "FROM daily_prices a "
            "LEFT JOIN daily_prices b ON a.ts_code = b.ts_code "
            "  AND b.trade_date = (SELECT MAX(trade_date) FROM daily_prices "
            "    WHERE ts_code = a.ts_code AND trade_date < :d AND market='a') "
            "WHERE a.trade_date = :d AND a.market = 'a' AND a.close IS NOT NULL"
        ),
        {"d": d},
    ).fetchall()

    changes: dict[str, float] = {}
    for ts_code, close, pct_chg, prev_close in rows:
        if pct_chg is not None:
            changes[ts_code] = float(pct_chg)
        elif prev_close is not None and prev_close > 0:
            changes[ts_code] = (float(close) - float(prev_close)) / float(prev_close) * 100
    return changes


def _get_northbound_recent(session, days: int = 5) -> list[dict[str, Any]]:
    """近 N 日北向资金净流向。"""
    try:
        rows = session.execute(
            text(
                "SELECT as_of_utc, metric_name, metric_value FROM market_sentiment "
                "WHERE factor_kind = 'northbound_daily' AND metric_name = '当日成交净买额' "
                "AND metric_value IS NOT NULL "
                "ORDER BY as_of_utc DESC LIMIT :n"
            ),
            {"n": days},
        ).fetchall()
        return [
            {"date": str(r[0])[:10] if r[0] else "?", "value": float(r[2]) if r[2] else 0}
            for r in rows
        ]
    except Exception:
        return []


def _get_industry_performance(session, changes: dict[str, float]) -> list[dict[str, Any]]:
    """当日行业板块表现（用预计算的 pct_chg）。"""
    if not changes:
        return []
    # 从 stock_list 获取 ts_code → industry 映射
    codes = list(changes.keys())
    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    rows = session.execute(
        text(
            f"SELECT ts_code, industry FROM stock_list "
            f"WHERE ts_code IN ({placeholders}) AND industry IS NOT NULL AND industry != ''"
        ),
        {f"c{i}": c for i, c in enumerate(codes)},
    ).fetchall()

    # 按行业聚合
    from collections import defaultdict
    ind_data: dict[str, list[float]] = defaultdict(list)
    for ts_code, industry in rows:
        chg = changes.get(ts_code)
        if chg is not None:
            ind_data[industry].append(chg)

    result = []
    for ind, vals in ind_data.items():
        if len(vals) < 8:
            continue
        avg = sum(vals) / len(vals)
        up_n = sum(1 for v in vals if v > 0)
        down_n = sum(1 for v in vals if v < 0)
        result.append({"industry": ind, "stock_cnt": len(vals), "avg_pct": round(avg, 2),
                       "up_n": up_n, "down_n": down_n})

    result.sort(key=lambda x: x["avg_pct"], reverse=True)
    return result


def _get_concept_heat(session, changes: dict[str, float]) -> list[dict[str, Any]]:
    """概念板块热度（成分股聚合涨跌幅，用预计算的 pct_chg）。"""
    if not changes:
        return []
    codes = list(changes.keys())
    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    rows = session.execute(
        text(
            f"SELECT scm.ts_code, scm.concept_name FROM stock_concept_members scm "
            f"WHERE scm.ts_code IN ({placeholders})"
        ),
        {f"c{i}": c for i, c in enumerate(codes)},
    ).fetchall()

    from collections import defaultdict
    conc_data: dict[str, list[float]] = defaultdict(list)
    for ts_code, concept_name in rows:
        chg = changes.get(ts_code)
        if chg is not None:
            conc_data[concept_name].append(chg)

    result = []
    for conc, vals in conc_data.items():
        if len(vals) < 5:
            continue
        avg = sum(vals) / len(vals)
        result.append({"concept": conc, "stock_cnt": len(vals), "avg_pct": round(avg, 2)})

    result.sort(key=lambda x: x["avg_pct"], reverse=True)
    return result


# ═══ LLM Prompt 构建 ══════════════════════════════════════════════


def _build_forecast_prompt(
    trade_date: date,
    index_summaries: list[dict[str, Any]],
    breadth: dict[str, Any],
    northbound: list[dict[str, Any]],
    industries: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    global_summaries: list[dict[str, Any]] | None = None,
    policy_news: list[dict[str, str]] | None = None,
) -> str:
    """构建 LLM 预测 prompt。"""
    # 精简指数数据（只传摘要，不传完整 K 线）
    ix_lines = []
    for ix in index_summaries:
        pct_str = f"{ix.get('pct_chg'):+.2f}%" if ix.get('pct_chg') is not None else "?"
        amt_str = f"({ix.get('change_amt'):+.2f}点)" if ix.get('change_amt') is not None else ""
        ix_lines.append(
            f"- {ix['name']}({ix['code']}): close={ix.get('close')}, "
            f"当日涨跌={pct_str}{amt_str}, ret_5d={ix.get('ret_5d')}, ret_20d={ix.get('ret_20d')}, "
            f"MA20={ix.get('ma20')}, vol_ratio={ix.get('vol_ratio20')}, RSI14={ix.get('rsi14')}"
        )
        if ix.get("patterns"):
            p = ix["patterns"]
            ix_lines.append(f"  历史相似形态: {p.get('summary', {}).get('verdict', '?')} "
                            f"({p.get('summary', {}).get('win_count', 0)}/"
                            f"{p.get('summary', {}).get('total', 0)} 上涨, "
                            f"平均收益 {p.get('summary', {}).get('avg_return_20d', 0):+.1f}%)")

    industry_lines = []
    for ind in industries[:8]:
        industry_lines.append(f"- {ind['industry']}: {ind['avg_pct']:+.2f}% ({ind['up_n']}↑/{ind['down_n']}↓)")
    for ind in industries[-5:]:
        if ind not in industries[:8]:
            industry_lines.append(f"- {ind['industry']}: {ind['avg_pct']:+.2f}% ({ind['up_n']}↑/{ind['down_n']}↓)")

    concept_lines = []
    for c in concepts[:5]:
        concept_lines.append(f"- {c['concept']}: {c['avg_pct']:+.2f}% ({c['stock_cnt']}只)")
    if len(concepts) > 5:
        concept_lines.append(f"... 共 {len(concepts)} 个概念板块")
        for c in concepts[-3:]:
            concept_lines.append(f"- {c['concept']}: {c['avg_pct']:+.2f}%")

    nb_lines = [f"- {n['date']}: {n['value']:+.1f}亿" for n in northbound] if northbound else ["（无数据）"]

    global_lines = []
    for gix in global_summaries:
        if gix.get("close"):
            g_pct = f"{gix.get('pct_chg'):+.2f}%" if gix.get('pct_chg') is not None else "?"
            g_ret5 = f"{gix.get('ret_5d'):+.2f}%" if gix.get('ret_5d') is not None else "?"
            g_date = gix.get('latest_date', '?')
            global_lines.append(f"- {gix['name']}({gix['code']}): close={gix.get('close')}, 涨跌={g_pct}, 5日={g_ret5}（数据至{g_date}）")
        else:
            global_lines.append(f"- {gix['name']}({gix['code']}): 数据暂缺")

    policy_lines = ["（无）"] if not policy_news else [
        f"- [{n['date']}] {n['title']}" for n in policy_news[:8]
    ]

    prompt = f"""你是 A 股市场策略分析师。请**严格基于以下数据**，对次日大盘走势和板块轮动做出预判。

【⚠️ 核心方法论 — 必须遵守】
1. **先列信号，后下结论**：先分别列出 bullish_signals 和 bearish_signals，每条标注强度（1-3），根据总权重对比得出方向。**禁止先有结论再挑信号。**
2. **权重校准**：看涨信号总权重和看跌信号总权重的**比例决定方向，差值决定置信度**。如果一侧信号全是 weight=3 而另一侧多是 weight=1，说明信号质量差异巨大，应更确信。
3. **判断规则（关键！）range-bound 不是默认值，必须有意使用**：
   - **range-bound 门槛最高**：仅当 bull_w 和 bear_w 差 ≤ 1（几乎完全平衡）时才能给 range-bound。日常交易中日线方向明确才是常态。
   - **bearish 门槛最低**（对抗数据天然偏多）：bear_w ≥ bull_w + 1 → bearish。只要空头信号略微占优就应看跌。
   - **bullish 门槛居中**：bull_w ≥ bear_w + 3 → bullish。多头信号需要清晰占优才能看涨。
   - 置信度：差 1-3 → 45-55%、差 4-6 → 55-65%、差 ≥ 7 → 65-75%
   - **犹豫时选 bearish 而不是 range-bound**：如果你在 bearish 和 range-bound 之间纠结 → 选 bearish。如果你在 bullish 和 range-bound 之间纠结 → 选 bullish。A 股极少出现真正的「平盘」。
5. **恐慌日特殊处理**：如果当日大跌（中位数 < -1.5%），这本身不是看跌理由（已经跌完了）。重点看历史相似形态：如果多数指数形态显示高胜率反弹（≥67% 胜率 + 正前向收益）→ 看涨（均值回归）；如果多数指数形态也显示低胜率 → 看跌（趋势延续）。
6. **今日涨跌 ≠ 明日方向**：当日涨跌是最弱的信号（weight=1），仅作背景参考。历史相似形态的前向收益才是最强的预测信号（weight=3）。
7. **反向自检**：下结论前问自己：「如果我的判断错了，最可能是因为什么？」把答案写入 key_uncertainty。

【数据截止日期】{trade_date}

【1. A 股七大指数走势】
{chr(10).join(ix_lines)}

【2. 市场宽度】
全市场 {breadth.get('total', '?')} 只 · 上涨 {breadth.get('up_n', '?')} / 下跌 {breadth.get('down_n', '?')} · 中位数 {breadth.get('median_pct', 0):+.2f}%

【3. 北向资金近 5 日】
{chr(10).join(nb_lines)}

【4. 行业板块表现（Top 8 + Bottom 5）】
{chr(10).join(industry_lines)}

【5. 概念板块热度（Top 5 + Bottom 3）】
{chr(10).join(concept_lines)}

【6. 外盘关联（隔夜美股 + 港股）】
{chr(10).join(global_lines)}

【7. 政策/宏观新闻（近 2 日）】
{chr(10).join(policy_lines)}

【任务】
1. market_direction: 先列 bullish_signals（看涨信号）和 bearish_signals（看跌信号），每条标注 weight(1-3)，然后基于信号对比给出 direction + confidence + reasoning + key_uncertainty
2. index_forecasts: 对每个指数给出方向 + 关键支撑/压力位
3. sector_calls: 3-5 个看好/看淡的行业板块，每条附 evidence
4. concept_calls: 3-5 个值得关注的概念题材
5. risk_factors: 当前宏观/政策/外盘风险点（2-4 条）
6. next_day_scenarios: 次日 2-3 种走势情景 + 概率 + 触发条件"""
    return prompt


# ═══ 主流程 ════════════════════════════════════════════════════════


def _get_policy_news(session, days: int = 2) -> list[dict[str, str]]:
    """查询近 N 日政策/宏观相关新闻。"""
    try:
        rows = session.execute(
            text(
                "SELECT title, published_at_utc FROM financial_alerts "
                "WHERE (title LIKE '%央行%' OR title LIKE '%降准%' OR title LIKE '%降息%' "
                "  OR title LIKE '%证监会%' OR title LIKE '%国务院%' OR title LIKE '%政治局%' "
                "  OR title LIKE '%LPR%' OR title LIKE '%关税%' OR title LIKE '%美联储%' "
                "  OR title LIKE '%加息%' OR title LIKE '%GDP%' OR title LIKE '%CPI%' "
                "  OR title LIKE '%PMI%' OR title LIKE '%逆回购%' OR title LIKE '%MLF%' "
                "  OR title LIKE '%汇率%' OR title LIKE '%人民币%') "
                "AND published_at_utc >= datetime('now', :days) "
                "ORDER BY published_at_utc DESC LIMIT 15"
            ),
            {"days": f"-{days} days"},
        ).fetchall()
        return [{"title": r[0], "date": str(r[1])[:16] if r[1] else "?"} for r in rows]
    except Exception:
        return []


def _build_global_summary(
    index_code: str,
    bars_df,
) -> dict[str, Any]:
    """构建外盘指数摘要（精简版）。"""
    name = _INDEX_NAMES.get(index_code, index_code)
    if bars_df.empty or len(bars_df) < 2:
        return {"code": index_code, "name": name, "close": None}

    recent = bars_df.tail(20)
    close = float(recent["close"].iloc[-1]) if "close" in recent.columns else None

    pct_chg = None
    if close and len(bars_df) >= 2:
        prev_close = float(bars_df["close"].iloc[-2])
        if prev_close and prev_close > 0:
            pct_chg = round((close - prev_close) / prev_close * 100, 2)

    ret_5d = None
    if close and len(recent) >= 6:
        ret_5d = round((close / float(recent["close"].iloc[-6]) - 1) * 100, 2)

    latest_date = str(recent.index[-1])[:10] if hasattr(recent.index[-1], '__str__') else str(bars_df["trade_date"].iloc[-1])

    return {
        "code": index_code,
        "name": name,
        "close": close,
        "pct_chg": pct_chg,
        "ret_5d": ret_5d,
        "latest_date": latest_date,
    }


def _build_index_summary(
    index_code: str,
    trade_date: date,
    bars_df,
    similar_patterns: dict[str, Any] | None,
) -> dict[str, Any]:
    """构建单个指数的摘要数据。"""
    name = _INDEX_NAMES.get(index_code, index_code)

    if bars_df.empty or len(bars_df) < 5:
        return {"code": index_code, "name": name, "close": None, "pct_chg": None}

    recent = bars_df.tail(60)
    close = float(recent["close"].iloc[-1]) if "close" in recent.columns else None

    # 基础指标
    ret_5d = None
    ret_20d = None
    ma20 = None
    if len(recent) >= 20:
        ret_5d = round((float(recent["close"].iloc[-1]) / float(recent["close"].iloc[-6]) - 1) * 100, 2) if len(recent) >= 6 else None
        ret_20d = round((float(recent["close"].iloc[-1]) / float(recent["close"].iloc[-21]) - 1) * 100, 2)
        ma20 = round(float(recent["close"].tail(20).mean()), 2) if "close" in recent.columns else None

    pct_chg = None
    change_amt = None
    # 优先用已有 pct_chg，否则从 close 推算
    if "pct_chg" in recent.columns and recent["pct_chg"].iloc[-1] is not None:
        pct_chg = round(float(recent["pct_chg"].iloc[-1]), 2)
    elif close and len(bars_df) >= 2:
        prev_close = float(bars_df["close"].iloc[-2])
        if prev_close and prev_close > 0:
            pct_chg = round((close - prev_close) / prev_close * 100, 2)
            change_amt = round(close - prev_close, 2)

    # 技术指标（如果 enrich_bars 已计算）
    vol_ratio20 = None
    rsi14 = None
    try:
        from zplan_shared.features import enrich_bars, latest_features
        enriched = enrich_bars(bars_df)
        feats = latest_features(enriched)
        vol_ratio20 = round(feats.get("vol_ratio20", 0), 2) if feats.get("vol_ratio20") is not None else None
        rsi14 = round(feats.get("rsi14", 0), 1) if feats.get("rsi14") is not None else None
    except Exception:
        pass

    summary: dict[str, Any] = {
        "code": index_code,
        "name": name,
        "close": close,
        "pct_chg": pct_chg,
        "change_amt": change_amt,
        "ret_5d": ret_5d,
        "ret_20d": ret_20d,
        "ma20": ma20,
        "vol_ratio20": vol_ratio20,
        "rsi14": rsi14,
    }

    if similar_patterns and similar_patterns.get("matches"):
        summary["patterns"] = similar_patterns

    return summary


def _save_forecast(
    session,
    as_of_date: date,
    forecast: dict[str, Any],
    chart_paths: dict[str, str] | None,
) -> int:
    """存入 market_forecasts 表。"""
    direction = forecast.get("market_direction", {}).get("direction", "")
    confidence = forecast.get("market_direction", {}).get("confidence", 0)

    existing = session.execute(
        select(MarketForecast).where(MarketForecast.as_of_date == as_of_date)
    ).scalar()

    if existing:
        existing.market_direction = direction
        existing.direction_confidence = confidence
        existing.forecast_json = json.dumps(forecast, ensure_ascii=False)
        if chart_paths:
            existing.index_charts_json = json.dumps(chart_paths, ensure_ascii=False)
        session.commit()
        return existing.id

    mf = MarketForecast(
        as_of_date=as_of_date,
        market_direction=direction,
        direction_confidence=confidence,
        forecast_json=json.dumps(forecast, ensure_ascii=False),
        index_charts_json=json.dumps(chart_paths, ensure_ascii=False) if chart_paths else None,
        push_sent=False,
        created_at_utc=datetime.now(timezone.utc),
    )
    session.add(mf)
    session.commit()
    return mf.id


# ═══ TOP10 选股加载 ──────────────────────────────────────────────

_SKIP_CONCEPTS = {
    "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
    "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
    "央国企改革", "黑龙江", "深圳特区", "机械设备", "通信", "电子", "计算机",
    "公用事业", "电力", "基础化工", "化学制品", "元件", "通信技术", "通信设备",
}


def _load_top10_with_context(session) -> list[dict[str, Any]]:
    """加载最新 TOP10 选股，含行业/概念/PE/市值。"""
    run = session.execute(
        select(PickRun)
        .where(PickRun.run_kind.in_(["scan", "llm_top300"]))
        .order_by(desc(PickRun.created_at_utc))
        .limit(1)
    ).scalars().first()

    if not run:
        return []

    entries = session.execute(
        select(PickEntry)
        .where(PickEntry.run_id == run.id)
        .order_by(PickEntry.rank_in_run, PickEntry.final_composite_score.desc().nullslast())
        .limit(10)
    ).scalars().all()

    if not entries:
        return []

    codes = [e.ts_code for e in entries]
    items = []
    for e in entries:
        code = e.ts_code
        name = e.name or code
        score = e.final_composite_score or e.rule_composite_score
        close = e.close_price
        verdict = e.verdict or ""
        items.append({
            "ts_code": code, "name": name, "score": score,
            "close": close, "verdict": verdict,
            "predicted_buy": e.predicted_buy_price,
            "predicted_target": e.predicted_target_price,
        })

    # 行业
    industry_map: dict[str, str] = {}
    rows = session.execute(
        select(StockList.ts_code, StockList.industry)
        .where(StockList.ts_code.in_(codes))
    ).all()
    industry_map = {r.ts_code: (r.industry or "") for r in rows}

    # PE / 市值
    pe_map: dict[str, float] = {}
    mv_map: dict[str, float] = {}
    latest_snap = session.execute(
        text("SELECT MAX(trade_date) FROM daily_snapshot")
    ).scalar()
    if latest_snap and codes:
        placeholders = ",".join(f":c{i}" for i in range(len(codes)))
        params = {f"c{i}": c for i, c in enumerate(codes)}
        params["d"] = str(latest_snap)
        rows = session.execute(
            text(
                f"SELECT ts_code, pe_ttm, total_mv FROM daily_snapshot "
                f"WHERE ts_code IN ({placeholders}) AND trade_date = :d"
            ),
            params,
        ).fetchall()
        for r in rows:
            if r[1] is not None:
                pe_map[r[0]] = float(r[1])
            if r[2] is not None:
                mv_map[r[0]] = float(r[2])

    # 概念
    concept_map: dict[str, list[str]] = {}
    rows = session.execute(
        select(StockConceptMember.ts_code, StockConceptMember.concept_name)
        .where(StockConceptMember.ts_code.in_(codes))
    ).all()
    for r in rows:
        if r[1] not in _SKIP_CONCEPTS:
            concept_map.setdefault(r[0], []).append(r[1])

    for item in items:
        code = item["ts_code"]
        item["industry"] = industry_map.get(code, "")
        item["pe_ttm"] = pe_map.get(code)
        item["total_mv"] = mv_map.get(code)
        item["concepts"] = concept_map.get(code, [])[:2]

    return items


def _format_picks_and_actions(
    entries: list[dict[str, Any]],
    forecast_direction: str,
) -> list[str]:
    """构建 TOP10 紧凑表格 + 操作建议。"""
    lines: list[str] = []

    if not entries:
        return lines

    # ── 紧凑 TOP10 表格 ──
    lines.append("### 📈 今日 TOP10")
    lines.append("")
    lines.append("| # | 股票 | 评分 | 行业 | PE | 收盘 |")
    lines.append("|---|------|------|------|----|------|")
    for i, item in enumerate(entries):
        name = (item.get("name") or item.get("ts_code", "?"))[:8]
        code = item.get("ts_code", "?")
        score = item.get("score")
        score_s = f"{score:.0f}" if score is not None else "--"
        industry = item.get("industry", "")[:6]
        pe = item.get("pe_ttm")
        pe_s = f"{pe:.0f}" if (pe is not None and pe > 0) else "--"
        close = item.get("close")
        close_s = f"¥{close:.2f}" if close is not None else "--"
        lines.append(f"| {i+1} | {name}({code}) | {score_s} | {industry} | {pe_s} | {close_s} |")

    lines.append("")

    # ── 操作建议 ──
    direction_map = {
        "bullish": "🟢 大盘偏多，可积极选股",
        "bearish": "🔴 大盘偏空，谨慎防守",
        "range-bound": "🟡 大盘震荡，精选个股",
    }
    dir_label = direction_map.get(forecast_direction, "❓ 方向不明")
    lines.append("### 💡 操作建议")
    lines.append("")
    lines.append(f"**{dir_label}**")
    lines.append("")

    # 根据方向 + 选股给出具体建议
    if entries:
        # 低 PE 的票（防御型）
        low_pe = [e for e in entries if e.get("pe_ttm") and e["pe_ttm"] > 0 and e["pe_ttm"] < 30]
        # 强势板块（概念多）
        strong = [e for e in entries if e.get("concepts")]

        if forecast_direction == "bullish":
            candidates = strong[:3] if strong else entries[:3]
            names = [f"{e['name']}({e['ts_code']})" for e in candidates]
            lines.append(f"- 优先关注: {', '.join(names)}")
            lines.append("- 建议买入价: 昨收×0.98（偏多市场滑点小）")
            lines.append("- 止损: 买入价下方 2%")
        elif forecast_direction == "bearish":
            if low_pe:
                names = [f"{e['name']}({e['ts_code']})" for e in low_pe[:2]]
                lines.append(f"- 防御关注: {', '.join(names)}（低PE）")
            lines.append("- ⚠️ 建议观望，等待止跌信号")
            lines.append("- 若要入场: 买入价收紧到昨收×0.96")
            lines.append("- 止损: 买入价下方 1.5%")
        else:
            lines.append("- 仓位: 控制在 5 成以下")
            lines.append("- 建议买入价: 昨收×0.99（等回调不追高）")
            if low_pe:
                names = [f"{e['name']}({e['ts_code']})" for e in low_pe[:3]]
                lines.append(f"- 精选标的: {', '.join(names)}（低PE优先）")

    lines.append("")
    return lines


# ═══ Push Markdown ══════════════════════════════════════════════════


def _format_wechat_markdown(
    forecast: dict[str, Any],
    as_of_date: date,
    chart_paths: dict[str, str] | None = None,
    index_summaries: list[dict[str, Any]] | None = None,
    top10_entries: list[dict[str, Any]] | None = None,
) -> str:
    """格式化企微推送 markdown。"""
    beijing_now = datetime.now(BEIJING_TZ)
    date_str = beijing_now.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    md = forecast.get("market_direction", {})
    direction_map = {"bullish": "🟢 看涨", "bearish": "🔴 看跌", "range-bound": "🟡 震荡"}
    direction_label = direction_map.get(md.get("direction", ""), md.get("direction", "?"))

    lines = [
        f"## 🔮 大盘预测 {date_str} {weekday}",
        f"> 数据截止: **{as_of_date}** · 预测次日走势",
        "",
        f"### 📊 综合判断: {direction_label}（置信度 {md.get('confidence', '?')}%）",
        f"> {md.get('reasoning', '')}",
        "",
    ]

    # ── 看涨 / 看跌 分栏对照 ──
    evidence = md.get("evidence") or []
    counter = md.get("counter_evidence") or []
    if evidence or counter:
        lines.append(f"### ⚖️ 多空对照")
        lines.append("")
        lines.append("| 🔺 看涨理由 | 🔻 看跌风险 |")
        lines.append("|-------------|-------------|")
        max_rows = max(len(evidence), len(counter))
        for i in range(max_rows):
            bull = evidence[i] if i < len(evidence) else None
            bear = counter[i] if i < len(counter) else None
            bull_str = f"[{bull.get('type', '')}] {bull.get('signal', '')}: {bull.get('value', '')}" if bull else ""
            bear_str = bear if bear else ""
            lines.append(f"| {bull_str} | {bear_str} |")
        lines.append("")

    # ── 指数整体分析 ──
    ix_forecasts = forecast.get("index_forecasts") or []
    if ix_forecasts:
        # 聚合统计
        bullish_ix = [ix for ix in ix_forecasts if ix.get("direction") == "偏多"]
        bearish_ix = [ix for ix in ix_forecasts if ix.get("direction") == "偏空"]
        neutral_ix = [ix for ix in ix_forecasts if ix.get("direction") == "震荡"]
        avg_conf = sum(ix.get("confidence", 0) for ix in ix_forecasts) / len(ix_forecasts) if ix_forecasts else 0
        # 找领涨和拖后腿的
        bullish_names = [ix["name"] for ix in bullish_ix] if bullish_ix else []
        bearish_names = [ix["name"] for ix in bearish_ix] if bearish_ix else []

        lines.append("### 🏛️ 指数全景分析")
        lines.append("")
        # 方向分布
        b_cnt = len(bullish_ix)
        s_cnt = len(bearish_ix)
        n_cnt = len(neutral_ix)
        bar_bull = "█" * b_cnt
        bar_neut = "░" * n_cnt
        bar_bear = "▁" * s_cnt
        lines.append(f"🔺 偏多 {b_cnt}只 {bar_bull} ➖ 震荡 {n_cnt}只 {bar_neut} 🔻 偏空 {s_cnt}只 {bar_bear}")
        lines.append(f"平均置信度: **{avg_conf:.0f}%**")

        # 格局判断
        if b_cnt >= 5:
            pattern = "普涨格局，各指数共振向上，反弹可信度较高"
        elif s_cnt >= 5:
            pattern = "普跌格局，各指数共振向下，系统性风险较大"
        elif b_cnt > s_cnt and b_cnt >= 3:
            pattern = f"分化偏多：{', '.join(bullish_names[:3])}领涨，{', '.join(bearish_names[:2]) if bearish_names else ''}偏弱"
        elif s_cnt > b_cnt and s_cnt >= 3:
            pattern = f"分化偏空：{', '.join(bearish_names[:3])}领跌，{', '.join(bullish_names[:2]) if bullish_names else ''}抗跌"
        else:
            pattern = "方向分歧，各指数走势不一致，市场缺乏共识"
        lines.append(f"> {pattern}")
        lines.append("")

        # 详细列表
        lines.append("| 指数 | 方向 | 置信 | 历史相似形态 | 支撑/压力 |")
        lines.append("|------|------|------|-------------|-----------|")
        for ix in ix_forecasts:
            emoji = {"偏多": "🔺", "偏空": "🔻", "震荡": "➖"}.get(ix.get("direction", ""), "❓")
            sp = ix.get("similar_patterns_verdict", "") or "-"
            levels = ix.get("key_levels") or {}
            lvl_str = f"{levels['support']:.0f}/{levels['resistance']:.0f}" if levels.get("support") and levels.get("resistance") else "-"
            lines.append(
                f"| {emoji} {ix.get('name', '')} | {ix.get('direction', '')} | "
                f"{ix.get('confidence', '?')}% | {sp} | {lvl_str} |"
            )
        lines.append("")

    # 板块
    sectors = forecast.get("sector_calls") or []
    if sectors:
        lines.append("### 🏭 板块判断")
        lines.append("")
        for s in sectors:
            emoji = {"看多": "🟢", "看淡": "🔴", "中性": "⚪"}.get(s.get("direction", ""), "")
            lines.append(f"- {emoji} **{s.get('sector', '')}** ({s.get('direction', '')}, "
                         f"置信{s.get('confidence', '?')}%): {s.get('reasoning', '')}")
        lines.append("")

    # 概念
    concepts = forecast.get("concept_calls") or []
    if concepts:
        lines.append("### 💡 概念关注")
        lines.append("")
        for c in concepts:
            lines.append(f"- **{c.get('concept', '')}** ({c.get('direction', '')}): {c.get('reasoning', '')}")
        lines.append("")

    # 风险
    risks = forecast.get("risk_factors") or []
    if risks:
        lines.append("### ⚠️ 风险因素")
        lines.append("")
        for r in risks:
            lines.append(f"- {r}")
        lines.append("")

    # 情景
    scenarios = forecast.get("next_day_scenarios") or []
    if scenarios:
        lines.append("### 🎯 次日情景")
        lines.append("")
        for s in scenarios:
            lines.append(f"- **{s.get('scenario', '')}**（概率 {s.get('probability', '?')}%）: {s.get('trigger', '')}")
        lines.append("")

    # ── 选股参考（有真实 TOP10 则替换，否则用通用建议）──
    if top10_entries:
        lines.extend(_format_picks_and_actions(top10_entries, md.get("direction", "")))
    else:
        dir_signal = md.get("direction", "")
        pick_guide = {
            "bullish": ("🟢 大盘偏多，积极选股", [
                "可以提高仓位比例，优先选强势板块（当日涨幅>1%的行业）",
                "建议买入价可以适当放宽到 close×0.98（正常滑点）",
                "重点看历史相似形态偏多的指数对应标的",
            ]),
            "bearish": ("🔴 大盘偏空，谨慎防守", [
                "降低仓位或暂停新开仓，等待止跌信号",
                "已有持仓注意止损位，建议收紧到支撑位下方 1%",
                "可关注逆势板块（今日仍上涨的低位行业）做防御配置",
            ]),
            "range-bound": ("🟡 大盘震荡，精选个股", [
                "控制仓位在 5 成以下，重点选强势板块中的低吸标的",
                "建议买入价收紧到 close×0.99（等回调不追高）",
                "板块轮动快，优先选当日涨幅居中（0.5-2%）的概念",
            ]),
        }.get(dir_signal, ("❓ 方向不明", ["等待更明确信号后再操作"]))

        lines.append("### 📋 选股参考（这意味着什么）")
        lines.append("")
        lines.append(f"**{pick_guide[0]}**")
        lines.append("")
        for tip in pick_guide[1]:
            lines.append(f"- {tip}")
        lines.append("")

    # 指数今日实况（涨跌幅+点数）
    ix_list = index_summaries or []
    if ix_list:
        lines.append("### 📈 今日指数实况")
        lines.append("")
        for ix in ix_list:
            name = ix.get("name", "?")
            close_v = ix.get("close")
            pct = ix.get("pct_chg")
            amt = ix.get("change_amt")
            if pct is not None:
                arrow = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
                amt_str = f" {amt:+.2f}点" if amt is not None else ""
                lines.append(f"- {arrow} **{name}**: {close_v:.2f}  {pct:+.2f}%{amt_str}")
            elif close_v:
                lines.append(f"- **{name}**: {close_v:.2f}")
        lines.append("")

    lines.append("---")
    lines.append("💡 上证形态对比图 + 板块热力图见附件 · 次日盘后自动对照验证")

    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv
    no_push = "--no-push" in sys.argv or dry_run
    with_picks = "--with-picks" in sys.argv

    init_db()

    # 1. 确定交易日期
    trade_date = latest_index_trade_date()
    if trade_date is None:
        logger.error("daily_index 无数据，请先运行 etl_index")
        sys.exit(1)

    logger.info("预测日期: %s", trade_date)

    with SessionLocal() as session:
        # 2. 拉取市场数据（先预计算 pct_chg）
        changes = _get_daily_changes(session, trade_date)
        breadth = _get_market_breadth(changes)
        northbound = _get_northbound_recent(session, days=5)
        industries = _get_industry_performance(session, changes)
        concepts = _get_concept_heat(session, changes)
        policy_news = _get_policy_news(session, days=2)

    logger.info("市场宽度: %s 只, 上涨 %s, 下跌 %s, 中位数 %+.2f%%",
                breadth.get("total"), breadth.get("up_n"), breadth.get("down_n"), breadth.get("median_pct", 0))
    logger.info("北向数据: %s 条", len(northbound))
    logger.info("行业板块: %s 个", len(industries))
    logger.info("概念板块: %s 个", len(concepts))

    # 3. 指数 K 线 + 相似形态搜索
    index_summaries: list[dict[str, Any]] = []
    chart_paths: dict[str, str] = {}

    for code in _A_INDEX_ORDER:
        try:
            bars_df = get_index_bars(code, lookback=365)
        except Exception as exc:
            logger.warning("拉取指数 %s K 线失败: %s", code, exc)
            index_summaries.append({"code": code, "name": _INDEX_NAMES.get(code, code)})
            continue

        # 相似形态搜索
        patterns = None
        try:
            from zplan_shared.pattern_similarity import search_similar_index_patterns
            patterns = search_similar_index_patterns(code, top_k=3, min_similarity=0.65)
            if patterns.get("matches"):
                logger.info("指数 %s 相似形态: %s (胜率 %.0f%%)",
                            code, patterns["summary"].get("verdict", "?"),
                            patterns["summary"].get("win_rate", 0) * 100)
        except Exception as exc:
            logger.warning("指数 %s 相似形态搜索失败: %s", code, exc)

        summary = _build_index_summary(code, trade_date, bars_df, patterns)
        index_summaries.append(summary)

        # 生成图表
        if not dry_run:
            try:
                from zplan_shared.chart_viz import plot_index_chart
                paths = plot_index_chart(code, lookback=120)
                chart_paths[code] = paths
                logger.info("指数 %s 图表已生成: %s", code, paths.get("macd", ""))
            except Exception as exc:
                logger.warning("指数 %s 图表生成失败: %s", code, exc)

    # 板块热力图
    if not dry_run and industries:
        try:
            from zplan_shared.chart_viz import plot_sector_heatmap
            heatmap_path = plot_sector_heatmap(industries, top_n=20)
            chart_paths["sector_heatmap"] = heatmap_path
            logger.info("板块热力图: %s", heatmap_path)
        except Exception as exc:
            logger.warning("板块热力图生成失败: %s", exc)

    # 3b. 外盘指数摘要
    global_summaries: list[dict[str, Any]] = []
    for code in _GLOBAL_INDEX_ORDER:
        try:
            bars_df = get_index_bars(code, lookback=60)
            gs = _build_global_summary(code, bars_df)
            global_summaries.append(gs)
            if gs.get("close"):
                logger.info("外盘 %s: close=%s, pct=%.2f%%", code, gs.get("close"), gs.get("pct_chg", 0))
        except Exception as exc:
            logger.warning("拉取外盘 %s 失败: %s", code, exc)
            global_summaries.append({"code": code, "name": _INDEX_NAMES.get(code, code)})

    logger.info("政策新闻: %s 条", len(policy_news))

    # 4. LLM 预测
    if not llm_available():
        logger.error("LLM 不可用（未配置 API Key），跳过预测")
        sys.exit(1)

    prompt = _build_forecast_prompt(trade_date, index_summaries, breadth, northbound,
                                    industries, concepts, global_summaries, policy_news)

    if dry_run:
        logger.info("=" * 60)
        logger.info("[DRY RUN] LLM Prompt:")
        logger.info(prompt)
        logger.info("=" * 60)
        return

    logger.info("调用 LLM 预测...")
    try:
        result = generate_json(
            prompt=prompt,
            response_schema=_FORECAST_SCHEMA,
            temperature=0.3,
            max_output_tokens=8192,
            model=_LLM_MODEL,
        )
        usage = pop_usage(result)
        logger.info("LLM 用量: %s", usage)
    except Exception as exc:
        logger.error("LLM 预测失败: %s", exc)
        sys.exit(1)

    if not isinstance(result, dict) or not result.get("market_direction"):
        logger.error("LLM 返回格式异常: %s", str(result)[:200])
        sys.exit(1)

    # 5. 落库
    with SessionLocal() as session:
        forecast_id = _save_forecast(session, trade_date, result, chart_paths if chart_paths else None)
        session.commit()
    logger.info("预测已入库: id=%s, 方向=%s", forecast_id,
                result.get("market_direction", {}).get("direction", "?"))

    # 5b. 加载 TOP10 选股（--with-picks）
    top10_entries: list[dict[str, Any]] | None = None
    if with_picks:
        try:
            with SessionLocal() as session:
                top10_entries = _load_top10_with_context(session)
                logger.info("TOP10 选股: %s 只", len(top10_entries) if top10_entries else 0)
        except Exception as exc:
            logger.warning("加载 TOP10 选股失败: %s", exc)

    # 6. 企微推送
    if not no_push:
        markdown = _format_wechat_markdown(
            result, trade_date, chart_paths, index_summaries,
            top10_entries=top10_entries,
        )
        try:
            from wechat_push import push_wechat_markdown, push_wechat_image

            # 先推文字预测
            ok = push_wechat_markdown(markdown)
            if ok:
                logger.info("企微文字推送成功")

            # 再推关键图表：上证 MACD/相似图 + 板块热力图
            import time as _time
            for label, key in [("上证指数形态对比", "000001"), ("板块热力图", "sector_heatmap")]:
                paths = chart_paths.get(key)
                if not paths:
                    continue
                # chart_paths["000001"] = {"kline": ..., "macd": ...}
                img_path = paths.get("macd") if isinstance(paths, dict) else paths
                if img_path and Path(img_path).exists():
                    _time.sleep(1.5)  # 企微限速
                    try:
                        push_wechat_image(str(img_path))
                        logger.info("企微图片推送成功: %s (%s)", label, img_path)
                    except Exception as ie:
                        logger.warning("企微图片推送失败 %s: %s", label, ie)

            with SessionLocal() as session:
                mf = session.get(MarketForecast, forecast_id)
                if mf:
                    mf.push_sent = True
                    session.commit()
        except Exception as exc:
            logger.warning("企微推送异常: %s", exc)

    logger.info("大盘预测完成！")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
