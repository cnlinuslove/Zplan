#!/usr/bin/env bash
# 从 zplan/.env 同步企业微信机器人 Bot ID / Secret 到 OpenClaw（仅企微渠道）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少 ${ENV_FILE}，请先配置 WECOM_BOT_ID 与 WECOM_BOT_SECRET" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

if [[ -z "${WECOM_BOT_ID:-}" || -z "${WECOM_BOT_SECRET:-}" ]]; then
  echo "请在 .env 中设置：" >&2
  echo "  WECOM_BOT_ID=..." >&2
  echo "  WECOM_BOT_SECRET=..." >&2
  echo "（企业微信工作台 → 智能机器人 → API 模式 → 长连接）" >&2
  exit 1
fi

if ! command -v openclaw >/dev/null 2>&1; then
  echo "未找到 openclaw 命令" >&2
  exit 1
fi

openclaw config set channels.wecom.enabled true
openclaw config set channels.wecom.connectionMode websocket
openclaw config set channels.wecom.botId "$WECOM_BOT_ID"
openclaw config set channels.wecom.secret "$WECOM_BOT_SECRET"
openclaw config set plugins.entries.wechat.enabled false
openclaw config set plugins.entries.wecom-openclaw-plugin.enabled true

echo "已写入 OpenClaw channels.wecom（Bot ID: ${WECOM_BOT_ID:0:6}…）"
echo "重启网关: openclaw gateway restart"
