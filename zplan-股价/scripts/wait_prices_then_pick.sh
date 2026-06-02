#!/usr/bin/env bash
# 等待 cron_daily_prices 跑完后触发选股 pipeline（一次性守护）
set -euo pipefail
MONO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NEWS_ROOT="${ZPLAN_ROOT:-${MONO_ROOT}/zplan-资讯}"
PRICE_ROOT="${MONO_ROOT}/zplan-股价"
LOG="${NEWS_ROOT}/logs/cron_daily_prices.log"
OUT="${NEWS_ROOT}/logs/wait_prices_then_pick.log"
INTERVAL="${WAIT_PICK_INTERVAL_SEC:-90}"
MAX_WAIT_HOURS="${WAIT_PICK_MAX_HOURS:-8}"

mkdir -p "${NEWS_ROOT}/logs"
echo "[$(date '+%F %T')] wait_prices_then_pick 启动" >>"$OUT"

deadline=$(( $(date +%s) + MAX_WAIT_HOURS * 3600 ))
while [[ $(date +%s) -lt $deadline ]]; do
  if [[ -f "$LOG" ]] && grep -q 'daily_prices 完成' "$LOG" 2>/dev/null; then
    echo "[$(date '+%F %T')] 检测到 daily_prices 完成" >>"$OUT"
    break
  fi
  if ! pgrep -f 'run_daily_prices\.sh' >/dev/null 2>&1 && ! pgrep -f 'zplan-股价/.venv/bin/python main\.py' >/dev/null 2>&1; then
    if [[ -f "$LOG" ]] && grep -q 'daily_prices 完成' "$LOG" 2>/dev/null; then
      echo "[$(date '+%F %T')] 进程已结束且日志含完成标记" >>"$OUT"
      break
    fi
    echo "[$(date '+%F %T')] 股价任务已退出但未看到完成标记，继续等…" >>"$OUT"
  fi
  prog=$(grep -oE '\[[0-9]+/5517\]' "$LOG" 2>/dev/null | tail -1 || true)
  echo "[$(date '+%F %T')] 等待中… ${prog:-无进度}" >>"$OUT"
  sleep "$INTERVAL"
done

if ! grep -q 'daily_prices 完成' "$LOG" 2>/dev/null; then
  echo "[$(date '+%F %T')] 超时未等到 daily_prices 完成" >>"$OUT"
  exit 1
fi

chmod +x "${PRICE_ROOT}/scripts/run_pick_after_prices.py"
"${PRICE_ROOT}/.venv/bin/python" "${PRICE_ROOT}/scripts/run_pick_after_prices.py" --notify "$@" >>"$OUT" 2>&1
echo "[$(date '+%F %T')] pick 触发结束 exit=$?" >>"$OUT"
