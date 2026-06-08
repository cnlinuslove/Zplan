#!/bin/bash
# Z-Plan 全量数据管道 — 所有板块按依赖顺序执行，单步失败不中断
# 用法:
#   ./run_full_pipeline.sh                     # 全部（含季报 + 规则打分）
#   ./run_full_pipeline.sh --status            # 仅打印当前数据状态
#   ./run_full_pipeline.sh --lite              # 日常盘后（跳过季报 + 规则打分）
#   ./run_full_pipeline.sh --lite --dry-run    # 看要跑哪些步骤但不执行
set -euo pipefail

MONO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PRICE_ROOT="$MONO_ROOT/zplan-股价"
NEWS_ROOT="$MONO_ROOT/zplan-资讯"
PICK_ROOT="$MONO_ROOT/zplan-选股"
export ZPLAN_ROOT="${ZPLAN_ROOT:-$NEWS_ROOT}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
# 东财 push2 等子域名直连不通，统一走代理；日线用新浪不受影响
export AKSHARE_EASTMONEY_DIRECT=false

LOG_DIR="$NEWS_ROOT/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/full_pipeline_$(date +%Y%m%d_%H%M).log"

PASS="OK"; FAIL="FAIL"; SKIP="SKIP"
STEPS=()

add_step()  { STEPS+=("$1|$2|$(date +%H:%M:%S)"); }

# ═══ 状态查询 ═══
if [[ "${1:-}" == "--status" ]]; then
  echo ""
  echo "  Z-Plan 数据板块状态  $(date +%Y-%m-%d)"
  echo "  ─────────────────────────────────────────────"
  cd "$PRICE_ROOT"
  "$PRICE_ROOT/.venv/bin/python" << 'PYEOF'
from zplan_shared.models import init_db, SessionLocal
from sqlalchemy import text
init_db(); s = SessionLocal()

checks = [
    ("股价日线",      "daily_prices",         "trade_date"),
    ("衍生指标",      "daily_features",       "trade_date"),
    ("估值截面",      "daily_snapshot",       "trade_date"),
    ("筹码峰",        "daily_chip",           "trade_date"),
    ("季报财务",      "financial_indicators", "report_date"),
]
for label, table, col in checks:
    try:
        r = s.execute(text(f"SELECT COUNT(*), MAX({col}) FROM {table}")).fetchone()
        cnt, latest = r[0], r[1]
        print(f"  {label:<12} {cnt:>8,} 行  最新 {latest}")
    except Exception as e:
        print(f"  {label:<12}         --  错误: {e}")

try:
    r = s.execute(text("SELECT COUNT(*), MAX(published_at_utc) FROM financial_alerts")).fetchone()
    print(f"  {'东财快讯':<12} {r[0]:>8,} 行  最新 {r[1]}")
except: print(f"  {'东财快讯':<12}         --  无数据")

try:
    r = s.execute(text("SELECT COUNT(*) FROM news_stock_link")).fetchone()
    print(f"  {'新闻关联':<12} {r[0]:>8,} 条")
except: print(f"  {'新闻关联':<12}         --  无数据")

try:
    r = s.execute(text("SELECT COUNT(*), MAX(trade_date_as_of) FROM stock_rule_scores")).fetchone()
    print(f"  {'规则打分':<12} {r[0]:>8,} 行  最新 {r[1]}")
except: print(f"  {'规则打分':<12}         --  无数据")

try:
    r = s.execute(text("SELECT COUNT(*), MAX(updated_at) FROM concept_product_cache")).fetchone()
    print(f"  {'概念产品':<12} {r[0]:>8,} 行  最新 {r[1]}")
except: print(f"  {'概念产品':<12}         --  无数据")

s.close()
PYEOF
  exit 0
fi

# ═══ 参数解析 ═══
LITE=false; DRY=false
for a in "$@"; do
  case "$a" in --lite|--skip-financial) LITE=true ;; --dry-run) DRY=true ;; esac
done

if $DRY; then
  echo "管道步骤预览（--lite=$LITE）："
  echo "  1. 股价日线 + 补缺"
  echo "  2. 衍生-回填 + 衍生-物化"
  echo "  3. 估值截面"
  if $LITE; then echo "  4. 季报财务 → 跳过"; else echo "  4. 季报财务"; fi
  echo "  5. 资讯同步"
  if $LITE; then echo "  6. 规则打分 → 跳过"; else echo "  6. 规则打分"; fi
  exit 0
fi

# ═══ 执行管道 ═══
exec >>"$LOG" 2>&1

