#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────
# 港股 (HKEX) 日线增量 ETL
#
# 用法:
#   ./scripts/run_hk_daily_prices.sh               # 全量增量
#   ./scripts/run_hk_daily_prices.sh --a1          # Phase A.1（日线 + 可选分时）
#   ./scripts/run_hk_daily_prices.sh --limit 10    # 仅前 10 只（调试）
#   ./scripts/run_hk_daily_prices.sh --snapshot    # 估值截面（需网络 + 启用开关）
#
# 环境变量（可选）:
#   HK_DAILY_BOOTSTRAP_CALENDAR_DAYS=400  新标回溯天数
#   HK_SNAPSHOT_PER_SYMBOL_ENABLED=true   启用估值截面 ETL
#   AKSHARE_EASTMONEY_DIRECT=false        强制东财走代理（境外网络通常需要）
# ────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(dirname "$SCRIPT_DIR")"

# 确保环境
if [ ! -d "$AGENT_DIR/.venv" ]; then
    echo "❌ .venv 不存在，请先运行 scripts/bootstrap_env.sh" >&2
    exit 1
fi

# 东财 CDN 在某些网络下需走代理
export AKSHARE_EASTMONEY_DIRECT="${AKSHARE_EASTMONEY_DIRECT:-false}"

echo "📊 港股 (HKEX) 日线 ETL"
echo "   数据根: ${ZPLAN_ROOT:-(默认)}"
echo "   东财直连: ${AKSHARE_EASTMONEY_DIRECT}"

exec "$AGENT_DIR/.venv/bin/python" -u main.py --market hk "$@"
