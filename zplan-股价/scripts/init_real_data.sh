#!/usr/bin/env bash
# 首次真实数据烟测：清演示种子 + 拉前 N 只 A 股近 120 日（腾讯源，东财限流时可用）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LIMIT="${1:-5}"
[[ -x .venv/bin/python ]] || ./scripts/bootstrap_env.sh
exec .venv/bin/python main.py --init --limit "$LIMIT"
