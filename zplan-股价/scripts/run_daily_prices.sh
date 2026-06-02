#!/usr/bin/env bash
# 每日行情更新（供 LaunchAgent / cron）：日线增量 → 补缺 → 衍生字段 → 估值截面
set -euo pipefail
PRICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONO_ROOT="$(cd "${PRICE_ROOT}/.." && pwd)"
NEWS_ROOT="${ZPLAN_ROOT:-${MONO_ROOT}/zplan-资讯}"
cd "${PRICE_ROOT}"

PY="${PRICE_ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "缺少 .venv，请先: ./scripts/bootstrap_env.sh" >&2
  exit 1
fi

if [[ -f "${NEWS_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${NEWS_ROOT}/.env"
  set +a
fi
export ZPLAN_ROOT="${NEWS_ROOT}"
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

LOG_DIR="${NEWS_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/cron_daily_prices.log"
LOCK="${LOG_DIR}/daily_prices_job.lock"

_run() {
  echo "[$(date '+%F %T')] $*"
  "$@"
}

if [[ -f "$LOCK" ]]; then
  old_pid="$(cat "$LOCK" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "[$(date '+%F %T')] 已有任务 pid=${old_pid}，跳过" >>"$LOG"
    exit 0
  fi
  rm -f "$LOCK"
fi
echo $$ >"$LOCK"
trap 'rm -f "$LOCK"' EXIT

{
  echo "======== $(date '+%Y-%m-%d %H:%M:%S %z') ========"
  # 日线增量 + 自动截面补齐（缺上一交易日/漏跑时由 DAILY_AUTO_CATCHUP_PANEL 恢复）
  _run "$PY" main.py
  if ! _run "$PY" scripts/retry_missing_daily.py; then
    echo "[$(date '+%F %T')] WARN retry_missing_daily 有失败，继续后续步骤"
  fi
  _run "$PY" scripts/enrich_daily_fields.py
  _run "$PY" scripts/materialize_daily_features.py
  if [[ "${DAILY_PRICES_RUN_SNAPSHOT:-true}" == "true" ]]; then
    _run "$PY" main.py --snapshot
  fi
  # 财报指标：默认每周五维护一轮（季报源更新慢，不必每日全量）
  _run_financial=false
  if [[ "${DAILY_PRICES_RUN_FINANCIAL:-weekly}" == "true" ]]; then
    _run_financial=true
  elif [[ "${DAILY_PRICES_RUN_FINANCIAL:-weekly}" == "weekly" && "$(date +%u)" == "5" ]]; then
    _run_financial=true
  fi
  if [[ "$_run_financial" == "true" ]]; then
    _run "$PY" main.py --financial
  fi
  # stock_list 元数据：仅当仍有大量空缺时尝试（东财失败则跳过）
  null_ind=$("$PY" -c "
from sqlalchemy import text
from zplan_shared.models import init_db, SessionLocal
init_db()
with SessionLocal() as s:
    print(s.execute(text('SELECT COUNT(*) FROM stock_list WHERE industry IS NULL')).scalar_one())
" 2>/dev/null || echo 0)
  if [[ "${null_ind:-0}" -gt 50 ]]; then
    NEWS_PY="${NEWS_ROOT}/.venv/bin/python"
    if [[ -x "$NEWS_PY" ]]; then
      _run "$NEWS_PY" "${NEWS_ROOT}/scripts/p0_backfill.py" --meta-only || true
    fi
  fi
  if [[ "${DAILY_PRICES_RUN_INTRADAY:-false}" == "true" ]]; then
    _run "$PY" main.py --a1 --limit "${DAILY_PRICES_INTRADAY_LIMIT:-50}"
  fi
  echo "[$(date '+%F %T')] daily_prices 完成"
  if [[ "${DAILY_PRICES_TRIGGER_PICK:-true}" == "true" ]]; then
    echo "[$(date '+%F %T')] 触发选股 pipeline"
    _run "$PY" scripts/run_pick_after_prices.py --notify ${PICK_PIPELINE_ARGS:-}
  fi
} >>"$LOG" 2>&1
