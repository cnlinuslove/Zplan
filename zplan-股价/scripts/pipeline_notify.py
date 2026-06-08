#!/usr/bin/env python3
"""管道完成后全模块播报：股价 + 衍生 + 估值 + 筹码 + 季报 + 资讯 + 规则打分。

用法:
    pipeline_notify.py [--alert] [pipeline_log_path]
    --alert  有模块异常时额外发送 🔴 告警
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(os.environ.get("ZPLAN_ROOT", Path(__file__).resolve().parents[2] / "zplan-资讯"))
sys.path.insert(0, str(NEWS_ROOT))

from zplan_shared.models import init_db, SessionLocal
from sqlalchemy import text
from wechat_push import push_wechat_text

BEIJING_TZ = timezone(timedelta(hours=8))


def check_all_modules() -> dict[str, dict]:
    """检查所有数据模块新鲜度。返回 {label: {count, latest, ok, threshold_days}}。"""
    init_db()
    s = SessionLocal()
    now = datetime.now().date()

    modules: dict[str, dict] = {}

    checks = [
        # (label, table, date_col, threshold_days)
        ("股价日线",     "daily_prices",          "trade_date",      1),
        ("衍生指标",     "daily_features",        "trade_date",      1),
        ("估值截面",     "daily_snapshot",        "trade_date",      1),
        ("筹码峰",       "daily_chip",            "trade_date",      1),
        ("季报财务",     "financial_indicators",  "report_date",   100),  # 季报滞后正常
        ("东财快讯",     "financial_alerts",      "published_at_utc", 1),
        ("规则打分",     "stock_rule_scores",     "trade_date_as_of", 7),  # 每周
    ]

    for label, table, col, threshold in checks:
        try:
            r = s.execute(text(f"SELECT COUNT(*), MAX({col}) FROM {table}")).fetchone()
            cnt, latest_raw = r[0], r[1]
        except Exception:
            modules[label] = {"count": 0, "latest": "无", "ok": False, "threshold": threshold}
            continue

        if latest_raw is None:
            modules[label] = {"count": cnt or 0, "latest": "无", "ok": False, "threshold": threshold}
            continue

        # 统一为 date
        if isinstance(latest_raw, str):
            if " " in latest_raw:
                latest_date = datetime.strptime(latest_raw[:19], "%Y-%m-%d %H:%M:%S").date()
            elif "T" in latest_raw:
                latest_date = date.fromisoformat(latest_raw[:10])
            else:
                latest_date = date.fromisoformat(latest_raw[:10])
        elif isinstance(latest_raw, datetime):
            latest_date = latest_raw.date()
        else:
            latest_date = latest_raw

        days_ago = (now - latest_date).days
        ok = days_ago <= threshold

        modules[label] = {
            "count": cnt or 0,
            "latest": str(latest_date),
            "days_ago": days_ago,
            "ok": ok,
            "threshold": threshold,
        }

    s.close()
    return modules


def build_message(modules: dict[str, dict], alert_mode: bool = False) -> tuple[str, bool]:
    """构建播报消息。返回 (message, has_anomaly)。"""
    now_str = datetime.now(BEIJING_TZ).strftime("%m-%d %H:%M")
    lines = [f"Z-Plan {now_str}"]

    all_ok = True
    for label, info in modules.items():
        status = "✅" if info["ok"] else "🔴"
        latest_str = info["latest"]
        if not info["ok"] and info.get("days_ago") is not None:
            latest_str = f"{info['latest']}（{info['days_ago']}天前）"
        elif info["latest"] == "无":
            latest_str = "无数据"
        lines.append(f"{status} {label} 至{latest_str}")
        if not info["ok"]:
            all_ok = False

    if alert_mode and not all_ok:
        lines.insert(1, "🚨 数据管道异常，以下模块未及时更新：")
    elif all_ok:
        lines.insert(1, "今日数据已全部就绪。")

    return " | ".join(lines), all_ok


def parse_pipeline_log(log_path: str) -> dict[str, str] | None:
    """从管道日志中提取步骤状态。返回 {label: result} 或 None。"""
    try:
        with open(log_path) as f:
            text = f.read()
    except Exception:
        return None

    steps: dict[str, str] = {}
    for line in text.splitlines():
        for prefix in ("  ✅ ", "  ❌ ", "  ⏭️ "):
            if line.startswith(prefix):
                # 格式: "  ✅ 标签 (时间)"
                rest = line[len(prefix):].strip()
                if " (" in rest:
                    label, _ = rest.rsplit(" (", 1)
                    result = {"✅": "OK", "❌": "FAIL", "⏭️": "SKIP"}[prefix]
                    steps[label] = result
    return steps if steps else None


def main():
    alert_mode = "--alert" in sys.argv
    log_path = None
    for a in sys.argv[1:]:
        if a != "--alert" and not a.startswith("--"):
            log_path = a
            break

    modules = check_all_modules()
    msg, all_ok = build_message(modules, alert_mode=alert_mode)

    # 如果有管道日志，额外附上步骤摘要
    if log_path:
        steps = parse_pipeline_log(log_path)
        if steps:
            fail_steps = [k for k, v in steps.items() if v == "FAIL"]
            if fail_steps:
                msg += f"\n🔧 失败步骤: {', '.join(fail_steps)}"

    ok = push_wechat_text(msg)
    print(f"播报 {'成功' if ok else '失败'} — {msg}")

    # 有异常且非告警模式时，再发一条告警（去重）
    if not all_ok and not alert_mode:
        alert_msg, _ = build_message(modules, alert_mode=True)
        if log_path:
            steps = parse_pipeline_log(log_path)
            if steps:
                fail_steps = [k for k, v in steps.items() if v == "FAIL"]
                if fail_steps:
                    alert_msg += f"\n🔧 失败步骤: {', '.join(fail_steps)}"
        push_wechat_text(alert_msg)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
