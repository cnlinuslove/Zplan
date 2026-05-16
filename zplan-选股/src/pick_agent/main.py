from __future__ import annotations

import argparse
import logging

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.market import get_panel, latest_trade_date
from zplan_shared.models import init_db
from zplan_shared.pick_context import get_pick_context


logger = logging.getLogger(__name__)


def run_pick_agent(*, top_n: int = 20) -> dict:
    """选股 Agent：基于最新收盘价做占位筛选（待替换为策略逻辑）。"""
    init_db()
    as_of = latest_trade_date()
    if as_of is None:
        return {
            "ok": True,
            "agent": "pick",
            "zplan_root": str(ZPLAN_ROOT),
            "picks": [],
            "message": "无日线数据，请先运行 zplan-股价",
        }
    panel = get_panel(as_of, fields=["close", "pct_chg", "turnover_rate"])
    if panel.empty:
        return {
            "ok": True,
            "agent": "pick",
            "zplan_root": str(ZPLAN_ROOT),
            "picks": [],
            "message": "无日线数据，请先运行 zplan-股价",
        }
    ranked = panel.dropna(subset=["close"]).sort_values("close", ascending=False).head(top_n)
    picks = []
    for row in ranked.itertuples(index=False):
        ctx = get_pick_context(row.ts_code)
        intraday = ctx.get("intraday") or {}
        picks.append(
            {
                "ts_code": row.ts_code,
                "name": ctx.get("name"),
                "close": row.close,
                "pct_chg": row.pct_chg,
                "turnover_rate": row.turnover_rate,
                "volume_ratio_vs_prior": intraday.get("volume_ratio_vs_prior"),
                "afternoon_volume_share": intraday.get("afternoon_volume_share"),
                "news_mentions_48h": (ctx.get("news_mentions") or {}).get("total", 0),
            }
        )
    return {
        "ok": True,
        "agent": "pick",
        "zplan_root": str(ZPLAN_ROOT),
        "as_of": str(as_of),
        "picks": picks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan 选股 Agent")
    parser.add_argument("--top", type=int, default=20, help="返回前 N 只（占位：按收盘价降序）")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_pick_agent(top_n=args.top)
    logger.info("完成: %s", result)


if __name__ == "__main__":
    main()
