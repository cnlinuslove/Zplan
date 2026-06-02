#!/usr/bin/env bash
# 一键拉起：日线增量 → 补缺 → 衍生字段 → 分时；并行：资讯、元数据、截面、财务
set -euo pipefail
ROOT_MONO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG="${ROOT_MONO}/zplan-资讯/logs"
mkdir -p "$LOG"
STAMP="$(date +%Y%m%d_%H%M%S)"
echo "[${STAMP}] run_all_data_jobs 启动" >> "${LOG}/run_all_data_jobs.log"

_run_bg() {
  local name="$1"
  shift
  nohup caffeinate -dims bash -lc "$*" >> "${LOG}/${name}.log" 2>&1 &
  echo "$! ${name}" >> "${LOG}/run_all_data_jobs.log"
}

# 并行：资讯 ETL + 新闻关联
_run_bg "daily_news" "cd '${ROOT_MONO}/zplan-资讯' && .venv/bin/python daily_news_job.py"

# 并行：stock_list 元数据（先交易所，成功后再尝试东财板块）
_run_bg "meta_exchange" "cd '${ROOT_MONO}/zplan-资讯' && .venv/bin/python scripts/p0_backfill.py --meta-only --exchange-meta-only && .venv/bin/python scripts/p0_backfill.py --meta-only || true"

# 并行：估值截面、财务指标
_run_bg "snapshot" "cd '${ROOT_MONO}/zplan-股价' && .venv/bin/python main.py --snapshot"
_run_bg "financial" "cd '${ROOT_MONO}/zplan-股价' && .venv/bin/python main.py --financial"

# 串行：日线增量 → 补缺 → pct_chg 等衍生字段
_run_bg "pipeline_daily" "
  cd '${ROOT_MONO}/zplan-股价'
  echo '=== incremental ===' && .venv/bin/python main.py
  echo '=== retry_missing ===' && .venv/bin/python scripts/retry_missing_daily.py
  echo '=== enrich_daily ===' && .venv/bin/python scripts/enrich_daily_fields.py
  echo '=== pipeline_daily done ==='
"

# 并行：全市场分时（Parquet，依赖东财；与日线争抢带宽但可同时进行）
_run_bg "intraday" "
  cd '${ROOT_MONO}/zplan-股价'
  .venv/bin/python -c \"
from sqlalchemy import select
from zplan_shared.etl_intraday import sync_intraday_universe
from zplan_shared.models import SessionLocal, StockList, init_db
init_db()
with SessionLocal() as s:
    symbols = [r[0] for r in s.execute(select(StockList.ts_code)).all()]
print('intraday symbols', len(symbols))
stats = sync_intraday_universe(symbols)
print('intraday done', stats)
\"
"

echo "已后台启动，日志目录: ${LOG}"
echo "  tail -f ${LOG}/pipeline_daily.log"
echo "  tail -f ${LOG}/daily_news.log"
echo "  tail -f ${LOG}/meta_exchange.log"
