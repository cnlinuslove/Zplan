#!/usr/bin/env bash
# 全流程后台：并行补齐截面 → 规则分 → Top300 LLM → Top30 深度
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT}/../zplan-资讯/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="${LOG_DIR}/pipeline_full_${STAMP}.log"

PY="${ROOT}/.venv/bin/python"
PRICE_PY="${ROOT}/../zplan-股价/.venv/bin/python"
[[ -x "$PY" ]] || bash "${ROOT}/scripts/bootstrap_env.sh"

exec >>"$LOG" 2>&1
echo "=== pipeline-full 开始 $(date -Iseconds) ==="
echo "日志: $LOG"

# 多进程补齐（默认 4 进程；AkShare/mini_racer 不能多线程）
cd "${ROOT}/../zplan-股价"
"$PRICE_PY" main.py --catch-up-panel --workers "${CATCHUP_WORKERS:-4}"

cd "$ROOT"
echo "--- daily_features 物化 ---"
"$PY" -c "from zplan_shared.etl_daily_features import run_daily_features_update; print(run_daily_features_update())"

export PICK_DEEPEN_WORKERS="${PICK_DEEPEN_WORKERS:-8}"
export PICK_DEEP_LLM_WORKERS="${PICK_DEEP_LLM_WORKERS:-2}"
echo "--- init-rule + llm-top + deep-top ---"
"$PY" main.py pipeline-full --top "${LLM_TOP:-300}" --deep-top "${DEEP_TOP:-30}" \
  --batch-size "${LLM_BATCH:-15}" \
  --catch-up-workers "${CATCHUP_WORKERS:-6}" \
  --deepen-workers "${PICK_DEEPEN_WORKERS}" \
  --deep-llm-workers "${PICK_DEEP_LLM_WORKERS}"

echo "=== pipeline-full 结束 $(date -Iseconds) ==="
