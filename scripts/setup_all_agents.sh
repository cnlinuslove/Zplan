#!/usr/bin/env bash
# 一键：兼容软链 → 各 Agent 装依赖 → 迁移库结构 →（可选）演示行情
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ln -sfn "zplan-共享" zplan-shared 2>/dev/null || true
ln -sfn "zplan-资讯" zplan 2>/dev/null || true
ln -sfn "zplan-股价" zplan-trend 2>/dev/null || true
ln -sfn "zplan-选股" zplan-pick 2>/dev/null || true
ln -sfn "zplan-回测" zplan-backtest 2>/dev/null || true

for dir in zplan-资讯 zplan-股价 zplan-选股 zplan-回测; do
  echo ">> bootstrap ${dir}"
  bash "${ROOT}/${dir}/scripts/bootstrap_env.sh"
done

PY="${ROOT}/zplan-资讯/.venv/bin/python"
echo ">> init_db + Phase A 迁移"
"$PY" -c "from zplan_shared.models import init_db; init_db(); print('ok', __import__('zplan_shared.config', fromlist=['DB_URL']).DB_URL)"

if [[ "${1:-}" != "--skip-demo" ]]; then
  echo ">> 写入演示行情（AkShare 不可达时便于联调）"
  "$PY" "${ROOT}/zplan-共享/scripts/seed_demo_prices.py"
fi

echo ">> 选股 / 回测 烟测"
"${ROOT}/zplan-选股/.venv/bin/python" "${ROOT}/zplan-选股/main.py" --top 3
"${ROOT}/zplan-回测/.venv/bin/python" "${ROOT}/zplan-回测/main.py" --code 000001
echo ""
echo "完成。正式行情请在网络可用时： cd zplan-股价 && .venv/bin/python main.py"
