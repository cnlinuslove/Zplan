#!/usr/bin/env bash
# 守护：任务退出或卡住时自动续跑；数据齐全后降频、避免重复重试
set -uo pipefail
ROOT_MONO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG="${ROOT_MONO}/zplan-资讯/logs"
PRICE="${ROOT_MONO}/zplan-股价"
NEWS="${ROOT_MONO}/zplan-资讯"
PY_NEWS="${NEWS}/.venv/bin/python"
PY_PRICE="${PRICE}/.venv/bin/python"
INTERVAL_ACTIVE="${KEEP_ALIVE_INTERVAL_SEC:-900}"
INTERVAL_IDLE="${KEEP_ALIVE_INTERVAL_IDLE_SEC:-3600}"
RETRY_MIN_INTERVAL="${KEEP_ALIVE_RETRY_MIN_SEC:-3600}"
NEWS_MIN_INTERVAL="${KEEP_ALIVE_NEWS_MIN_SEC:-21600}"

mkdir -p "$LOG"
echo "[$(date '+%F %T')] keep_data_jobs_alive 启动 active=${INTERVAL_ACTIVE}s idle=${INTERVAL_IDLE}s" >> "${LOG}/keep_alive.log"

_running() { pgrep -f "$1" >/dev/null 2>&1; }

_pipeline_done() {
  grep -q 'pipeline_daily done' "${LOG}/pipeline_daily.log" 2>/dev/null
}

_progress() {
  local log="$1" pat="$2"
  [[ -f "$log" ]] || { echo "0"; return; }
  local n
  n=$(grep -oE "$pat" "$log" 2>/dev/null | tail -1 | grep -oE '[0-9]+' | head -1)
  echo "${n:-0}"
}

_log_recent() {
  local log="$1" max_age_sec="$2"
  [[ -f "$log" ]] || return 1
  local m now
  m=$(stat -f '%m' "$log" 2>/dev/null || echo 0)
  now=$(date +%s)
  (( now - m < max_age_sec ))
}

_start_bg() {
  local name="$1"
  shift
  echo "[$(date '+%F %T')] 启动 ${name}" >> "${LOG}/keep_alive.log"
  nohup caffeinate -dims bash -lc "$*" >> "${LOG}/${name}.log" 2>&1 &
}

_ensure_shared() {
  cd "$NEWS" && uv pip install -e "${ROOT_MONO}/zplan-共享" --python .venv/bin/python -q 2>/dev/null || true
  cd "$PRICE" && uv pip install -e "${ROOT_MONO}/zplan-共享" --python .venv/bin/python -q 2>/dev/null || true
}

_missing_daily_count() {
  "${PY_PRICE}" "${PRICE}/scripts/retry_missing_daily.py" --dry-run 2>/dev/null || echo 9999
}

