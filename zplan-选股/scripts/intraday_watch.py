#!/usr/bin/env python3
"""盘中监控 CLI — 交易时段每 30 分钟触发，仅在触及关键价位时推送。

用法:
    cd zplan-选股 && .venv/bin/python scripts/intraday_watch.py
    cd zplan-选股 && .venv/bin/python scripts/intraday_watch.py --dry-run
    cd zplan-选股 && .venv/bin/python scripts/intraday_watch.py --top 15
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
ZPLAN_ROOT = os.environ.get("ZPLAN_ROOT", str(Path(__file__).resolve().parents[2] / "zplan-资讯"))
for p in (SRC, ZPLAN_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from execution.intraday import run_intraday_check


def main():
    dry_run = "--dry-run" in sys.argv
    top_n = 10
    for i, arg in enumerate(sys.argv):
        if arg == "--top" and i + 1 < len(sys.argv):
            try:
                top_n = int(sys.argv[i + 1])
            except ValueError:
                pass

    result = run_intraday_check(top_n=top_n, dry_run=dry_run)

    if result.get("skipped"):
        print(f"[跳过] {result['reason']}")
        return

    if not result.get("ok"):
        print(f"[错误] {result.get('error', '未知错误')}")
        sys.exit(1)

    # 无触发信号时静默
    if result.get("silent"):
        print(f"[{result.get('date')} {result.get('time')}] 无触发信号")
        return

    markdown = result.get("markdown", "")
    events = result.get("events", [])

    if dry_run:
        print("=" * 60)
        print(f"[DRY RUN] 盘中信号 {len(events)} 条:")
        print("=" * 60)
        print(markdown)
        print("=" * 60)
        return

    if not events:
        return

    try:
        from wechat_push import push_wechat_markdown
        ok = push_wechat_markdown(markdown)
        if ok:
            print(f"[{result['date']} {result['time']}] ✅ 盘中信号推送成功 ({len(events)} 条)")
        else:
            print(f"[{result['date']} {result['time']}] ❌ 推送失败")
            sys.exit(1)
    except ImportError:
        print("[错误] wechat_push 不可用")
        print(markdown)
        sys.exit(1)


if __name__ == "__main__":
    main()
