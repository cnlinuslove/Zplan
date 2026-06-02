#!/usr/bin/env bash
# 烟测：清 demo + A.1 拉前 N 只（日线源见 AKSHARE_DAILY_PROVIDER）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LIMIT="${1:-5}"
[[ -x .venv/bin/python ]] || ./scripts/bootstrap_env.sh
.venv/bin/python scripts/check_akshare_connectivity.py --quick --require any
exec .venv/bin/python main.py --a1 --init --limit "$LIMIT" --realign-source
