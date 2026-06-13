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


def _enrich_data(ts_code: str) -> dict[str, Any]:
    """从 enrich_company 表拉取 P0+P1 数据填充模块 2/6/8。"""
    result: dict[str, Any] = {
        "industry_peers": None,
        "research_reports": None,
        "institutional_holdings": None,
        "company_products": None,
    }
    try:
        from zplan_shared.enrich_company import build_enrich_prompt_section
        # 不直接拼 prompt text，而是用原始查询
    except ImportError:
        pass

    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parents[3] / "zplan-资讯" / "zplan.db"
        if not db_path.exists():
            return result
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row

        # enrich 表存的是纯数字代码（无后缀），统一格式
        code = str(ts_code).replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".HK", "")

        # Industry peers
        cur = db.execute(
            "SELECT * FROM industry_peers WHERE ts_code = ? ORDER BY as_of DESC LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
        if row:
            result["industry_peers"] = dict(row)

        # Research reports
        cur = db.execute(
            "SELECT * FROM research_reports WHERE ts_code = ? ORDER BY report_date DESC LIMIT 5",
            (code,),
        )
        rows = cur.fetchall()
        if rows:
            result["research_reports"] = [dict(r) for r in rows]

        # Institutional holdings
        cur = db.execute(
            "SELECT * FROM institutional_holdings WHERE ts_code = ? ORDER BY as_of DESC LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
        if row:
            result["institutional_holdings"] = dict(row)

        # Company products (P2)
        cur = db.execute(
            "SELECT * FROM company_products WHERE ts_code = ? LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
        if row:
            result["company_products"] = dict(row)

        db.close()
    except Exception:
        pass

    return result
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


def _format_product_data(products: dict[str, Any] | None) -> str | None:
    """格式化 P2 产品深度数据。"""
    if not products:
        return None
    parts = []
    if products.get("competitive_positioning"):
        parts.append(f"竞争定位：{products['competitive_positioning'][:200]}")
    if products.get("technology_moat"):
        parts.append(f"技术护城河：{products['technology_moat'][:200]}")
    if products.get("key_products_json"):
        try:
            kp = __import__("json").loads(products["key_products_json"])
            if isinstance(kp, list) and kp:
                parts.append(f"核心产品：{', '.join(str(x) for x in kp[:5])}")
        except Exception:
            pass
    if products.get("growth_catalysts"):
        parts.append(f"增长催化：{products['growth_catalysts'][:200]}")
    return "；".join(parts) if parts else None


def _format_holdings(holdings: dict[str, Any] | None) -> str | None:
    """格式化 P1 机构持仓数据。"""
    if not holdings:
        return None
    parts = []
    # 前三大股东
    if holdings.get("top_holders_json"):
        try:
            import json as _json
            holders = _json.loads(holdings["top_holders_json"])
            if holders:
                top3 = "、".join(f'{h["name"]}({h.get("pct","?")}%)' for h in holders[:3])
                parts.append(f"前三大股东：{top3}")
        except Exception:
            pass
    if holdings.get("fund_count") is not None:
        parts.append(f"基金持仓：{holdings['fund_count']} 只基金")
    if holdings.get("north_bound_pct") is not None:
        parts.append(f"北向持股：{holdings['north_bound_pct']:.1f}%")
    if holdings.get("north_bound_mv") is not None:
        parts.append(f"北向市值：{holdings['north_bound_mv']/1e8:.1f} 亿")
    return "；".join(parts) if parts else None


def _format_research_reports(reports: list[dict[str, Any]] | None) -> str | None:
    """格式化 P1 机构研报。"""
    if not reports:
        return None
    lines = []
    for r in reports[:3]:
        inst = r.get("institution") or "?"
        rating = r.get("rating") or "-"
        title = (r.get("title") or "")[:60]
        eps = ""
        if r.get("eps_2026"):
            eps += f" 2026E EPS={r['eps_2026']:.2f}"
        if r.get("eps_2027"):
            eps += f" 2027E EPS={r['eps_2027']:.2f}"
        lines.append(f"[{rating}] {inst}: {title}{eps}")
    return "\n".join(lines) if lines else None


def _format_industry_peers(peers: dict[str, Any] | None) -> str | None:
    """格式化 P0 行业对标数据。"""
    if not peers:
        return None
    parts = [f"行业: {peers.get('industry_name', '?')}（共 {peers.get('peer_count', '?')} 只）"]
    if peers.get("rank_by_revenue") and peers.get("peer_count"):
        parts.append(f"营收排名: {peers['rank_by_revenue']}/{peers['peer_count']}")
    if peers.get("rank_by_profit") and peers.get("peer_count"):
        parts.append(f"利润排名: {peers['rank_by_profit']}/{peers['peer_count']}")
    if peers.get("rank_by_market_cap") and peers.get("peer_count"):
        parts.append(f"市值排名: {peers['rank_by_market_cap']}/{peers['peer_count']}")
    if peers.get("industry_med_pe") is not None:
        parts.append(f"行业中位数PE: {peers['industry_med_pe']:.1f}")
    if peers.get("industry_med_pb") is not None:
        parts.append(f"行业中位数PB: {peers['industry_med_pb']:.2f}")
    if peers.get("industry_med_roe") is not None:
        parts.append(f"行业中位数ROE: {peers['industry_med_roe']:.1f}%")
    return "；".join(parts)


def _format_snapshot(snap: dict[str, Any] | None) -> str | None:
    """格式化 daily_snapshot 为可读文本。"""
    if not snap:
        return None
    parts = []
    pe = snap.get("pe_ttm")
    pb = snap.get("pb")
    mv = snap.get("total_mv")
    if pe is not None:
        parts.append(f"PE(TTM): {pe:.1f}")
    if pb is not None:
        parts.append(f"PB: {pb:.2f}")
    if mv is not None:
        parts.append(f"总市值: {mv/1e8:.0f}亿")
    return "；".join(parts) if parts else None


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
    enrich = _enrich_data(code)   # P0+P1+P2 深度数据
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
                "产品深度": _format_product_data(enrich.get("company_products")),
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
                "筹码分布": ctx.get("chip") or {},
                "数据来源": "daily_prices + intraday + features + daily_chip",
            },
            "5_财务情况": {
                "近三年记录": fin_rows[:12],
                "财务得分": fin_sc,
                "评语": fin_note,
                "估值截面": snap_row,
                "数据来源": "financial_indicators + daily_snapshot",
            },
            "6_投资持仓": {
                "状态": _format_holdings(enrich.get("institutional_holdings"))
                       or (_format_snapshot(snap_row) if snap_row else None)
                       or "待 Phase B 股东持仓 ETL",
                "机构研报": _format_research_reports(enrich.get("research_reports")),
            },
            "7_公司风险": {
                "技术风险": tech.signals,
                "舆情风险": linked.get("event_types") or {},
                "新闻条数_48h": news_total,
            },
            "8_核心竞争力": {
                "档案摘要": (profile or {}).get("positioning"),
                "舆情": linked,
                "行业对标": _format_industry_peers(enrich.get("industry_peers")),
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
    """规则引擎 Markdown 研报（8 模块完整结构）。"""
    meta = report["meta"]
    title = meta.get("name") or meta["ts_code"]
    modules = report["modules"]
    advice = report["投资建议"]

    lines = [
        f"# {title}（{meta['ts_code']}）投资研究报告",
        "",
        f"> 数据截止：{report.get('as_of', '—')} | 规则：{report.get('rule_version', '—')} | "
        f"综合推荐分：**{advice['综合推荐分']}** | 操作建议：**{advice.get('操作建议', '—')}**",
        "",
        "---",
        "",
    ]

    # ═══ 1. 公司基本信息 ═══
    m1 = modules.get("1_基本信息", {})
    lines.extend([
        "## 1. 公司基本信息",
        "",
        f"- **行业**：{m1.get('行业', '—')}",
        f"- **公司定位**：{m1.get('公司定位', '—')}",
        f"- **上市日期**：{m1.get('上市日期', '—')}",
        f"- **官网**：{m1.get('官网', '—')}",
        f"- **数据来源**：{m1.get('数据来源', '—')}",
        "",
    ])

    # ═══ 2. 核心产品 ═══
    m2 = modules.get("2_核心产品", {})
    products = m2.get("核心产品", "")
    lines.append("## 2. 核心产品")
    lines.append("")
    if products and products != "待扩展":
        lines.append(f"- {products}")
    else:
        lines.append("> ⚠️ 产品数据待充实（需 enrich_company P2 深度调研或资讯 Agent 扩展公司档案）")
    if m2.get("news_mentions_48h"):
        lines.append(f"- 48h 新闻提及：{m2['news_mentions_48h']} 条")
    lines.append("")

    # ═══ 3. 创始团队 ═══
    m3 = modules.get("3_创始团队", {})
    team = m3.get("团队", "")
    lines.append("## 3. 创始团队与核心管理层")
    lines.append("")
    if team and team != "待扩展":
        try:
            import json as _json
            team_dict = _json.loads(team) if isinstance(team, str) else team
            if isinstance(team_dict, dict) and team_dict:
                for k, v in team_dict.items():
                    lines.append(f"- **{k}**：{v}")
            else:
                lines.append(f"- {team}")
        except Exception:
            lines.append(f"- {team}")
    else:
        lines.append("> ⚠️ 管理层数据待充实（需 enrich_company P0 公司档案扩展）")
    lines.append("")

    # ═══ 4. 股价分析（核心）═══
    m4 = modules.get("4_股价分析", {})
    lines.extend([
        "## 4. 股价分析（核心）",
        "",
        "### 4.1 趋势",
        m4.get("趋势叙述", "—"),
        "",
        f"**技术面结论**：{m4.get('技术面结论', '—')}（得分 {m4.get('技术得分', '—')}）",
    ])
    ind_rel = m4.get("行业相对")
    if ind_rel:
        lines.append(f"- 行业相对强度：{ind_rel}")
    lines.extend(["", "### 4.2 关键信号", ""])
    for sig in m4.get("关键信号") or ["（无显著信号）"]:
        lines.append(f"- {sig}")

    # 筹码分布
    chip = m4.get("筹码分布") or {}
    if chip.get("available"):
        lines.extend([
            "",
            "### 4.3 筹码分布",
            f"- 获利比例：{chip['profit_ratio']:.1f}%　|　平均成本：{chip['avg_cost']:.2f}",
            f"- 90%筹码区间：[{chip['cost_90_low']:.2f}, {chip['cost_90_high']:.2f}]",
            f"- 90%集中度：{chip['concentration_90']:.4f}　|　70%集中度：{chip['concentration_70']:.4f}",
            f"- 数据截止：{chip.get('as_of', '—')}",
        ])

    # 分时特征
    intraday = m4.get("分时特征")
    if intraday:
        lines.extend(["", "### 4.4 分时特征", f"- {intraday}"])

    # 指标快照
    snap = m4.get("指标快照")
    if snap:
        lines.extend(["", "### 4.5 指标快照", "", "```"])
        for k, v in snap.items():
            if v is not None:
                lines.append(f"{k}: {v}")
        lines.append("```")
    lines.append("")

    # ═══ 5. 财务情况 ═══
    m5 = modules.get("5_财务情况", {})
    lines.extend([
        "## 5. 财务情况",
        "",
        m5.get("评语", "—"),
        "",
        f"**财务得分**：{m5.get('财务得分', '—')}",
        "",
    ])
    # 近三年财务记录
    fin_rows = m5.get("近三年记录") or []
    if fin_rows:
        lines.extend([
            "**近期财务数据**：",
            "",
            "| 报告期 | PE(TTM) | PB | 营收(亿) | 净利润(亿) | ROE(%) |",
            "|--------|---------|-----|---------|-----------|--------|",
        ])
        for r in fin_rows[:8]:
            rev = f"{r['revenue']/1e8:.1f}" if r.get("revenue") else "—"
            np_ = f"{r['net_profit']/1e8:.2f}" if r.get("net_profit") else "—"
            pe = f"{r['pe_ttm']:.1f}" if r.get("pe_ttm") else "—"
            pb = f"{r['pb']:.2f}" if r.get("pb") else "—"
            roe = f"{r['roe']:.1f}" if r.get("roe") else "—"
            lines.append(f"| {r.get('report_date', '—')} | {pe} | {pb} | {rev} | {np_} | {roe} |")
        lines.append("")

    # 估值截面
    snap_row = m5.get("估值截面")
    if snap_row and isinstance(snap_row, dict):
        pe = snap_row.get("pe_ttm")
        pb = snap_row.get("pb")
        mv = snap_row.get("total_mv")
        if pe or pb or mv:
            lines.append("**估值截面**：")
            if pe:
                lines.append(f"- PE(TTM)：{pe:.2f}")
            if pb:
                lines.append(f"- PB：{pb:.2f}")
            if mv:
                lines.append(f"- 总市值：{mv/1e8:.1f} 亿")
            lines.append("")

    # ═══ 6. 投资持仓 ═══
    m6 = modules.get("6_投资持仓", {})
    lines.append("## 6. 获得投资情况")
    lines.append("")
    holdings_text = m6.get("状态", "")
    reports_text = m6.get("机构研报", "")
    if holdings_text and not str(holdings_text).startswith("{") and "待" not in str(holdings_text):
        lines.append(f"- {holdings_text}")
    if reports_text and str(reports_text).strip():
        lines.append("")
        lines.append("**近期机构研报**：")
        lines.append("")
        for line in str(reports_text).split("\n"):
            if line.strip():
                lines.append(f"- {line.strip()}")
    if (not holdings_text or str(holdings_text).startswith("{") or "待" in str(holdings_text)) and not reports_text:
        lines.append("> ⚠️ 机构持仓数据待充实（需 enrich_company P1 股东+北向+基金 ETL）")
    lines.append("")

    # ═══ 7. 公司风险 ═══
    m7 = modules.get("7_公司风险", {})
    lines.extend([
        "## 7. 公司风险",
        "",
        f"- 48h 新闻：{m7.get('新闻条数_48h', 0)} 条",
    ])
    # 舆情风险
    event_types = m7.get("舆情风险") or {}
    if event_types:
        lines.append("- 事件类型分布：")
        for evt, cnt in event_types.items():
            lines.append(f"  - {evt}：{cnt} 条")
    # 技术风险
    tech_risks = m7.get("技术风险") or []
    if tech_risks:
        lines.append(f"- 技术面风险：{tech_risks}")
    lines.append("")

    # ═══ 8. 核心竞争力 ═══
    m8 = modules.get("8_核心竞争力", {})
    lines.append("## 8. 核心竞争力")
    lines.append("")
    positioning = m8.get("档案摘要")
    if positioning and positioning != "待资讯域补充":
        lines.append(f"- 公司定位：{positioning}")
    news_linked = m8.get("舆情") or {}
    if news_linked:
        news_summary = news_linked.get("summary") or news_linked.get("event_types")
        if news_summary:
            lines.append(f"- 近期舆情：{news_summary}")
    lines.append("")

    # ═══ 投资建议 ═══
    lines.extend([
        "---",
        "",
        "## 投资建议",
        "",
        advice["总结"],
        "",
        f"| 项目 | 价格 |",
        f"|------|------|",
        f"| 操作建议 | **{advice['操作建议']}** |",
        f"| 建议买入价 | {advice.get('建议买入价', '—')} |",
        f"| 目标价 | {advice.get('目标价', '—')} |",
        f"| 止损参考 | {advice.get('止损参考', '—')} |",
        f"| 综合推荐分 | {advice.get('综合推荐分', '—')} / 100 |",
        "",
        "### 不同走势应对",
        "",
    ])
    for s in advice.get("走势应对") or []:
        lines.append(f"- {s}")

    # 附录：数据缺口
    gaps = report.get("data_gaps_for_other_agents") or []
    if gaps:
        lines.extend(["", "## 附录：数据缺口"])
        for g in gaps:
            lines.append(f"- {g}")

    return "\n".join(lines)
