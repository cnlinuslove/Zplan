"""行情数据就绪与新鲜度检查（选股/回测门禁）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from zplan_shared.market import get_panel, latest_trade_date


# 全市场就绪参考阈值（烟测库可下调）
MIN_PANEL_ROWS = 300
MIN_PANEL_ROWS_PRODUCTION = 4000
MAX_STALE_CALENDAR_DAYS = 3


@dataclass
class MarketHealth:
    ok: bool
    latest: date | None
    panel_rows: int
    stale_calendar_days: int | None
    message: str


def _approx_stale_days(latest: date, today: date | None = None) -> int:
    ref = today or date.today()
    return (ref - latest).days


def check_market_health(
    *,
    min_panel_rows: int = MIN_PANEL_ROWS,
    max_stale_days: int = MAX_STALE_CALENDAR_DAYS,
    production: bool = False,
) -> MarketHealth:
    """检查日线截面规模与最新交易日新鲜度。"""
    if production:
        min_panel_rows = max(min_panel_rows, MIN_PANEL_ROWS_PRODUCTION)

    latest = latest_trade_date()
    if latest is None:
        return MarketHealth(
            ok=False,
            latest=None,
            panel_rows=0,
            stale_calendar_days=None,
            message="无日线数据，请先运行 zplan-股价：cd zplan-股价 && .venv/bin/python main.py --a1",
        )

    panel = get_panel(latest)
    rows = len(panel)
    stale = _approx_stale_days(latest)

    if rows < min_panel_rows:
        return MarketHealth(
            ok=False,
            latest=latest,
            panel_rows=rows,
            stale_calendar_days=stale,
            message=(
                f"截面仅 {rows} 只，低于阈值 {min_panel_rows}；"
                "请运行 zplan-股价 main.py --a1 同步全市场"
            ),
        )

    if stale > max_stale_days:
        return MarketHealth(
            ok=False,
            latest=latest,
            panel_rows=rows,
            stale_calendar_days=stale,
            message=(
                f"最新交易日 {latest} 已滞后约 {stale} 个自然日（阈值 {max_stale_days}），"
                "请更新行情后再选股"
            ),
        )

    return MarketHealth(
        ok=True,
        latest=latest,
        panel_rows=rows,
        stale_calendar_days=stale,
        message=f"行情就绪：{rows} 只，最新交易日 {latest}",
    )


def assert_market_ready(**kwargs: object) -> MarketHealth:
    health = check_market_health(**kwargs)  # type: ignore[arg-type]
    if not health.ok:
        raise RuntimeError(health.message)
    return health
