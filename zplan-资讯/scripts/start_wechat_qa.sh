#!/usr/bin/env bash
# 启动微信问答 HTTP 服务（含企业微信应用回调）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "请先运行 ./scripts/bootstrap_env.sh" >&2
  exit 1
fi
HOST="${WECHAT_SERVE_HOST:-0.0.0.0}"
PORT="${WECHAT_SERVE_PORT:-8765}"
echo "Z-Plan 微信问答：http://${HOST}:${PORT}"
echo "  - 企业微信回调: /v1/wework/callback"
echo "  - OpenClaw/HTTP: POST /v1/wechat/reply  {\"text\":\"北向资金最近怎样\",\"push\":true}"
exec "$PY" openclaw_bridge.py wechat-serve --host "$HOST" --port "$PORT"
