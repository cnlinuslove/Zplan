#!/bin/bash
# Z-Plan 全数据管道：股价 → 衍生指标 → 估值截面 → 季报财务 → 规则打分
# 用法：./run_data_pipeline.sh [--lite] [--skip-pick]
#   --lite      跳过季报（日常盘后快速版）
#   --skip-pick 跳过规则打分（仅数据更新）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ZPLAN_ROOT="${ZPLAN_ROOT:-/Users/richard/my_stock_ai/zplan-资讯}"
export PATH="$ROOT/.venv/bin:$PATH"

LOG_DIR="$ZPLAN_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/data_pipeline_$(date +%Y%m%d_%H%M%S).log"
exec >>"$LOG" 2>&1

echo "══════════════════════════════════════════════"
echo "  Z-Plan 数据管道  $(date)"
echo "  ZPLAN_ROOT=$ZPLAN_ROOT"
echo "══════════════════════════════════════════════"

LITE=false; SKIP_PICK=false
for arg in "$@"; do
    case "$arg" in --lite) LITE=true ;; --skip-pick) SKIP_PICK=true ;; esac
done

# ── Step 1: 股价日线 ────────────────────────────────────
echo ""
echo "=== [1/6] 股价日线补齐 ==="
cd "$ROOT"
"$ROOT/.venv/bin/python" main.py --catch-up-panel --workers 8
echo "  ✅ 日线补齐完成"

# ── Step 2: 衍生指标 ────────────────────────────────────
echo ""
echo "=== [2/6] 衍生指标回填 ==="
"$ROOT/.venv/bin/python" main.py --enrich-daily
echo "  ✅ 衍生指标完成"

# ── Step 3: 估值截面 ─────────────────────────────────────
echo ""
echo "=== [3/6] 估值截面 ==="
"$ROOT/.venv/bin/python" main.py --snapshot
echo "  ✅ 估值截面完成"

# ── Step 4: 季报财务 ─────────────────────────────────────
if $LITE; then
    echo ""
    echo "=== [4/6] 季报财务 · 跳过（--lite）==="
else
    echo ""
    echo "=== [4/6] 季报财务 ==="
    "$ROOT/.venv/bin/python" main.py --financial
    echo "  ✅ 季报财务完成"
fi

# ── Step 5: 资讯 ETL ────────────────────────────────────
echo ""
echo "=== [5/6] 资讯同步（东财快讯 + 北向 + 两融 + 指数 + 新闻关联）==="
cd "$ZPLAN_ROOT"
"$ZPLAN_ROOT/.venv/bin/python" -m sentiment_etl.runner
echo "  ✅ 资讯同步完成"

# ── Step 6: 规则打分 ────────────────────────────────────
if $SKIP_PICK; then
    echo ""
    echo "=== [6/6] 规则打分 · 跳过（--skip-pick）==="
else
    echo ""
    echo "=== [6/6] 规则引擎全市场打分 ==="
    cd "$ROOT/../zplan-选股"
    "$(dirname "$ROOT")/zplan-选股/.venv/bin/python" main.py init-rule
    echo "  ✅ 规则打分完成"
fi

echo ""
echo "══════════════════════════════════════════════"
echo "  管道完成 $(date)"
echo "  日志: $LOG"
echo "══════════════════════════════════════════════"

# 清理 30 天前日志
find "$LOG_DIR" -name "data_pipeline_*.log" -mtime +30 -delete 2>/dev/null || true
