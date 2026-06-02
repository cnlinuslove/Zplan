#!/usr/bin/env bash
# 续跑：规则分已完成后 → llm-top 300 → deep-top 30
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${ROOT}/../zplan-资讯/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/resume_llm_$(date +%Y%m%d_%H%M%S).log"
PY="${ROOT}/.venv/bin/python"

exec >>"$LOG" 2>&1
echo "=== resume llm $(date -Iseconds) ==="

cd "$ROOT"
export PICK_DEEPEN_WORKERS="${PICK_DEEPEN_WORKERS:-8}"
export PICK_DEEP_LLM_WORKERS="${PICK_DEEP_LLM_WORKERS:-2}"

"$PY" main.py llm-top --top "${LLM_TOP:-300}" --batch-size "${LLM_BATCH:-10}"

RUN_ID=$("$PY" -c "
from zplan_shared.pick_store import list_runs
for r in list_runs(limit=10):
    if r.get('run_kind') == 'llm_top300' and r.get('llm_enabled'):
        print(r['run_id'])
        break
else:
    raise SystemExit('未找到 llm_top300 运行')
")
echo "llm_top300 run_id=$RUN_ID"
"$PY" main.py deep-top --top "${DEEP_TOP:-30}" --from-run "$RUN_ID"
echo "=== done $(date -Iseconds) ==="
