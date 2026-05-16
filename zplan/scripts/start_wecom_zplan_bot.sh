#!/usr/bin/env bash
# Z-Plan 企微直连（推荐）：不依赖 OpenClaw Agent，@机器人即可问答
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "缺少 .env（需 WECOM_BOT_ID / WECOM_BOT_SECRET）" >&2
  exit 1
fi

if command -v openclaw >/dev/null 2>&1; then
  if openclaw gateway status 2>/dev/null | grep -q "Runtime: running"; then
    echo "⚠️  OpenClaw 网关正在运行，会与直连 Bot 抢同一企微长连接。"
    echo "    建议先执行: openclaw gateway stop"
    echo "    然后重新运行本脚本。"
    echo ""
  fi
fi

if ! command -v node >/dev/null 2>&1; then
  echo "需要 Node.js（运行 wecom_zplan_bot.mjs）" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "请先: ./scripts/bootstrap_env.sh" >&2
  exit 1
fi

exec node "$(dirname "$0")/wecom_zplan_bot.mjs"
