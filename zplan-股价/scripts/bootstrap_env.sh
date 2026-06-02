#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
REQ="${ROOT}/requirements.txt"
SHARED="${ROOT}/../zplan-共享"

if command -v uv >/dev/null 2>&1; then
  [[ -x .venv/bin/python ]] || uv venv
  uv pip install -e "$SHARED" -e "$ROOT" -r "$REQ" --python .venv/bin/python
else
  [[ -x .venv/bin/python ]] || python3 -m venv .venv
  .venv/bin/python -m pip install -U pip
  .venv/bin/python -m pip install -e "$SHARED" -e "$ROOT" -r "$REQ"
fi
if [[ "${ZPLAN_UPGRADE_AKSHARE:-0}" == "1" ]]; then
  "${ROOT}/scripts/upgrade_akshare.sh"
fi
echo "完成: ${ROOT}/.venv"
