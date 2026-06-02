#!/usr/bin/env bash
# 持仓每日简报（建议 cron 交易日 18:30）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || bash "${ROOT}/scripts/bootstrap_env.sh"
exec "$PY" "${ROOT}/main.py" watch daily "$@"
