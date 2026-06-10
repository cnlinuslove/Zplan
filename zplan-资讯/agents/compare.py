"""股票对比：并行分析两只股票，并排对比关键指标。"""
from __future__ import annotations

import concurrent.futures
import logging
import re
from typing import Any

from sqlalchemy import desc, select

from models import SessionLocal, init_db
from zplan_shared.models import (
    DailyPrice,
    PickEntry,
    PickRun,
    StockConceptMember,
    StockList,
)

logger = logging.getLogger(__name__)

# 对比命令解析
_COMPARE_RE = re.compile(
    r"^(对比|比较|vs\.?)\s*(?P<a>.+?)\s+(?:和|vs\.?|与|跟|、)?\s*(?P<b>.+)$",
    re.IGNORECASE,
)


def parse_compare_command(text: str) -> tuple[str, str] | None:
    """解析 "对比 A 和 B" → (A, B)。"""
    m = _COMPARE_RE.match(text.strip())
    if not m:
        return None
    a = m.group("a").strip().rstrip("和vsVS与跟、")
    b = m.group("b").strip()
    if a and b:
        return a, b
    return None


def _resolve_symbol(query: str) -> tuple[str, str]:
    """解析股票名 → (ts_code, name)。"""
    from agents.user_position import _resolve_symbol
    return _resolve_symbol(query)


def _get_latest_pick_entry(code: str) -> dict[str, Any] | None:
    """从 DB 获取该股票最新的选股记录。"""
    init_db()
    with SessionLocal() as session:
        entry = session.execute(
            select(PickEntry)
            .where(PickEntry.ts_code == code)
            .order_by(desc(PickEntry.id))
            .limit(1)
        ).scalars().first()
        if not entry:
            return None
        return {
            "ts_code": entry.ts_code,
            "name": entry.name,
            "final_composite_score": entry.final_composite_score,
            "rule_composite_score": entry.rule_composite_score,
            "llm_composite_score": entry.llm_composite_score,
            "verdict": entry.verdict,
            "recommendation": entry.recommendation,
            "buy_price": entry.predicted_buy_price,
            "target_price": entry.predicted_target_price,
            "stop_loss": entry.predicted_stop_loss,
            "close_price": entry.close_price,
        }


def _get_concepts(code: str, limit: int = 4) -> str:
    """获取概念标签。"""
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockConceptMember.concept_name)
            .where(StockConceptMember.ts_code == code)
            .limit(limit * 2)
        ).scalars().all()
    skip = {
        "小盘股", "小盘成长", "微盘股", "微利股", "昨日高振幅", "破增发价股",
        "2025年报预增", "2025年报扭亏", "QFII重仓", "转债标的", "贬值受益",
        "央国企改革", "黑龙江", "深圳特区",
    }
    concepts = [r for r in rows if r not in skip]
    return " · ".join(concepts[:limit])


def _get_industry(code: str) -> str:
    """获取行业分类。"""
    init_db()
    with SessionLocal() as session:
        row = session.execute(
            select(StockList.industry).where(StockList.ts_code == code)
        ).scalar_one_or_none()
    return str(row) if row else ""


def _get_recent_price(code: str) -> dict[str, Any]:
    """获取最新收盘价和涨跌幅。"""
    init_db()
    with SessionLocal() as session:
        row = session.execute(
            select(DailyPrice.close, DailyPrice.pct_chg)
            .where(DailyPrice.ts_code == code)
            .order_by(desc(DailyPrice.trade_date))
            .limit(1)
        ).first()
    if row:
        return {"close": float(row[0]) if row[0] else None,
                "pct_chg": float(row[1]) if row[1] else None}
    return {"close": None, "pct_chg": None}


