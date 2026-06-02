#!/usr/bin/env bash
# AkShare 日线烟测（源由 AKSHARE_DAILY_PROVIDER 决定，默认东财）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -x .venv/bin/python ]] || ./scripts/bootstrap_env.sh

echo "== 配置 =="
.venv/bin/python -c "
from zplan_shared.data_sources import daily_provider_label, daily_source_tag
print('日线:', daily_provider_label(), daily_source_tag())
print('分时: 东财 akshare_em（固定）')
"

echo ""
echo "== 拉取 1 只 =="
.venv/bin/python main.py --a1 --limit 1 --realign-source

echo ""
echo "== 验收 =="
.venv/bin/python scripts/show_pull_results.py
