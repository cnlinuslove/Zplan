#!/bin/bash
# Z-Plan 资讯 ETL 自动运行脚本（东财快讯 + 北向 + 两融 + 指数换手率 + Google RSS）
# 由 launchd 定时触发；也可手动执行
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ZPLAN_ROOT="$ROOT"

cd "$ROOT"

# 日志文件
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/sentiment_etl_$(date +%Y%m%d_%H%M%S).log"

exec >>"$LOG_FILE" 2>&1

echo "=== Z-Plan 资讯 ETL 开始 $(date) ==="

"$ROOT/.venv/bin/python" -m sentiment_etl.runner

echo "=== Z-Plan 资讯 ETL 完成 $(date) ==="

# 清理 30 天前的日志
find "$LOG_DIR" -name "sentiment_etl_*.log" -mtime +30 -delete 2>/dev/null || true