def compare_two(query_a: str, query_b: str) -> dict[str, Any]:
    """对比两只股票：并行运行选股分析，并排展示关键指标。

    Returns:
        dict with reply_text and intent for wechat_interact.
    """
    # 1. 解析两个标的
    try:
        code_a, name_a = _resolve_symbol(query_a)
    except LookupError as e:
        return {"ok": True, "intent": "compare_error",
                "reply_text": f"未找到「{query_a}」：{e}"}
    try:
        code_b, name_b = _resolve_symbol(query_b)
    except LookupError as e:
        return {"ok": True, "intent": "compare_error",
                "reply_text": f"未找到「{query_b}」：{e}"}

    if code_a == code_b:
        return {"ok": True, "intent": "compare_error",
                "reply_text": "两只股票相同，无需对比。"}

    # 2. 并行触发选股分析（生成最新报告 + PDF）
    def _analyze(query: str) -> dict | None:
        try:
            from pick_wechat import try_handle_pick
            return try_handle_pick(f"选股 {query}")
        except Exception:
            logger.warning("对比分析 %s 失败", query, exc_info=True)
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_analyze, name_a or code_a)
        fut_b = pool.submit(_analyze, name_b or code_b)
        # 等待完成（最多 180s）
        result_a = fut_a.result(timeout=180)
        result_b = fut_b.result(timeout=180)

    # 3. 查询 DB 获取结构化指标
    pick_a = _get_latest_pick_entry(code_a)
    pick_b = _get_latest_pick_entry(code_b)
    price_a = _get_recent_price(code_a)
    price_b = _get_recent_price(code_b)
    ind_a = _get_industry(code_a)
    ind_b = _get_industry(code_b)
    conc_a = _get_concepts(code_a)
    conc_b = _get_concepts(code_b)

    # 4. 格式化对比输出
    lines = [f"【对比】{name_a or code_a} vs {name_b or code_b}", ""]

    # 综合评分行
    lines.append("| 指标 | {} | {} |".format(
        f"{name_a or code_a}({code_a})",
        f"{name_b or code_b}({code_b})",
    ))
    lines.append("|---|---|---|")

    def _score_str(v):
        return f"{v:.0f}" if v is not None else "--"

    def _price_str(v):
        return f"¥{v:.2f}" if v is not None else "--"

    def _pct_str(v):
        return f"{v:+.2f}%" if v is not None else "--"

    pa = pick_a or {}
    pb = pick_b or {}

    rows = [
        ("综合评分", _score_str(pa.get("final_composite_score")), _score_str(pb.get("final_composite_score"))),
        ("· 规则分", _score_str(pa.get("rule_composite_score")), _score_str(pb.get("rule_composite_score"))),
        ("· LLM分", _score_str(pa.get("llm_composite_score")), _score_str(pb.get("llm_composite_score"))),
        ("综合研判", pa.get("verdict", "--"), pb.get("verdict", "--")),
        ("操作建议", pa.get("recommendation", "--"), pb.get("recommendation", "--")),
        ("当前价格", _price_str(price_a["close"]), _price_str(price_b["close"])),
        ("当日涨跌", _pct_str(price_a["pct_chg"]), _pct_str(price_b["pct_chg"])),
        ("目标价", _price_str(pa.get("target_price")), _price_str(pb.get("target_price"))),
        ("建议买入价", _price_str(pa.get("buy_price")), _price_str(pb.get("buy_price"))),
        ("止损价", _price_str(pa.get("stop_loss")), _price_str(pb.get("stop_loss"))),
        ("行业", ind_a or "--", ind_b or "--"),
        ("概念", conc_a or "--", conc_b or "--"),
    ]

    for label, val_a, val_b in rows:
        lines.append(f"| {label} | {val_a} | {val_b} |")

    lines.append("")
    lines.append("💡 发送「选股 名称」可查看单只股票的完整分析报告。")

    return {
        "ok": True,
        "intent": "compare",
        "reply_text": "\n".join(lines),
        "ts_code_a": code_a,
        "name_a": name_a,
        "ts_code_b": code_b,
        "name_b": name_b,
    }
