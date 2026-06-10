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
bash "${ROOT}/zplan-选股/scripts/smoke.sh"
"${ROOT}/zplan-回测/.venv/bin/python" "${ROOT}/zplan-回测/main.py" smoke --code 000001
echo ">> 安装每日定时（资讯 + 股价 + 执行层，macOS LaunchAgent）"
chmod +x "${ROOT}/zplan-资讯/scripts/"*.sh "${ROOT}/zplan-股价/scripts/"*.sh "${ROOT}/zplan-选股/scripts/"*.sh 2>/dev/null || true
bash "${ROOT}/zplan-资讯/scripts/install_daily_news_launchagent.sh" || true
bash "${ROOT}/zplan-股价/scripts/install_daily_prices_launchagent.sh" || true
bash "${ROOT}/zplan-选股/scripts/install_execution_launchagents.sh" || true
echo ""
echo "完成。正式行情： cd zplan-股价 && .venv/bin/python main.py"
echo "每日自动更新日志： zplan-资讯/logs/cron_daily_prices.log / cron_daily_news.log"
