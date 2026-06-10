#!/usr/bin/env python3
"""盘前简报 CLI — T 日 8:28 由 launchd 触发。

用法:
    cd zplan-选股 && .venv/bin/python scripts/pre_market_briefing.py
    cd zplan-选股 && .venv/bin/python scripts/pre_market_briefing.py --dry-run   # 仅打印
    cd zplan-选股 && .venv/bin/python scripts/pre_market_briefing.py --top 15    # 检查 TOP15

依赖: 选股流水线已完成（pick_entries 中有最新推荐）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保 src 在 path
SRC = str(Path(__file__).resolve().parents[1] / "src")
ZPLAN_ROOT = os.environ.get("ZPLAN_ROOT", str(Path(__file__).resolve().parents[2] / "zplan-资讯"))
for p in (SRC, ZPLAN_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from execution.pre_market import run_pre_market_check


def main():
    dry_run = "--dry-run" in sys.argv
    top_n = 10
    for i, arg in enumerate(sys.argv):
        if arg == "--top" and i + 1 < len(sys.argv):
            try:
                top_n = int(sys.argv[i + 1])
            except ValueError:
                pass

    result = run_pre_market_check(top_n=top_n, dry_run=dry_run)

    if result.get("skipped"):
        print(f"[跳过] {result['reason']}")
        return

    if not result.get("ok"):
        print(f"[错误] {result.get('error', '未知错误')}")
        sys.exit(1)

    markdown = result.get("markdown", "")
    if not markdown:
        print("[错误] 生成简报为空")
        sys.exit(1)

    if dry_run:
        print("=" * 60)
        print("[DRY RUN] 盘前简报预览:")
        print("=" * 60)
        print(markdown)
        print("=" * 60)
        return

    # 企微推送
    try:
        from wechat_push import push_wechat_markdown
        ok = push_wechat_markdown(markdown)
        if ok:
            print(f"[{result['date']}] ✅ 盘前简报推送成功")
        else:
            print(f"[{result['date']}] ❌ 推送失败")
            sys.exit(1)
    except ImportError:
        print("[错误] wechat_push 模块不可用，请确认在 zplan-资讯 的 .venv 环境下运行")
        print("")
        print(markdown)
        sys.exit(1)


if __name__ == "__main__":
    main()
