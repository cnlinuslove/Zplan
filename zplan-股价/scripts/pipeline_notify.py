#!/usr/bin/env python3
"""管道完成后一句话播报：股价 + 资讯是否正常更新。"""
from __future__ import annotations

import os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

NEWS_ROOT = Path(os.environ.get("ZPLAN_ROOT", Path(__file__).resolve().parents[2] / "zplan-资讯"))
sys.path.insert(0, str(NEWS_ROOT))

from zplan_shared.models import init_db, SessionLocal
from sqlalchemy import text
from wechat_push import push_wechat_text

def check():
    init_db(); s = SessionLocal()
    now = datetime.now().date()

    # 股价：最新交易日
    r = s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).fetchone()
    from datetime import date
    price_date_raw = r[0]
    if isinstance(price_date_raw, str): price_date_raw = date.fromisoformat(price_date_raw)
    price_ok = bool(price_date_raw) and (now - price_date_raw).days <= 1
    price_date = str(price_date_raw) if price_date_raw else "无"

    # 资讯：最新发布时间
    r2 = s.execute(text("SELECT MAX(published_at_utc) FROM financial_alerts")).fetchone()
    news_latest = r2[0]
    if news_latest:
        if isinstance(news_latest, str):
            news_latest = datetime.strptime(news_latest[:19], "%Y-%m-%d %H:%M:%S")
        news_ok = (now - news_latest.date()).days <= 1
        news_date = news_latest.strftime("%m-%d %H:%M")
    else:
        news_ok = False
        news_date = "无"

    s.close()

    parts = []
    parts.append("✅ 股价" if price_ok else "🔴 股价")
    parts[-1] += f" 至{price_date}"
    parts.append("✅ 资讯" if news_ok else "🔴 资讯")
    parts[-1] += f" 至{news_date}"

    ok = price_ok and news_ok
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    return f"Z-Plan {now_str} | {' | '.join(parts)}"


def main():
    msg = check()
    ok = push_wechat_text(msg)
    print(f"播报 {'成功' if ok else '失败'} — {msg}")


if __name__ == "__main__":
    main()
