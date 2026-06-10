"""根因分析引擎：逐层归因「AI 推荐但下跌」的原因。

五层归因模型：
  1. 市场拖累    — 大盘是否普跌？
  2. 板块抽血    — 所属行业/板块是否跑输？
  3. AI 因子失效  — 选股时的信号是否失效？
  4. 技术面反转  — MA/MACD/KDJ 是否转向？
  5. 个股事件    — 是否有突发利空？

用法::

    from backtest_agent.root_cause import RootCauseEngine

    engine = RootCauseEngine()
    # 分析单笔亏损交易
    causes = engine.analyze_trade(ts_code="601939", entry_date=..., exit_date=...)

    # 批量分析模拟结果
    report = engine.analyze_sim_result(sim_output)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text

from zplan_shared.market import get_bars, get_stock_concepts, resolve_ts_code
from zplan_shared.models import (
    DailyPrice,
    NewsStockLink,
    PickLlmEvaluation,
    SessionLocal,
    StockList,
    init_db,
)

# ── 指数映射 ──
_INDEX_MAP = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000688": "科创50",
    "000300": "沪深300",
}
_INDEX_ORDER = ["000001", "399001", "399006", "000688", "000300"]


class RootCauseEngine:
    """多层归因分类器。"""

    def __init__(self):
        self._index_cache: dict[str, pd.DataFrame] = {}
        self._industry_cache: dict[date, pd.DataFrame] = {}

    # ── 主入口 ──

    def analyze_trade(
        self,
        *,
        ts_code: str,
        name: str = "",
        entry_date: date,
        exit_date: date,
        entry_price: float,
        exit_price: float,
        entry_id: int | None = None,
    ) -> dict[str, Any]:
        """对单笔交易做完整归因分析。"""
        ret_pct = round((exit_price - entry_price) / entry_price * 100, 4)
        code = resolve_ts_code(ts_code)

        causes: list[dict[str, Any]] = []
        contributions: dict[str, float] = {}

        # Layer 1: 市场拖累
        market = self._analyze_market(code, entry_date, exit_date)
        if market:
            causes.append(market)
            contributions["market"] = market.get("contribution_pct", 0)

        # Layer 2: 板块抽血
        sector = self._analyze_sector(code, entry_date, exit_date)
        if sector:
            causes.append(sector)
            contributions["sector"] = sector.get("contribution_pct", 0)

        # Layer 3: AI 因子失效
        ai_factors = self._analyze_ai_factors(entry_id) if entry_id else None
        if ai_factors:
            causes.append(ai_factors)

        # Layer 4: 技术面反转
        technical = self._analyze_technical_reversal(code, entry_date, exit_date)
        if technical:
            causes.append(technical)

        # Layer 5: 个股事件
        events = self._analyze_news_events(code, entry_date, exit_date)
        if events:
            causes.append(events)

        # 归因结论
        primary = self._primary_cause(causes, ret_pct)

        return {
            "ts_code": ts_code,
            "name": name,
            "entry_date": str(entry_date),
            "exit_date": str(exit_date),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "return_pct": ret_pct,
            "is_loss": ret_pct < 0,
            "causes": causes,
            "contributions": contributions,
            "primary_cause": primary,
            "improvement_hint": self._improvement_hint(primary, causes),
        }

    def analyze_sim_result(
        self,
        sim_output: dict[str, Any],
    ) -> dict[str, Any]:
        """批量分析模拟交易结果。"""
        trades = sim_output.get("trades") or []
        if not trades:
            return {"ok": False, "message": "无交易记录"}

        closed = [
            t for t in trades
            if t.get("status") == "closed" and t.get("return_pct") is not None
        ]
        if not closed:
            return {"ok": False, "message": "无已平仓交易"}

        analyzed: list[dict[str, Any]] = []
        loss_analyzed: list[dict[str, Any]] = []

        for t in closed:
            entry_d = _parse_date(t.get("entry_date"))
            exit_d = _parse_date(t.get("exit_date"))
            if not entry_d or not exit_d:
                continue

            result = self.analyze_trade(
                ts_code=t.get("ts_code", ""),
                name=t.get("name", ""),
                entry_date=entry_d,
                exit_date=exit_d,
                entry_price=t.get("entry_price") or 0,
                exit_price=t.get("exit_price") or 0,
                entry_id=t.get("entry_id"),
            )
            analyzed.append(result)
            if result["is_loss"]:
                loss_analyzed.append(result)

        # 聚合成因统计
        cause_counts: dict[str, int] = {}
        for a in loss_analyzed:
            label = a.get("primary_cause", {}).get("layer", "unknown")
            cause_counts[label] = cause_counts.get(label, 0) + 1

        # 改进建议汇总
        hints: dict[str, list[str]] = {"prompt": [], "strategy": [], "rule_engine": []}
        for a in loss_analyzed:
            hint = a.get("improvement_hint") or {}
            for layer, items in hint.items():
                if isinstance(items, list):
                    hints.setdefault(layer, []).extend(items)

        return {
            "ok": True,
            "total_trades": len(analyzed),
            "loss_trades": len(loss_analyzed),
            "loss_rate": round(len(loss_analyzed) / len(analyzed), 4) if analyzed else 0,
            "cause_counts": cause_counts,
            "trades": analyzed,
            "losses": loss_analyzed,
            "improvement_hints": {k: list(set(v)) for k, v in hints.items() if v},
        }

    # ── Layer 1: 市场拖累 ──

    def _analyze_market(
        self, ts_code: str, entry_date: date, exit_date: date,
    ) -> dict[str, Any] | None:
        """判断多大程度归因于大盘下跌。"""
        # 取最相关的指数
        index_code = self._best_index_for(ts_code)
        index_bars = self._load_index(index_code)

        stock_bars = get_bars(ts_code)
        stock_ret = self._period_return(stock_bars, entry_date, exit_date)
        index_ret = self._period_return(index_bars, entry_date, exit_date)

        if stock_ret is None or index_ret is None:
            return None

        # 贡献估算：若大盘跌 2%，个股跌 5%，则市场贡献约 -2%（Beta≈1 假设）
        if index_ret < -0.005 and stock_ret < 0:
            contribution = max(index_ret, stock_ret)  # 不超过个股跌幅
            severity = "轻微" if index_ret > -1.0 else ("中等" if index_ret > -2.0 else "严重")
            return {
                "layer": "market",
                "label": "📉 市场拖累",
                "detail": f"{_INDEX_MAP.get(index_code, index_code)} 同期 {index_ret:+.2f}%，"
                          f"个股 {stock_ret:+.2f}%",
                "index_code": index_code,
                "index_name": _INDEX_MAP.get(index_code, index_code),
                "index_return_pct": round(index_ret, 2),
                "stock_return_pct": round(stock_ret, 2),
                "contribution_pct": round(contribution, 2),
                "severity": severity,
            }
        return None

    # ── Layer 2: 板块抽血 ──

    def _analyze_sector(
        self, ts_code: str, entry_date: date, exit_date: date,
    ) -> dict[str, Any] | None:
        """判断是否被板块/行业拖累。"""
        industry = self._get_industry(ts_code)
        if not industry:
            return None

        stock_bars = get_bars(ts_code)
        stock_ret = self._period_return(stock_bars, entry_date, exit_date)
        if stock_ret is None:
            return None

        industry_ret = self._industry_average_return(industry, entry_date, exit_date)
        if industry_ret is None:
            return None

        gap = stock_ret - industry_ret
        # 板块整体下跌
        if industry_ret < -0.5 and stock_ret < 0:
            contribution = industry_ret
            return {
                "layer": "sector",
                "label": "🏭 板块拖累",
                "detail": f"{industry} 行业同期均跌 {industry_ret:+.2f}%，"
                          f"个股 {stock_ret:+.2f}%（差于行业 {gap:+.2f}%）",
                "industry": industry,
                "industry_return_pct": round(industry_ret, 2),
                "stock_return_pct": round(stock_ret, 2),
                "gap_pct": round(gap, 2),
                "contribution_pct": round(contribution, 2),
            }
        # 板块涨但个股跌 → 资金轮出
        if industry_ret > 0.5 and stock_ret < -1:
            return {
                "layer": "sector",
                "label": "🔄 资金轮出",
                "detail": f"{industry} 行业同期涨 {industry_ret:+.2f}%，"
                          f"但个股独跌 {stock_ret:+.2f}%（资金轮出此板块/个股）",
                "industry": industry,
                "industry_return_pct": round(industry_ret, 2),
                "stock_return_pct": round(stock_ret, 2),
                "gap_pct": round(gap, 2),
                "contribution_pct": 0,
            }

        return None

    # ── Layer 3: AI 因子失效 ──

    def _analyze_ai_factors(self, entry_id: int) -> dict[str, Any] | None:
        """检查 LLM 诊断标签。"""
        init_db()
        with SessionLocal() as session:
            ev = session.execute(
                text("SELECT failure_tags_json, verdict, llm_score, rule_score "
                     "FROM pick_llm_evaluations WHERE entry_id = :eid"),
                {"eid": entry_id},
            ).fetchone()

        if not ev:
            return None

        tags = json.loads(ev[0] or "[]")
        if not tags:
            return {"layer": "ai_factors", "label": "✅ AI 信号正常", "detail": "无失败标签", "tags": []}

        # 翻译标签
        label_map = {
            "momentum_chase": "追高风险（20日涨幅过高）",
            "buy_unreachable": "定价失误（建议买价无法成交）",
            "score_inflation": "LLM 抬分（评分虚高理由空洞）",
            "generic_bullish": "套话推荐（未引用具体信号）",
            "near_60d_high": "接近60日高点",
            "forward_loss": "验证期实际亏损",
            "forward_flat": "验证期收益平盘",
            "over_recommendation": "推荐档位偏积极",
        }

        detail_parts = []
        for t in tags:
            detail_parts.append(f"`{t}`={label_map.get(t, t)}")

        return {
            "layer": "ai_factors",
            "label": "🤖 AI 因子失效",
            "detail": "；".join(detail_parts),
            "tags": tags,
            "tag_count": len(tags),
            "verdict": ev[1],
            "llm_score": ev[2],
            "rule_score": ev[3],
        }

    # ── Layer 4: 技术面反转 ──

    def _analyze_technical_reversal(
        self, ts_code: str, entry_date: date, exit_date: date,
    ) -> dict[str, Any] | None:
        """检查买入后技术信号是否转向。"""
        bars = get_bars(ts_code)
        if bars.empty:
            return None

        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        bars = bars.copy()
        bars.index = idx

        # 入场前和出场时的数据
        entry_bars = bars[bars.index <= pd.Timestamp(entry_date)]
        exit_bars = bars[(bars.index >= pd.Timestamp(entry_date)) & (bars.index <= pd.Timestamp(exit_date))]

        if entry_bars.empty or exit_bars.empty:
            return None

        reversals: list[str] = []

        # MA5 vs MA20
        ma5_entry = entry_bars["close"].tail(5).mean()
        ma20_entry = entry_bars["close"].tail(20).mean()
        ma5_exit = float(exit_bars["close"].iloc[-1])
        # 简化：用 exit close vs entry MA20
        if ma20_entry > 0 and ma5_exit < ma20_entry:
            reversals.append("跌破 MA20")

        # MACD 方向
        if len(exit_bars) >= 3 and len(entry_bars) >= 26:
            entry_macd = self._quick_macd(entry_bars)
            exit_macd = self._quick_macd(exit_bars)
            if entry_macd is not None and exit_macd is not None:
                if entry_macd > 0 and exit_macd < 0:
                    reversals.append("MACD 转负")
                elif exit_macd < entry_macd * 0.5:
                    reversals.append("MACD 柱大幅缩减")

        # 放量下跌
        avg_vol = float(entry_bars["volume"].tail(20).mean()) if "volume" in entry_bars.columns else 0
        exit_vol = float(exit_bars["volume"].tail(3).mean()) if "volume" in exit_bars.columns else 0
        if avg_vol > 0 and exit_vol > avg_vol * 1.5:
            exit_pct = float(exit_bars["close"].iloc[-1]) / float(entry_bars["close"].iloc[-1]) - 1
            if exit_pct < -0.02:
                reversals.append("放量下跌")

        if not reversals:
            return None

        return {
            "layer": "technical",
            "label": "🔄 技术面反转",
            "detail": "；".join(reversals),
            "reversals": reversals,
        }

    # ── Layer 5: 个股事件 ──

    def _analyze_news_events(
        self, ts_code: str, entry_date: date, exit_date: date,
    ) -> dict[str, Any] | None:
        """检查是否有突发利空新闻。"""
        init_db()
        code = resolve_ts_code(ts_code)
        with SessionLocal() as session:
            rows = session.execute(
                text(
                    "SELECT event_type, published_at_utc FROM news_stock_link "
                    "WHERE ts_code = :c AND published_at_utc BETWEEN :s AND :e "
                    "LIMIT 5"
                ),
                {"c": code, "s": str(entry_date), "e": str(exit_date + timedelta(days=1))},
            ).fetchall()

        if not rows:
            return None

        events = [r[0] for r in rows if r[0]]
        return {
            "layer": "news",
            "label": "📰 个股事件",
            "detail": f"期间 {len(rows)} 条关联事件（{', '.join(events[:3]) if events else '无分类'}）",
            "event_count": len(rows),
            "events": events[:3],
        }

    # ── 辅助方法 ──

    def _primary_cause(
        self, causes: list[dict[str, Any]], ret_pct: float,
    ) -> dict[str, Any]:
        """判定主因。优先级：AI因子 > 技术反转 > 板块 > 市场 > 未知。"""
        if not causes:
            return {"layer": "unknown", "label": "无法判定", "detail": "无足够数据"}

        priority = ["ai_factors", "technical", "sector", "market", "news"]
        for p in priority:
            for c in causes:
                if c.get("layer") == p:
                    return c

        return causes[0] if causes else {"layer": "unknown", "label": "无法判定"}

    def _improvement_hint(
        self, primary: dict[str, Any] | None, causes: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        """根据归因生成改进建议。"""
        hints: dict[str, list[str]] = {"prompt": [], "strategy": [], "rule_engine": []}
        if not primary:
            return hints

        layer = primary.get("layer", "")

        if layer == "ai_factors":
            tags = primary.get("tags", [])
            if "buy_unreachable" in tags:
                hints["strategy"].append(
                    "suggested_buy MA20折扣 0.99→0.97，或增加「可成交价≈close*0.995」"
                )
                hints["prompt"].append(
                    "LLM buy_price 不得高于 close*0.99，须说明与 suggested_buy 关系"
                )
            if "momentum_chase" in tags:
                hints["strategy"].append("filters.max_ret_20d 收紧到 3.0")
                hints["prompt"].append("ret_20d>5% 时必须提示追高风险，recommendation 最高「观望」")
            if "score_inflation" in tags:
                hints["prompt"].append("LLM composite_score 默认=规则分，有具体信号才加分（上限+5）")
            if "generic_bullish" in tags:
                hints["prompt"].append("trend_one_liner 必须引用具体指标数值，禁止套话")
            if "near_60d_high" in tags:
                hints["strategy"].append("filters.max_high_60d_pct 收紧到 0.90")
                hints["rule_engine"].append("high_60d_pct>0.92 时 tech_score -= 8")

        elif layer == "technical":
            hints["strategy"].append("考虑增加「技术面确认」过滤：MA5>MA20 且 MACD>0 才可推荐")
            hints["rule_engine"].append("扫描阶段增加 MACD 方向检查")

        elif layer == "sector":
            hints["strategy"].append(
                f"板块轮动信号：{primary.get('industry','')} 行业表现弱于大盘时降低该行业权重"
            )
            hints["rule_engine"].append("增加行业强度因子（industry_relative 权重大于 0.10）")

        elif layer == "market":
            hints["strategy"].append("市场普跌日减少或不执行新买入（market_timing 过滤）")
            hints["rule_engine"].append("增加 market_health 大盘强度门禁：全A中位数<0 时跳过选股")

        return hints

    def _best_index_for(self, ts_code: str) -> str:
        code = resolve_ts_code(ts_code)
        if code.startswith("60") or code.startswith("68"):
            return "000001"  # 上证
        if code.startswith("30"):
            return "399006"  # 创业板
        return "399001"  # 深证

    def _load_index(self, index_code: str) -> pd.DataFrame:
        """加载指数 K 线（带缓存）。"""
        if index_code in self._index_cache:
            return self._index_cache[index_code]
        bars = get_bars(index_code)
        if not bars.empty:
            self._index_cache[index_code] = bars
        return bars

    @staticmethod
    def _period_return(
        bars: pd.DataFrame, start: date, end: date,
    ) -> float | None:
        if bars.empty:
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(bars.index)).normalize()
        bars = bars.copy()
        bars.index = idx
        before = bars[bars.index <= pd.Timestamp(start)]
        after = bars[(bars.index >= pd.Timestamp(start)) & (bars.index <= pd.Timestamp(end))]
        if before.empty or after.empty:
            return None
        p0 = float(before["close"].iloc[-1])
        p1 = float(after["close"].iloc[-1])
        return round((p1 - p0) / p0 * 100, 4)

    def _get_industry(self, ts_code: str) -> str | None:
        init_db()
        with SessionLocal() as session:
            row = session.execute(
                text("SELECT industry FROM stock_list WHERE ts_code = :c"),
                {"c": resolve_ts_code(ts_code)},
            ).fetchone()
        return row[0] if row and row[0] else None

    def _industry_average_return(
        self, industry: str, start: date, end: date,
    ) -> float | None:
        """计算行业在区间内的平均涨跌幅。"""
        init_db()
        with SessionLocal() as session:
            # 取行业成分股在该区间的收益
            rows = session.execute(
                text(
                    "SELECT dp.ts_code, "
                    "MIN(CASE WHEN dp.trade_date <= :s THEN dp.close END) as p0, "
                    "MAX(CASE WHEN dp.trade_date >= :e THEN dp.close END) as p1 "
                    "FROM daily_prices dp "
                    "JOIN stock_list sl ON dp.ts_code = sl.ts_code "
                    "WHERE sl.industry = :ind AND dp.market = 'a' "
                    "AND dp.trade_date BETWEEN :s_start AND :e "
                    "GROUP BY dp.ts_code "
                    "HAVING p0 IS NOT NULL AND p1 IS NOT NULL AND p0 > 0"
                ),
                {
                    "ind": industry,
                    "s": str(start),
                    "e": str(end),
                    "s_start": str(start - timedelta(days=5)),
                },
            ).fetchall()

        if not rows:
            return None

        returns = []
        for r in rows:
            if r[1] and r[2] and float(r[1]) > 0:
                ret = (float(r[2]) - float(r[1])) / float(r[1]) * 100
                returns.append(ret)

        if len(returns) < 3:
            return None
        return round(sum(returns) / len(returns), 2)

    @staticmethod
    def _quick_macd(bars: pd.DataFrame) -> float | None:
        """快速 MACD 柱估计。"""
        if len(bars) < 26:
            return None
        close = bars["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        return float((dif - dea).iloc[-1])


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


# ── 格式化输出 ──


def format_root_cause_report(analysis: dict[str, Any]) -> str:
    """格式化根因分析为 Markdown。"""
    if not analysis.get("ok"):
        return f"# 根因分析\n\n❌ {analysis.get('message', '失败')}"

    lines = [
        "# 🔍 根因分析报告",
        "",
        f"- 总交易: **{analysis['total_trades']}** | 亏损: **{analysis['loss_trades']}** "
        f"| 亏损率: **{analysis['loss_rate']:.0%}**",
        "",
        "## 归因分布",
        "",
    ]

    counts = analysis.get("cause_counts") or {}
    if counts:
        lines.append("| 主因 | 次数 |")
        lines.append("|------|------|")
        for cause, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            label_map = {
                "market": "📉 市场拖累",
                "sector": "🏭 板块抽血",
                "ai_factors": "🤖 AI因子失效",
                "technical": "🔄 技术面反转",
                "news": "📰 个股事件",
                "unknown": "❓ 无法判定",
            }
            lines.append(f"| {label_map.get(cause, cause)} | **{cnt}** |")
        lines.append("")

    # 逐只亏损分析
    losses = analysis.get("losses") or []
    if losses:
        lines.append("## 亏损交易归因")
        lines.append("")
        for i, a in enumerate(losses):
            emoji = "🔴" if (a.get("return_pct") or 0) < -5 else "🟡"
            lines.append(
                f"### {i+1}. {emoji} {a.get('name')}({a.get('ts_code')}) "
                f"— {a.get('return_pct', 0):+.2f}%"
            )
            lines.append(f"> 入场 {a.get('entry_date')} ¥{a.get('entry_price')} "
                         f"→ 出场 {a.get('exit_date')} ¥{a.get('exit_price')}")
            lines.append("")

            for c in a.get("causes") or []:
                lines.append(f"- **{c.get('label')}**: {c.get('detail')}")

            primary = a.get("primary_cause") or {}
            if primary:
                lines.append(f"  - 🎯 主因: **{primary.get('label')}**")

            hint = a.get("improvement_hint") or {}
            if hint:
                for layer, items in hint.items():
                    for item in items:
                        icon = {"prompt": "📝", "strategy": "⚙️", "rule_engine": "🔧"}.get(layer, "💡")
                        lines.append(f"  - {icon} [{layer}] {item}")

            lines.append("")

    # 改进建议汇总
    hints = analysis.get("improvement_hints") or {}
    if hints:
        lines.append("## 改进建议汇总")
        lines.append("")
        for layer, items in hints.items():
            icon = {"prompt": "📝 Prompt", "strategy": "⚙️ Strategy", "rule_engine": "🔧 Rule"}.get(layer, f"💡 {layer}")
            lines.append(f"### {icon}")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

    return "\n".join(lines)
