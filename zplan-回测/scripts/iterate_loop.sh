#!/usr/bin/env bash
# 选股 ↔ 回测迭代闭环（可挂 crontab）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-verify}"
PY="${ROOT}/.venv/bin/python"

case "$MODE" in
  verify)
    "$PY" "${ROOT}/main.py" iterate verify
    ;;
  full)
    "$PY" "${ROOT}/main.py" iterate full
    ;;
  history)
    "$PY" "${ROOT}/main.py" iterate history "${@:2}"
    ;;
  diff)
    "$PY" "${ROOT}/main.py" iterate diff
    ;;
  *)
    echo "用法: $0 {verify|full|history|diff}"
    exit 1
    ;;
esac
