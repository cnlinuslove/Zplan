"""单票研究报告：技术面为核心，资讯/财务为可扩展模块。"""
from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import select

from zplan_shared.fundamentals import get_financials, get_snapshot
from zplan_shared.market import get_bars, resolve_ts_code
from zplan_shared.market_health import assert_market_ready
from zplan_shared.models import SessionLocal, StockList, init_db
from zplan_shared.pick_context import get_pick_context

from pick_agent.profile import get_company_profile
from pick_agent.scoring import (
    composite_score,
    financial_score_from_rows,
    industry_relative_score,
    intraday_adjust,
    news_score,
)
from pick_agent.strategy import PickStrategy, load_strategy
from pick_agent.technical import analyze_technical, price_levels


class InsufficientBarsError(ValueError):
    pass


def _stock_meta(ts_code: str) -> dict[str, Any]:
    init_db()
    code = resolve_ts_code(ts_code)
    with SessionLocal() as session:
        row = session.execute(select(StockList).where(StockList.ts_code == code)).scalar_one_or_none()
    if not row:
        return {"ts_code": code, "name": None, "industry": None, "listing_date": None}
    return {
        "ts_code": row.ts_code,
        "name": row.name,
        "industry": row.industry,
        "listing_date": str(row.listing_date) if row.listing_date else None,
    }


def _financial_rows(ts_code: str, limit: int = 8) -> list[dict[str, Any]]:
    df = get_financials(ts_code, limit=limit)
    if df.empty:
        return []
    return [
        {
            "report_date": str(r["report_date"]),
            "pe_ttm": r.get("pe_ttm"),
            "pb": r.get("pb"),
            "revenue": r.get("revenue"),
            "net_profit": r.get("net_profit"),
            "roe": r.get("roe"),
        }
        for _, r in df.iterrows()
    ]


def _trend_narrative(bars: pd.DataFrame) -> str:
    if len(bars) < 5:
        return "历史 K 线不足，无法描述趋势。"
    tail = bars.tail(60)
    start_c = float(tail["close"].iloc[0])
    end_c = float(tail["close"].iloc[-1])
    chg = (end_c / start_c - 1) * 100 if start_c else 0
    direction = "上涨" if chg > 3 else ("下跌" if chg < -3 else "震荡")
    return (
        f"近 60 交易日收盘由 {start_c:.2f} 至 {end_c:.2f}，区间涨跌约 {chg:+.2f}%，整体呈{direction}。"
        f"数据来源：zplan.db daily_prices（前复权）。"
    )


def _scenario_strategies(levels: dict[str, float | None], tech_verdict: str) -> list[str]:
    buy = levels.get("suggested_buy")
    target = levels.get("target_price")
    stop = levels.get("stop_loss")
    strategies = [
        f"基准情景（{tech_verdict}）：收盘价站稳 MA20 且 KDJ 金叉延续时，可沿趋势持有，参考目标价 {target}。",
        f"回调情景：回落至建议买入区 {buy} 附近且缩量，可考虑分批布局。",
        f"破位情景：跌破止损参考 {stop} 或 MACD 柱持续走弱，减仓或观望。",
        "突发利空：若 48h 资讯命中数骤增且股价放量下跌，优先风控而非加仓。",
    ]
    return strategies