echo "══════════════════════════════════════════════════"
echo "  Z-Plan 全量数据管道  $(date)"
echo "  ZPLAN_ROOT=$ZPLAN_ROOT  lite=$LITE"
echo "══════════════════════════════════════════════════"

# 管道锁：防止并发跑，且僵尸锁不影响下次运行
LOCK_FILE="$LOG_DIR/.pipeline_running"
if [ -f "$LOCK_FILE" ]; then
  echo "⚠️ 检测到残留锁文件（上次可能异常退出），继续执行"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f '$LOCK_FILE'" EXIT

run_step() {
  local label="$1" cwd="$2"; shift 2
  echo ""
  echo "=== [$label] $(date +%H:%M:%S) ==="
  if cd "$cwd" 2>/dev/null && "$@" >>"$LOG" 2>&1; then
    echo "  ✅ $label 完成"
    add_step "$label" "OK"
  else
    echo "  ❌ $label 失败（code=$?），继续后续步骤"
    add_step "$label" "FAIL"
  fi
}

# 1. 股价日线（并行补齐当天截面，预计 10-15min）
TODAY=$(date +%Y-%m-%d)
run_step "股价日线" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --catch-up-panel --workers 6 --panel-date "$TODAY"

# 2. 衍生指标
run_step "衍生-回填" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" scripts/enrich_daily_fields.py
run_step "衍生-物化" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" scripts/materialize_daily_features.py

# 3. 估值截面
run_step "估值截面" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --snapshot

# 3b. 筹码峰 ETL（增量模式，首次约 40min，后续 ~5min）
run_step "筹码峰ETL" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --chip

# 4. 季报财务
if $LITE; then
  echo ""; echo "=== [季报财务] $(date +%H:%M:%S) ==="
  echo "  ⏭️ 季报财务 跳过（--lite）"
  add_step "季报财务" "SKIP"
else
  run_step "季报财务" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --financial
fi

# 5. 资讯
run_step "资讯同步" "$NEWS_ROOT" "$NEWS_ROOT/.venv/bin/python" -m sentiment_etl.runner

# 6. 规则打分
if $LITE; then
  echo ""; echo "=== [规则打分] $(date +%H:%M:%S) ==="
  echo "  ⏭️ 规则打分 跳过（--lite）"
  add_step "规则打分" "SKIP"
else
  run_step "规则打分" "$PICK_ROOT" "$PICK_ROOT/.venv/bin/python" main.py init-rule
fi

# 汇总
echo ""
echo "──────────────────────────────────────────────────"
echo "  管道完成 $(date)"
echo "──────────────────────────────────────────────────"
ok_n=0; fail_n=0; skip_n=0
for s in "${STEPS[@]}"; do
  IFS='|' read -r label result time <<< "$s"
  case "$result" in
    OK)   echo "  ✅ $label ($time)"; ((ok_n++)) ;;
    FAIL) echo "  ❌ $label ($time)"; ((fail_n++)) ;;
    SKIP) echo "  ⏭️ $label ($time)"; ((skip_n++)) ;;
  esac
done
echo "  共 ${#STEPS[@]} 步  OK=$ok_n  FAIL=$fail_n  SKIP=$skip_n"
echo "  日志: $LOG"

# 企微播报
echo ""; echo "=== [企微播报] $(date +%H:%M:%S) ==="
NOTIFY_FLAGS=""
[ "$fail_n" -gt 0 ] && NOTIFY_FLAGS="--alert"
if "$NEWS_ROOT/.venv/bin/python" "$PRICE_ROOT/scripts/pipeline_notify.py" $NOTIFY_FLAGS "$LOG" 2>/dev/null; then
    echo "  ✅ 播报完成"
else
    echo "  ⚠️ 播报失败（可能未配置 WECHAT_PUSH_WEBHOOK）"
fi

# 盘后 TOP10 复盘
echo ""; echo "=== [TOP10复盘] $(date +%H:%M:%S) ==="
if "$NEWS_ROOT/.venv/bin/python" "$NEWS_ROOT/scripts/evening_review.py" 2>/dev/null; then
    echo "  ✅ TOP10 复盘完成"
else
    echo "  ⚠️ TOP10 复盘失败（可能无选股数据或未配置 WECHAT_PUSH_WEBHOOK）"
fi

find "$LOG_DIR" -name "full_pipeline_*.log" -mtime +30 -delete 2>/dev/null || true
[ "$fail_n" -eq 0 ] || exit 1