while true; do
  _ensure_shared
  null_ind=11
  inc=0
  snap=0
  fin=0
  sleep_sec="${INTERVAL_ACTIVE}"

  # 日线流水线
  if ! _pipeline_done; then
    if ! _running "pipeline_daily|echo '=== incremental ==='"; then
      if ! _running "${PY_PRICE} main.py" || _running "retry_missing_daily|enrich_daily_fields"; then
        :
      elif ! _running "${PY_PRICE} main.py"; then
        inc=$(_progress "${LOG}/pipeline_daily.log" '\[[0-9]+/5517\]')
        if [[ "$inc" -lt 5517 ]]; then
          _start_bg "pipeline_daily" "
            cd '${PRICE}'
            echo '=== incremental (resume) ===' && ${PY_PRICE} main.py
            echo '=== retry_missing ===' && ${PY_PRICE} scripts/retry_missing_daily.py
            echo '=== enrich_daily ===' && ${PY_PRICE} scripts/enrich_daily_fields.py
            echo '=== pipeline_daily done ==='
          "
        fi
      fi
    fi
  else
    miss=$(_missing_daily_count)
    if [[ "${miss}" -eq 0 ]]; then
      grep -q 'retry_missing done' "${LOG}/pipeline_daily.log" 2>/dev/null || \
        echo "[$(date '+%F %T')] retry_missing done (0 missing)" >> "${LOG}/pipeline_daily.log"
    elif ! _running "retry_missing_daily"; then
      if ! grep -q 'retry_missing done' "${LOG}/pipeline_daily.log" 2>/dev/null || [[ "${miss}" -gt 2 ]]; then
        if ! _log_recent "${LOG}/retry_only.log" "${RETRY_MIN_INTERVAL}"; then
          _start_bg "retry_only" "
            cd '${PRICE}' && ${PY_PRICE} scripts/retry_missing_daily.py
            echo \"[$(date '+%F %T')] retry_missing done (miss=${miss})\"
          "
        fi
      fi
    fi
    if ! _running "enrich_daily_fields" && ! grep -q 'enrich_daily done' "${LOG}/pipeline_daily.log" 2>/dev/null; then
      if ! _log_recent "${LOG}/enrich_only.log" 7200; then
        _start_bg "enrich_only" "cd '${PRICE}' && ${PY_PRICE} scripts/enrich_daily_fields.py && echo enrich_daily done"
      fi
    fi
  fi

  # 元数据：仅大量缺失时重试
  null_ind=$("${PY_NEWS}" -c "
from zplan_shared.models import init_db, SessionLocal
from sqlalchemy import text
init_db()
with SessionLocal() as s:
    print(s.execute(text('SELECT COUNT(*) FROM stock_list WHERE industry IS NULL')).scalar_one())
" 2>/dev/null || echo 11)
  if [[ "${null_ind}" -gt 50 ]] && ! _running "p0_backfill.py --meta"; then
    _start_bg "meta_retry" "cd '${NEWS}' && ${PY_NEWS} scripts/p0_backfill.py --meta-only || true"
  fi

  # 截面 / 财务：串行，避免与日线/东财抢带宽（各任务日志 2h 内有更新则不再拉起）
  snap=$(_progress "${LOG}/snapshot.log" '\[[0-9]+/5517\]')
  fin=$(_progress "${LOG}/financial.log" 'financial \[[0-9]+/5517\]')
  if [[ "$snap" -lt 5517 ]] && ! _running "main.py --snapshot" && ! _running "main.py --financial"; then
    if ! _log_recent "${LOG}/snapshot.log" 7200; then
      _start_bg "snapshot" "cd '${PRICE}' && ${PY_PRICE} main.py --snapshot"
    fi
  elif [[ "$fin" -lt 5517 ]] && ! _running "main.py --financial" && ! _running "main.py --snapshot"; then
    if ! _log_recent "${LOG}/financial.log" 7200; then
      _start_bg "financial" "cd '${PRICE}' && ${PY_PRICE} main.py --financial"
    fi
  fi

  # 资讯：LaunchAgent 已跑则跳过；否则按间隔补 link-only（比全量 ETL 快）
  last_news=0
  if [[ -f "${LOG}/cron_daily_news.log" ]]; then
    last_news=$(stat -f '%m' "${LOG}/cron_daily_news.log" 2>/dev/null || echo 0)
  elif [[ -f "${LOG}/daily_news.log" ]]; then
    last_news=$(stat -f '%m' "${LOG}/daily_news.log" 2>/dev/null || echo 0)
  fi
  now=$(date +%s)
  if (( now - last_news > NEWS_MIN_INTERVAL )) && ! _running "daily_news_job.py"; then
    _start_bg "daily_news" "
      cd '${NEWS}' && DAILY_NEWS_RUN_ETL=false DAILY_NEWS_LINK_RELINK=false \\
        ${PY_NEWS} daily_news_job.py
    "
  fi

  inc=$(_progress "${LOG}/pipeline_daily.log" '\[[0-9]+/5517\]')
  if _pipeline_done && [[ "$snap" -ge 5517 ]] && [[ "$fin" -ge 5517 ]] && [[ "${null_ind}" -le 20 ]]; then
    sleep_sec="${INTERVAL_IDLE}"
  fi
  echo "[$(date '+%F %T')] inc=${inc}/5517 snap=${snap}/5517 fin=${fin}/5517 meta_null=${null_ind} sleep=${sleep_sec}s" >> "${LOG}/keep_alive.log"

  sleep "$sleep_sec"
done