def build_research_report(
    ts_code: str,
    *,
    news_hours: int = 48,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
    min_bars: int | None = None,
) -> dict[str, Any]:
    """生成结构化研究报告（供 CLI / 后续 LLM 润色）。"""
    strat = strategy or load_strategy()
    min_bars = min_bars if min_bars is not None else strat.min_bars
    if not skip_health_check:
        assert_market_ready(
            min_panel_rows=strat.min_panel_rows,
            max_stale_days=strat.max_stale_days,
        )

    meta = _stock_meta(ts_code)
    code = meta["ts_code"]
    bars = get_bars(code)
    if len(bars) < min_bars:
        raise InsufficientBarsError(
            f"{meta.get('name') or code} 日线不足 {min_bars} 根（当前 {len(bars)}）"
        )

    tech = analyze_technical(code, min_bars=min_bars)
    levels = price_levels(bars)
    ctx = get_pick_context(code, news_hours=news_hours)
    profile = get_company_profile(code)
    fin_rows = _financial_rows(code)
    fin_sc, fin_note = financial_score_from_rows(fin_rows)
    if not fin_rows:
        fin_note = "库内暂无财报指标（需 Phase D：股价 Agent 季报 ETL）"

    news_sc, _ = news_score(ctx)
    industry_map = {code: meta.get("industry") or ctx.get("industry")}
    ret_map: dict[str, list[float]] = {}
    if industry_map.get(code) and tech.features.get("ret_20d") is not None:
        ret_map[industry_map[code]] = [float(tech.features["ret_20d"])]
    ind_sc, ind_note = industry_relative_score(
        code, tech.features.get("ret_20d"), industry_map, ret_map
    )
    composite = composite_score(
        tech=tech,
        fin_score=fin_sc,
        news_sc=news_sc,
        industry_sc=ind_sc,
        intraday_adj=intraday_adjust(tech, ctx, strat),
        strategy=strat,
    )
    tech_score = tech.score
    news_total = int((ctx.get("news_mentions") or {}).get("total", 0))
    linked = ctx.get("news_linked") or {}

    snap = get_snapshot()
    snap_row = None
    if not snap.empty:
        sub = snap[snap["ts_code"] == code]
        if not sub.empty:
            snap_row = sub.iloc[0].to_dict()

    if composite >= 70:
        recommendation = "关注 / 逢低布局"
    elif composite >= 55:
        recommendation = "观望，等待信号确认"
    else:
        recommendation = "谨慎，暂不推荐追涨"

    data_gaps = [
        "公司官网/产品/团队/融资：需资讯 Agent 扩展结构化公司档案（见 DATA_ARCHITECTURE 资讯域）。",
        "板块相对强弱：需 stock_list.industry 填充 + 行业指数 ETL。",
        "机构持仓：需 Phase B daily_snapshot 或东财股东接口。",
    ]

    return {
        "meta": meta,
        "as_of": tech.as_of,
        "rule_version": strat.rule_version,
        "modules": {
            "1_基本信息": {
                "公司定位": (profile or {}).get("positioning") or "待资讯域补充",
                "行业": meta.get("industry") or ctx.get("industry") or "未入库",
                "上市日期": meta.get("listing_date") or ctx.get("listing_date"),
                "官网": (profile or {}).get("website"),
                "数据来源": "stock_list + company_profile",
            },
            "2_核心产品": {
                "核心产品": (profile or {}).get("core_products_json") or "待扩展",
                "news_mentions_48h": news_total,
            },
            "3_创始团队": {
                "团队": (profile or {}).get("team_json") or "待扩展",
            },
            "4_股价分析": {
                "趋势叙述": _trend_narrative(bars),
                "技术面结论": tech.verdict,
                "技术得分": tech_score,
                "行业相对": ind_note,
                "关键信号": tech.signals,
                "指标快照": tech.features,
                "分时特征": ctx.get("intraday"),
                "数据来源": "daily_prices + intraday + features",
            },
            "5_财务情况": {
                "近三年记录": fin_rows[:12],
                "财务得分": fin_sc,
                "评语": fin_note,
                "估值截面": snap_row,
                "数据来源": "financial_indicators + daily_snapshot",
            },
            "6_投资持仓": {
                "状态": snap_row or "待 Phase B 股东持仓 ETL",
            },
            "7_公司风险": {
                "技术风险": tech.signals,
                "舆情风险": linked.get("event_types") or {},
                "新闻条数_48h": news_total,
            },
            "8_核心竞争力": {
                "档案摘要": (profile or {}).get("positioning"),
                "舆情": linked,
            },
        },
        "投资建议": {
            "总结": (
                f"{meta.get('name') or code} 综合分 {composite}（技术 {tech_score}，"
                f"财务 {(fin_sc if fin_sc is not None else 50):.0f}，资讯 {news_sc:.0f}），技术面 {tech.verdict}。"
            ),
            "综合推荐分": composite,
            "操作建议": recommendation,
            "建议买入价": levels.get("suggested_buy"),
            "目标价": levels.get("target_price"),
            "止损参考": levels.get("stop_loss"),
            "走势应对": _scenario_strategies(levels, tech.verdict),
            "理由": tech.signals,
        },
        "data_gaps_for_other_agents": data_gaps,
    }


def format_report_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    title = meta.get("name") or meta["ts_code"]
    lines = [
        f"# {title}（{meta['ts_code']}）投资研究报告",
        "",
        f"> 数据截止：{report.get('as_of', '—')} | 规则：{report.get('rule_version', '—')} | "
        f"综合推荐分：**{report['投资建议']['综合推荐分']}**",
        "",
        "## 1. 公司基本信息",
        f"- 行业：{report['modules']['1_基本信息'].get('行业')}",
        f"- 上市日期：{report['modules']['1_基本信息'].get('上市日期') or '—'}",
        "",
        "## 4. 股价分析（核心）",
        report["modules"]["4_股价分析"]["趋势叙述"],
        "",
        f"**技术面**：{report['modules']['4_股价分析']['技术面结论']}（得分 {report['modules']['4_股价分析']['技术得分']}）",
    ]
    ind_rel = report["modules"]["4_股价分析"].get("行业相对")
    if ind_rel:
        lines.append(f"- {ind_rel}")
    lines.extend(["", "**关键信号**："])
    for sig in report["modules"]["4_股价分析"]["关键信号"] or ["（无显著信号）"]:
        lines.append(f"- {sig}")

    snap = report["modules"]["4_股价分析"]["指标快照"]
    if snap:
        lines.extend(["", "**指标快照**：", "```"])
        for k, v in snap.items():
            if v is not None:
                lines.append(f"{k}: {v}")
        lines.append("```")

    lines.extend(
        [
            "",
            "## 5. 财务情况",
            report["modules"]["5_财务情况"]["评语"],
            "",
            "## 7. 公司风险",
            f"- 48h 新闻：{report['modules']['7_公司风险'].get('新闻条数_48h', 0)} 条",
            "",
            "## 投资建议",
            report["投资建议"]["总结"],
            "",
            f"- **操作建议**：{report['投资建议']['操作建议']}",
            f"- **建议买入价**：{report['投资建议']['建议买入价']}",
            f"- **目标价**：{report['投资建议']['目标价']}",
            f"- **止损参考**：{report['投资建议']['止损参考']}",
            "",
            "### 不同走势应对",
        ]
    )
    for s in report["投资建议"]["走势应对"]:
        lines.append(f"- {s}")

    gaps = report.get("data_gaps_for_other_agents") or []
    if gaps:
        lines.extend(["", "## 其他 Agent 数据需求", ""])
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines)
