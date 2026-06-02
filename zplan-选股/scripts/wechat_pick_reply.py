#!/usr/bin/env python3
"""stdin/argv 一句话选股 → stdout JSON（供 zplan-资讯 企业微信桥调用）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pick_agent.wechat_pick import handle_wechat_pick_message  # noqa: E402


def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    out = handle_wechat_pick_message(text.strip())
    if out is None:
        print(json.dumps({"ok": False, "intent": "pick_skip"}, ensure_ascii=False))
        return
    # 报告体过大，勿序列化
    slim = {k: v for k, v in out.items() if k != "report"}
    print(json.dumps(slim, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
