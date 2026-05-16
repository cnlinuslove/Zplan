#!/usr/bin/env bash
# 从 zplan/.env 的 GEMINI_API_KEY 配置 OpenClaw Agent（企微回复需要 LLM）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少 ${ENV_FILE}" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "请在 .env 设置 GEMINI_API_KEY" >&2
  exit 1
fi

MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
openclaw onboard --non-interactive --accept-risk --mode local \
  --auth-choice gemini-api-key --gemini-api-key "$GEMINI_API_KEY" --flow quickstart

openclaw config set agents.defaults.model.primary "google/${MODEL}"
openclaw config set tools.profile coding
openclaw config set agents.defaults.skills '["zplan-qa"]' --strict-json 2>/dev/null || \
  openclaw config set agents.defaults.skills zplan-qa 2>/dev/null || true

echo "OpenClaw 已配置 Gemini 模型: google/${MODEL}"
echo "请执行: openclaw gateway restart"
