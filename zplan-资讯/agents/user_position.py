"""用户持仓 CRUD — 企微个人持仓追踪。"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from models import SessionLocal
from zplan_shared.market import get_realtime_quote, resolve_ts_code
from zplan_shared.models import PickWatchlist, StockList, UserPosition, init_db

logger = logging.getLogger(__name__)

# ── 解析买入命令 ──────────────────────────────────────────────

_BUY_RE = re.compile(
    r"^买入\s+(?P<symbol>.+?)\s+(?P<shares>\d+)\s*(?:股)?\s*"
    r"(?:@\s*|¥\s*)?(?P<price>[\d.]+)?\s*"
    r"(?P<notes>.+)?$"
)
_SELL_RE = re.compile(r"^卖出\s+(?P<symbol>.+?)\s*$")


def parse_buy_command(text: str) -> dict[str, Any] | None:
    """解析 "买入 爱普股份 1000股 12.50" → {symbol, shares, price, notes}。"""
    m = _BUY_RE.match(text.strip())
    if not m:
        return None
    shares = int(m.group("shares"))
    if shares <= 0:
        return None
    price_str = m.group("price")
    price = float(price_str) if price_str else None
    return {
        "symbol": m.group("symbol").strip(),
        "shares": shares,
        "price": price,
        "notes": (m.group("notes") or "").strip() or None,
    }


def parse_sell_command(text: str) -> str | None:
    """解析 "卖出 爱普股份" → symbol。"""
    m = _SELL_RE.match(text.strip())
    return m.group("symbol").strip() if m else None


# ── 股票名解析（本地，不依赖 pick_watchlist 的 _resolve）────────

def _resolve_symbol(query: str) -> tuple[str, str]:
    """返回 (ts_code, name) 或 raise LookupError。"""
    raw = query.strip()
    # 6 位代码
    if re.fullmatch(r"\d{6}", raw):
        code = resolve_ts_code(raw)
        with SessionLocal() as session:
            name = session.execute(
                select(StockList.name).where(StockList.ts_code == code)
            ).scalar_one_or_none()
        if not name:
            raise LookupError(f"未找到代码 {raw}")
        return code, str(name)

    # 名称匹配（精确优先）
    key = raw.replace(" ", "")
    with SessionLocal() as session:
        rows = session.execute(
            select(StockList.ts_code, StockList.name)
            .where(StockList.name == raw)
            .limit(5)
        ).all()
        if not rows:
            rows = session.execute(
                select(StockList.ts_code, StockList.name)
                .where(StockList.name.contains(key))
                .limit(10)
            ).all()
    if not rows:
        raise LookupError(f"未找到匹配「{query}」的股票")
    if len(rows) == 1:
        return str(rows[0][0]), str(rows[0][1])
    exact = [r for r in rows if str(r[1]) == raw]
    if len(exact) == 1:
        return str(exact[0][0]), str(exact[0][1])
    raise LookupError(
        "匹配多只：" + "、".join(f"{r[1]}({r[0]})" for r in rows[:6])
    )


# ── CRUD ──────────────────────────────────────────────────────

def add_position(
    user_id: str,
    query: str,
    shares: int,
    buy_price: float | None = None,
    buy_date_str: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """新增或追加持仓。已存在则累加股数并更新成本均价。"""
    init_db()
    code, name = _resolve_symbol(query)
    buy_date = None
    if buy_date_str:
        try:
            buy_date = datetime.fromisoformat(buy_date_str)
        except ValueError:
            pass
    if buy_date is None:
        buy_date = datetime.utcnow()

    with SessionLocal() as session:
        existing = session.execute(
            select(UserPosition).where(
                UserPosition.user_id == user_id,
                UserPosition.ts_code == code,
            )
        ).scalar_one_or_none()

        if existing:
            # 累加：加权平均成本
            total_shares = existing.shares + shares
            if buy_price is not None and existing.buy_price is not None:
                avg_price = (
                    existing.shares * existing.buy_price + shares * buy_price
                ) / total_shares
            elif buy_price is not None:
                avg_price = buy_price
            else:
                avg_price = existing.buy_price

            existing.shares = total_shares
            existing.buy_price = round(avg_price, 3) if avg_price else None
            existing.buy_date = buy_date
            existing.notes = notes or existing.notes
            existing.updated_at_utc = datetime.utcnow()
            session.commit()
            return {
                "action": "updated",
                "ts_code": code,
                "name": name,
                "shares": total_shares,
                "buy_price": avg_price,
            }

        # 新增
        session.add(
            UserPosition(
                user_id=user_id,
                ts_code=code,
                name=name,
                shares=shares,
                buy_price=buy_price,
                buy_date=buy_date,
                notes=notes,
            )
        )
        session.commit()
    return {
        "action": "added",
        "ts_code": code,
        "name": name,
        "shares": shares,
        "buy_price": buy_price,
    }


def remove_position(user_id: str, query: str) -> dict[str, Any] | None:
    """删除持仓记录。返回被删记录信息或 None。"""
    init_db()
    code, name = _resolve_symbol(query)
    with SessionLocal() as session:
        row = session.execute(
            select(UserPosition).where(
                UserPosition.user_id == user_id,
                UserPosition.ts_code == code,
            )
        ).scalar_one_or_none()
        if not row:
            return None
        info = {"ts_code": row.ts_code, "name": row.name, "shares": row.shares}
        session.delete(row)
        session.commit()
    return info


def list_positions(user_id: str) -> list[dict[str, Any]]:
    """列出用户所有持仓。"""
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(UserPosition)
            .where(UserPosition.user_id == user_id)
            .order_by(UserPosition.updated_at_utc.desc())
        ).scalars().all()
    return [
        {
            "ts_code": r.ts_code,
            "name": r.name,
            "shares": r.shares,
            "buy_price": r.buy_price,
            "buy_date": str(r.buy_date) if r.buy_date else None,
            "notes": r.notes,
        }
        for r in rows
    ]


def format_positions_text(user_id: str) -> str:
    """格式化持仓为企微文本（含现价 + 盈亏估算）。"""
    positions = list_positions(user_id)
    if not positions:
        return "暂无持仓记录。\n发送「买入 股票名 股数 价格」添加持仓。"

    # 批量获取实时行情
    codes = [p["ts_code"] for p in positions]
    quotes: dict[str, dict] = {}
    try:
        from zplan_shared.market import get_realtime_quotes_batch
        quotes = get_realtime_quotes_batch(codes)
    except Exception:
        logger.warning("获取实时行情失败", exc_info=True)

    lines = ["【我的持仓】", ""]

    total_cost = 0.0
    total_value = 0.0

    for p in positions:
        code = p["ts_code"]
        name = p["name"] or code
        shares = p["shares"]
        cost = p["buy_price"]
        q = quotes.get(code) or {}
        current_price = q.get("price") if q else None

        if cost is not None:
            cost_value = cost * shares
            total_cost += cost_value
        else:
            cost_value = None

        if current_price is not None:
            current_value = current_price * shares
            total_value += current_value
            if cost is not None and cost > 0:
                pnl_pct = (current_price / cost - 1) * 100
                pnl_amount = current_value - cost_value
                pnl_str = f"{pnl_pct:+.2f}%  {pnl_amount:+.0f}"
            else:
                pnl_str = f"¥{current_price:.2f}"
        else:
            pnl_str = "--"

        price_str = f"¥{current_price:.2f}" if current_price else "--"
        cost_str = f"¥{cost:.2f}" if cost else "--"

        lines.append(
            f"{name}({code})  {shares}股  "
            f"成本{cost_str}  现价{price_str}  "
            f"盈亏 {pnl_str}"
        )
        if p.get("notes"):
            lines.append(f"  📝 {p['notes']}")

    lines.append("")
    if total_cost > 0 and total_value > 0:
        total_pnl = total_value - total_cost
        total_pnl_pct = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0
        lines.append(f"💰 总成本 ¥{total_cost:.0f}  |  总市值 ¥{total_value:.0f}  |  总盈亏 {total_pnl_pct:+.2f}% (¥{total_pnl:+.0f})")
    lines.append("")
    lines.append("发送「买入 名称 股数 价格」添加 | 「卖出 名称」移除")

    return "\n".join(lines)


def format_watchlist_text() -> str:
    """格式化自选列表为企微文本。"""
    from zplan_shared.pick_watchlist import list_watch

    items = list_watch(enabled_only=True)
    if not items:
        return "暂无自选股票。\n发送「加入自选 股票名」添加。"

    lines = ["【我的自选】", ""]
    for i, w in enumerate(items):
        name = w["name"] or w["ts_code"]
        code = w["ts_code"]
        note = f" — {w['note']}" if w.get("note") else ""
        lines.append(f"{i+1}. {name}({code}){note}")
    lines.append("")
    lines.append("发送「选股 名称」查看分析 | 「移除自选 名称」删除")
    return "\n".join(lines)
