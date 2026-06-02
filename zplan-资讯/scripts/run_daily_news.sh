#!/usr/bin/env bash
# 每日资讯更新（供 cron / LaunchAgent 调用）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "缺少 .venv，请先: ./scripts/bootstrap_env.sh" >&2
  exit 1
fi

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
LOG="${ROOT}/logs/cron_daily_news.log"
mkdir -p "${ROOT}/logs"

{
  echo "======== $(date '+%Y-%m-%d %H:%M:%S %z') ========"
  exec "$PY" "${ROOT}/daily_news_job.py" "$@"
} >>"$LOG" 2>&1
