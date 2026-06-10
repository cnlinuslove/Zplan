"""ExecutionPlan 数据模型 — 将 pick_entry 转化为贯穿交易日的可执行计划。

每个 ExecutionPlan 对应一只推荐股，从盘前到盘中依次填入各阶段快照。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import desc, select

from zplan_shared.models import PickEntry, PickRun, SessionLocal, init_db

logger = logging.getLogger(__name__)


@dataclass
class ExecutionPlan:
    """单只标的的完整执行计划，各阶段快照按时间线填入。"""

    # ── 来自 pick_entry（T-1 晚间生成）──
    ts_code: str
    name: str = ""
    rank: int = 0
    close_yesterday: float | None = None       # T-1 收盘价
    predicted_buy: float | None = None          # 建议买入价
    predicted_target: float | None = None       # 目标价
    predicted_stop: float | None = None         # 止损价
    recommendation: str = ""                    # LLM 建议
    verdict: str = ""                           # 看多/看空/观望
    final_score: float | None = None            # 最终综合分
    concepts: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    llm_trend: str = ""                         # LLM 走势简评（推荐理由）

    # ── 盘前调整（T 日 8:28 填入）──
    overnight_adjustment: float = 0.0           # 隔夜情绪调整%
    adjusted_buy: float | None = None           # 调整后建议买入价
    pre_market_notes: list[str] = field(default_factory=list)

    # ── 竞价快照（T 日 9:25 填入）──
    auction_price: float | None = None
    auction_volume: float | None = None
    auction_vs_buy_pct: float | None = None     # 竞价 vs 调整买入价 偏离%

    # ── 开盘决策（T 日 9:30-9:35 填入）──
    open_price: float | None = None
    open_action: str = ""                       # BUY_AT_OPEN | BUY_ON_PULLBACK | WAIT_OBSERVE | SKIP_TODAY
    open_reason: str = ""

    # ── 盘中监控（T 日盘中更新）──
    current_price: float | None = None
    hit_target: bool = False
    hit_stop: bool = False
    intraday_notes: list[str] = field(default_factory=list)

    # ── T+1 规划（盘后填入）──
    t1_action: str = ""                         # HOLD | SELL_TARGET | SELL_STOP | BUY_MORE
    t1_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecutionPlan":
        # 过滤掉 dataclass 不认识的 key
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid_keys})


def _safe_float(v: Any) -> float | None:
    """NaN/Inf → None，避免 JSON 序列化报错。"""
    if v is None:
        return None
    try:
        f = float(v)
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def load_latest_picks(
    top_n: int = 20,
    run_kind: str = "llm_top300",
) -> list[ExecutionPlan]:
    """从最新 pick_entries 加载 TOP N，转为 ExecutionPlan 列表。

    优先取条目数足够的最近 run。
    """
    init_db()
    with SessionLocal() as session:
        # 找最近几个 run，选条目数 >= top_n 的
        candidates = session.execute(
            select(PickRun)
            .where(PickRun.run_kind == run_kind)
            .order_by(desc(PickRun.created_at_utc))
            .limit(5)
        ).scalars().all()

        run = None
        for r in candidates:
            cnt = session.execute(
                select(PickEntry).where(PickEntry.run_id == r.id)
            ).scalars().all()
            if len(list(cnt)) >= top_n:
                run = r
                break

        if not run and candidates:
            run = candidates[0]

        if not run:
            logger.warning("无 pick run，无法生成执行计划")
            return []

        entries = session.execute(
            select(PickEntry)
            .where(PickEntry.run_id == run.id)
            .order_by(PickEntry.rank_in_run)
            .limit(top_n)
        ).scalars().all()

        plans = []
        for e in entries:
            analysis = {}
            if e.analysis_process_json:
                try:
                    analysis = json.loads(e.analysis_process_json)
                except json.JSONDecodeError:
                    pass

            llm_brief = analysis.get("llm_brief") or {}
            plans.append(ExecutionPlan(
                ts_code=e.ts_code,
                name=e.name or "",
                rank=e.rank_in_run or 0,
                close_yesterday=_safe_float(e.close_price),
                predicted_buy=_safe_float(e.predicted_buy_price),
                predicted_target=_safe_float(e.predicted_target_price),
                predicted_stop=_safe_float(e.predicted_stop_loss),
                recommendation=e.recommendation or "",
                verdict=e.verdict or "",
                final_score=_safe_float(e.final_composite_score),
                concepts=llm_brief.get("concepts") or [],
                signals=list(analysis.get("signals") or []),
                risk_flags=list(llm_brief.get("risk_flags") or []),
                llm_trend=str(llm_brief.get("trend") or ""),
            ))

        return plans


def load_plan_snapshot(plans: list[ExecutionPlan], filepath: str) -> None:
    """从 JSON 文件恢复执行计划状态（用于跨脚本续传）。"""
    from pathlib import Path
    p = Path(filepath)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        by_code = {item["ts_code"]: item for item in data}
        for plan in plans:
            if plan.ts_code in by_code:
                saved = by_code[plan.ts_code]
                # 只恢复盘前/竞价/开盘字段，不覆盖 T-1 原始数据
                for key in ("overnight_adjustment", "adjusted_buy", "pre_market_notes",
                           "auction_price", "auction_volume", "auction_vs_buy_pct",
                           "open_price", "open_action", "open_reason",
                           "current_price", "hit_target", "hit_stop", "intraday_notes"):
                    if key in saved:
                        setattr(plan, key, saved[key])
    except Exception:
        logger.warning("加载执行计划快照失败: %s", filepath, exc_info=True)


def save_plan_snapshot(plans: list[ExecutionPlan], filepath: str) -> None:
    """将执行计划状态保存到 JSON 文件。"""
    from pathlib import Path
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [plan.to_dict() for plan in plans]
    p.write_text(json.dumps(data, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
