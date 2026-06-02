#!/usr/bin/env bash
# 初始化 stock_list：行业（东财现货）+ 上市日（沪深京交易所）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || { echo "请先 ./scripts/bootstrap_env.sh" >&2; exit 1; }
[[ -f .env ]] && set -a && source .env && set +a
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
# 东财域名直连，避免 Clash 把 push2 搞挂
export AKSHARE_EASTMONEY_DIRECT=true
export AKSHARE_USE_SYSTEM_PROXY=false
export AKSHARE_EASTMONEY_PROXY_FALLBACK=false
export STOCK_META_FLUSH_EACH_PHASE=1
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY || true
# 东财 push2 不可用时仍先写沪深京；东财恢复后去掉 SKIP_EM 再跑一轮
if [[ "${STOCK_META_SKIP_EM:-}" == "" ]]; then
  if ! curl -fsS --max-time 8 -o /dev/null \
    "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f12&fs=m:0+t:6&fields=f12,f100" 2>/dev/null; then
    export STOCK_META_SKIP_EM=1
    export STOCK_META_SH_INDIVIDUAL_EM=0
    echo "[init_stock_list_meta] 东财 push2 暂不可达：仅交易所+深交所 xlsx（恢复后去掉 SKIP_EM 再跑）"
  fi
fi
exec "$PY" scripts/p0_backfill.py --meta-only
