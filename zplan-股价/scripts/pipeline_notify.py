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
    """检查所有数据模块新鲜度 + 截面数量。返回 {label: {count, latest, ok, ...}}。"""
    init_db()
    s = SessionLocal()
    now = datetime.now().date()

    modules: dict[str, dict] = {}

    checks = [
        # (label, table, date_col, threshold_days, prev_day_min_pct)  prev_day_min_pct=99 表示不低于前日 99%
        ("股价日线",     "daily_prices",          "trade_date",      1, 99),
        ("衍生指标",     "daily_features",        "trade_date",      1, 99),
        ("估值截面",     "daily_snapshot",        "trade_date",      1, 97),
        ("筹码峰",       "daily_chip",            "trade_date",      1, None),
        ("季报财务",     "financial_indicators",  "report_date",   100, None),
        ("东财快讯",     "financial_alerts",      "published_at_utc", 1, None),
        ("规则打分",     "stock_rule_scores",     "trade_date_as_of", 7, None),
    ]

    for label, table, col, threshold, min_pct in checks:
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

        # 日期对但截面数量异常（对比前一个交易日，差异 > 允许值告警）
        count_note = ""
        if ok and min_pct is not None:
            try:
                today_cnt = s.execute(
                    text(f"SELECT COUNT(DISTINCT ts_code) FROM {table} WHERE {col}=:d"),
                    {"d": str(latest_date)},
                ).fetchone()[0]
                # 找前一个交易日日期
                prev_date = s.execute(
                    text(f"SELECT MAX({col}) FROM {table} WHERE {col}<:d"),
                    {"d": str(latest_date)},
                ).fetchone()
                if prev_date and prev_date[0]:
                    prev_cnt = s.execute(
                        text(f"SELECT COUNT(DISTINCT ts_code) FROM {table} WHERE {col}=:pd"),
                        {"pd": str(prev_date[0])},
                    ).fetchone()[0]
                    if today_cnt > 0 and prev_cnt > 0:
                        pct = today_cnt / prev_cnt * 100
                        if pct < min_pct:
                            count_note = f"截面{today_cnt}只(前日{prev_cnt},仅{pct:.1f}%)"
            except Exception:
                pass

        modules[label] = {
            "count": cnt or 0,
            "latest": str(latest_date),
            "days_ago": days_ago,
            "ok": ok and not count_note,
            "threshold": threshold,
            "count_note": count_note,
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
        extra = info.get("count_note", "")
        if not info["ok"] and info.get("days_ago") is not None and info.get("days_ago", 0) > info.get("threshold", 99):
            latest_str = f"{info['latest']}（{info['days_ago']}天前）"
        elif info["latest"] == "无":
            latest_str = "无数据"
        if extra:
            lines.append(f"{status} {label} 至{latest_str} {extra}")
        else:
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
    mode = "done"
    log_path = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--mode" and i + 1 < len(args):
            mode = args[i + 1]; i += 2
        elif a.startswith("--mode="):
            mode = a.split("=", 1)[1]; i += 1
        elif not a.startswith("--"):
            log_path = a; i += 1
        else:
            i += 1

    # ── 启动通知：简短 ──
    if mode == "start":
        now_str = datetime.now(BEIJING_TZ).strftime("%m-%d %H:%M")
        msg = f"🚀 Z-Plan 管道启动 {now_str}"
        push_wechat_text(msg)
        print(f"启动播报 — {msg}")
        return

    # ── 崩溃通知 ──
    if mode == "crashed":
        now_str = datetime.now(BEIJING_TZ).strftime("%m-%d %H:%M")
        msg = f"💀 Z-Plan 管道异常终止 {now_str}\n已自动清理锁文件，后续健康检查将尝试补跑"
        if log_path:
            msg += f"\n日志: {log_path}"
        push_wechat_text(msg)
        print(f"崩溃播报 — {msg}")
        return

    # ── 正常完成：全模块状态 ──
    modules = check_all_modules()
    msg, all_ok = build_message(modules, alert_mode=False)

    # 如果有管道日志，额外附上步骤摘要 + LLM 消耗
    if log_path:
        steps = parse_pipeline_log(log_path)
        if steps:
            fail_steps = [k for k, v in steps.items() if v == "FAIL"]
            if fail_steps:
                msg += f"\n🔧 失败步骤: {', '.join(fail_steps)}"
        # 从日志抓 LLM_COST 行
        try:
            with open(log_path) as f:
                for line in f:
                    if "LLM_COST:" in line and "run=" in line:
                        parts = line.strip().split("LLM_COST:")[1].strip()
                        # parts: run=153 model=deepseek-v4 in=122648 out=1272 total=159632 usd=0.0345 cny=0.25
                        data = {}
                        for kv in parts.split():
                            if "=" in kv:
                                k, v = kv.split("=", 1)
                                data[k] = v
                        cny = data.get("cny", "?")
                        total = data.get("total", "?")
                        msg += f"\n🤖 LLM: {total} tokens · ¥{cny}"
                        break
        except Exception:
            pass

    ok = push_wechat_text(msg)
    print(f"播报 {'成功' if ok else '失败'} — {msg}")

    # 有异常时额外发告警
    if not all_ok:
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
