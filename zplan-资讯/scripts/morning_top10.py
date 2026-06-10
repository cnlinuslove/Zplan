#!/usr/bin/env python3
"""早间 TOP10 推荐播报：从最新选股运行中取前 10 只，推送到企微群。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/morning_top10.py
    cd zplan-资讯 && .venv/bin/python scripts/morning_top10.py --dry-run   # 仅打印不推送

调度: launchd 交易日 9:00 触发（盘前 30 分钟）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# 确保 zplan-资讯 在 sys.path
NEWS_ROOT = Path(os.environ.get("ZPLAN_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(NEWS_ROOT))

from zplan_shared.models import (
    DailyPrice,
    PickEntry,
    PickRun,
    SessionLocal,
    StockList,
    StockRuleScore,
    init_db,
)
from sqlalchemy import desc, select, text
from wechat_push import push_wechat_markdown

BEIJING_TZ = timezone(timedelta(hours=8))


def _top10_from_pick_run(session) -> tuple[list[dict[str, Any]], PickRun | None]:
    """从最新 PickRun 取 TOP10 entries。"""
    run = session.execute(
        select(PickRun)
        .where(PickRun.run_kind == "llm_top300")
        .order_by(desc(PickRun.created_at_utc))
        .limit(1)
    ).scalars().first()

    if not run:
        return [], None

    entries = session.execute(
        select(PickEntry)
        .where(PickEntry.run_id == run.id)
        .order_by(
            PickEntry.rank_in_run,
            PickEntry.final_composite_score.desc().nullslast(),
        )
        .limit(10)
    ).scalars().all()

    return list(entries), run


def _top10_from_rule_scores(session) -> list[dict[str, Any]]:
    """Fallback：从 stock_rule_scores 最新交易日取 TOP10。"""
    latest_date = session.execute(
        text("SELECT MAX(trade_date_as_of) FROM stock_rule_scores")
    ).scalar()

    if not latest_date:
        return []

    rows = session.execute(
        text(
            "SELECT ts_code, name, composite_score, close_price, verdict, signals_json "
            "FROM stock_rule_scores "
            "WHERE trade_date_as_of = :d AND market = 'a' "
            "ORDER BY composite_score DESC "
            "LIMIT 10"
        ),
        {"d": latest_date},
    ).fetchall()

    import json as _json

    result = []
    for r in rows:
        signals = []
        try:
            signals = _json.loads(r[5]) if r[5] else []
        except Exception:
            pass
        result.append({
            "ts_code": r[0],
            "name": r[1],
            "final_composite_score": r[2],
            "rule_composite_score": r[2],
            "close_price": r[3],
            "verdict": r[4],
            "signals": signals,
            "llm_composite_score": None,
            "recommendation": None,
        })
    return result


def _enrich_entries(session, entries: list[Any]) -> list[dict[str, Any]]:
    """批量补全行业、概念、PE、市值。"""
    if not entries:
        return []

    codes = []
    enriched = []
    for e in entries:
        # 兼容 PickEntry ORM 对象和 dict
        if isinstance(e, dict):
            code = e["ts_code"]
            name = e.get("name", code)
            score = e.get("final_composite_score") or e.get("rule_composite_score")
            llm_score = e.get("llm_composite_score")
            close = e.get("close_price")
            verdict = e.get("verdict", "")
            signals = e.get("signals", [])
        else:
            code = e.ts_code
            name = e.name or code
            score = e.final_composite_score or e.rule_composite_score
            llm_score = e.llm_composite_score
            close = e.close_price
            verdict = e.verdict or ""
            signals = []

        codes.append(code)
        enriched.append({
            "ts_code": code,
            "name": name,
            "score": score,
            "llm_score": llm_score,
            "close": close,
            "verdict": verdict,
            "signals": signals,
        })

    if not codes:
        return enriched

    # 行业
    industry_map: dict[str, str] = {}
    rows = session.execute(
        select(StockList.ts_code, StockList.industry)
        .where(StockList.ts_code.in_(codes))
    ).all()
    industry_map = {r.ts_code: (r.industry or "") for r in rows}

    # PE / 市值（最新交易日）
    pe_map: dict[str, float] = {}
    mv_map: dict[str, float] = {}
    latest_date = session.execute(
        text("SELECT MAX(trade_date) FROM daily_snapshot")
    ).scalar()
    if latest_date and codes:
        placeholders = ",".join(f":c{i}" for i in range(len(codes)))
        params = {f"c{i}": c for i, c in enumerate(codes)}
        params["d"] = str(latest_date)
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

    # 概念标签（每只取前 3 个）
    from zplan_shared.models import StockConceptMember
    concept_map: dict[str, list[str]] = {}
    rows = session.execute(
        select(StockConceptMember.ts_code, StockConceptMember.concept_name)
        .where(StockConceptMember.ts_code.in_(codes))
    ).all()
    _skip_concepts = {
        "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
        "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
        "央国企改革", "黑龙江", "深圳特区", "机械设备", "通信", "电子", "计算机",
        "公用事业", "电力", "基础化工", "化学制品", "元件", "通信技术", "通信设备",
    }
    for r in rows:
        if r[1] not in _skip_concepts:
            concept_map.setdefault(r[0], []).append(r[1])

    for item in enriched:
        code = item["ts_code"]
        item["industry"] = industry_map.get(code, "")
        item["pe_ttm"] = pe_map.get(code)
        item["total_mv"] = mv_map.get(code)
        concepts = concept_map.get(code, [])[:3]
        item["concepts"] = concepts

    return enriched


def _format_morning_markdown(
    entries: list[dict[str, Any]],
    as_of_label: str,
    source: str,
) -> str:
    """格式化早间播报 markdown 消息。"""
    beijing_now = datetime.now(BEIJING_TZ)
    date_str = beijing_now.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][beijing_now.weekday()]

    lines = [
        f"## 📈 Z-Plan 今日关注 TOP{len(entries)}",
        f"> {date_str} {weekday} 盘前 · 数据截止 {as_of_label}",
        f"> 来源: {source}",
        "",
    ]

    if not entries:
        lines.append("⚠️ 暂无选股数据，请先运行选股扫描。")
        lines.append("> `cd zplan-选股 && .venv/bin/python main.py llm-top --top 300`")
        return "\n".join(lines)

    for i, item in enumerate(entries):
        code = item["ts_code"]
        name = item["name"] or code
        score = item["score"]
        score_str = f"{score:.0f}" if score is not None else "--"
        llm = item["llm_score"]
        close = item["close"]
        price_str = f"¥{close:.2f}" if close is not None else ""
        verdict = item.get("verdict", "")
        industry = item.get("industry", "")

        # 第一行：排名 · 名称代码 · 评分 · 价格
        meta_parts = []
        if score_str != "--":
            meta_parts.append(f"**{score_str}分**")
        if verdict:
            meta_parts.append(str(verdict))
        if price_str:
            meta_parts.append(price_str)
        if llm is not None:
            meta_parts.append(f"LLM {llm:.0f}")

        lines.append(
            f"### {i+1}. {name}({code})  {' · '.join(meta_parts)}"
        )

        # 第二行：行业 / 概念 / PE / 市值
        detail_parts = []
        if industry:
            detail_parts.append(f"📌 {industry}")
        concepts = item.get("concepts", [])
        if concepts:
            detail_parts.append(f"🏷 {' · '.join(concepts[:3])}")
        pe = item.get("pe_ttm")
        if pe is not None and pe > 0:
            detail_parts.append(f"PE {pe:.1f}")
        mv = item.get("total_mv")
        if mv is not None and mv > 0:
            if mv >= 1e8:
                detail_parts.append(f"市值 {mv/1e8:.0f}亿")
            elif mv >= 1e4:
                detail_parts.append(f"市值 {mv/1e4:.0f}万")
        if detail_parts:
            lines.append(f"> {'  |  '.join(detail_parts)}")

        # 第三行：信号（若有）
        signals = item.get("signals", [])
        if signals:
            sig_text = " · ".join(str(s) for s in signals[:2])
            lines.append(f"> 信号: {sig_text}")

        lines.append("")

    lines.append("---")
    lines.append("💡 发送「**分析 股票名**」可查看个股深度研报")
    lines.append(f"> 📄 指令: 选股 名称 | 筛选 题材 | 帮助")

    return "\n".join(lines)


def _should_skip_today() -> bool:
    """周末/节假日跳过播报。简单判断：周六周日不播。"""
    beijing_now = datetime.now(BEIJING_TZ)
    # 周六(5) / 周日(6) 跳过
    if beijing_now.weekday() >= 5:
        return True
    # TODO: 可接入交易日历做精确判断
    return False


def main():
    dry_run = "--dry-run" in sys.argv

    if _should_skip_today():
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] 周末/假日，跳过播报")
        return

    init_db()

    with SessionLocal() as session:
        entries, run = _top10_from_pick_run(session)

        if entries:
            as_of = (
                run.trade_date_as_of.strftime("%Y-%m-%d")
                if run and run.trade_date_as_of
                else (run.created_at_utc.strftime("%Y-%m-%d %H:%M") if run else "未知")
            )
            source = f"选股引擎 (run_id={run.id})" if run else "选股引擎"
            enriched = _enrich_entries(session, entries)
        else:
            # Fallback: 规则打分 TOP10
            entries = _top10_from_rule_scores(session)
            if entries:
                # 获取规则打分最新日期
                latest = session.execute(
                    text("SELECT MAX(trade_date_as_of) FROM stock_rule_scores")
                ).scalar()
                as_of = str(latest) if latest else "最新"
                source = "规则引擎 TOP10（无 LLM 简评）"
            else:
                as_of = "无"
                source = "无可用数据"
            enriched = _enrich_entries(session, entries)

    markdown = _format_morning_markdown(enriched, as_of, source)

    if dry_run:
        print("=" * 50)
        print("[DRY RUN] 以下为企微推送内容预览:")
        print("=" * 50)
        print(markdown)
        print("=" * 50)
        print(f"共 {len(enriched)} 只标的")
        return

    ok = push_wechat_markdown(markdown)
    if ok:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ✅ 早间 TOP10 播报成功 ({len(enriched)} 只)")
    else:
        print(f"[{datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M}] ❌ 播报失败（检查 WECHAT_PUSH_WEBHOOK 配置）")
        sys.exit(1)


if __name__ == "__main__":
    main()
