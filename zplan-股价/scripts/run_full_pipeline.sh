#!/bin/bash
# Z-Plan 全量数据管道 — 所有板块按依赖顺序执行，单步失败不中断
# 用法:
#   ./run_full_pipeline.sh                     # 全部（含季报 + 规则打分）
#   ./run_full_pipeline.sh --status            # 仅打印当前数据状态
#   ./run_full_pipeline.sh --lite              # 日常盘后（跳过季报 + 规则打分）
#   ./run_full_pipeline.sh --lite --dry-run    # 看要跑哪些步骤但不执行
set -u  # 未定义变量报错；单步失败不退出（由 run_step/retry_step 自行处理）

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

try:
    r = s.execute(text("SELECT COUNT(*), MAX(trade_date) FROM daily_index")).fetchone()
    print(f"  {'指数日线':<12} {r[0]:>8,} 行  最新 {r[1]}")
except: print(f"  {'指数日线':<12}         --  无数据")

try:
    r = s.execute(text("SELECT COUNT(*), MAX(as_of_date) FROM market_forecasts")).fetchone()
    print(f"  {'大盘预测':<12} {r[0]:>8,} 行  最新 {r[1]}")
except: print(f"  {'大盘预测':<12}         --  无数据")

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
  echo "管道步骤预览（--lite=${LITE}）："
  echo "  ═══ Phase 1: 数据底座（串行，失败熔断）═══"
  echo "  1a. 股价日线 [retry x2]"
  echo "  1b. 衍生-回填 [retry x2，需 1a]"
  echo "  1c. 衍生-物化 [retry x2，需 1b]"
  echo "  ═══ Phase 2: 辅助数据（并行，需 1a）═══"
  echo "  2a. 估值截面"
  echo "  2b. 筹码峰 ETL"
  echo "  2c. 指数日线"
  echo "  ═══ Phase 3: 衍生计算（串行）═══"
  if $LITE; then echo "  3a. 季报财务 → 跳过"; else echo "  3a. 季报财务"; fi
  echo "  3b. 资讯同步"
  echo "  3c. 规则打分 [需 1c]"
  echo "  ═══ Phase 4: LLM（需 3c）═══"
  echo "  4a. LLM简评 TOP300（~10min，~¥0.30）"
  echo "  ═══ Phase 5: 推送（数据播报永远发）═══"
  echo "  5a. 数据状态播报"
  echo "  5b. 综合复盘 [需 1a+1c]"
  echo "  5c. 大盘预测 [需 1a+1c]"
  echo "  5d. TOP10 选股+操作建议 [需 1a+1c]"
  exit 0
fi

# ═══ 交易日检查 ═══
is_trading_day() {
  # 周末跳过
  local dow=$(date +%u)  # 1=Mon ... 7=Sun
  [ "$dow" -ge 6 ] && return 1
  # 已知节假日（2026 年中国）
  local md=$(date +%m-%d)
  case "$md" in
    01-01|01-02|01-03|01-04|01-05|01-06|01-07) return 1 ;;  # 元旦+春节前
    02-16|02-17|02-18|02-19|02-20|02-21) return 1 ;;        # 春节
    04-06|04-07) return 1 ;;                                  # 清明
    05-01|05-02|05-03|05-04|05-05) return 1 ;;                # 劳动节
    06-19|06-20|06-21) return 1 ;;                             # 端午
    10-01|10-02|10-03|10-04|10-05|10-06|10-07|10-08) return 1 ;; # 国庆+中秋
  esac
  return 0
}

# ═══ 执行管道 ═══
exec >>"$LOG" 2>&1

echo "══════════════════════════════════════════════════"
echo "  Z-Plan 全量数据管道  $(date)"
echo "  ZPLAN_ROOT=$ZPLAN_ROOT  lite=${LITE}"
echo "══════════════════════════════════════════════════"

if ! is_trading_day; then
  echo "⏭️ 今日非交易日，管道退出"
  # 不发通知，静默退出（但 trap EXIT 仍会清理锁文件）
  exit 0
fi

# 管道锁：防止并发跑，且僵尸锁不影响下次运行
LOCK_FILE="$LOG_DIR/.pipeline_running"
if [ -f "$LOCK_FILE" ]; then
  OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || true)
  if [ -n "${OLD_PID:-}" ] && ! kill -0 "$OLD_PID" 2>/dev/null; then
    echo "⚠️ 检测到残留锁文件（PID $OLD_PID 已不存在），清理后继续"
    rm -f "$LOCK_FILE"
  else
    echo "⚠️ 管道已在运行中（PID ${OLD_PID:-unknown}），退出"
    exit 0
  fi
fi
echo $$ > "$LOCK_FILE"

# ── 退出通知：无论如何都会执行 ──
_send_exit_notification() {
  local exit_code=$?
  rm -f "$LOCK_FILE"
  # 用 Python 发企微通知（成功/失败/崩溃）
  local mode="done"
  [ $exit_code -ne 0 ] && mode="crashed"
  "$NEWS_ROOT/.venv/bin/python" "$PRICE_ROOT/scripts/pipeline_notify.py" \
    --mode "$mode" "$LOG" 2>/dev/null || true
  exit $exit_code
}
trap _send_exit_notification EXIT

# ── 启动通知 ──
"$NEWS_ROOT/.venv/bin/python" "$PRICE_ROOT/scripts/pipeline_notify.py" \
  --mode start 2>/dev/null || true

# 步骤状态追踪（用于依赖检查）
_step_ok()  { for s in "${STEPS[@]}"; do [[ "$s" == "$1|OK|"* ]] && return 0; done; return 1; }

run_step() {
  local label="$1" cwd="$2"; shift 2
  echo ""
  echo "=== [$label] $(date +%H:%M:%S) ==="
  if cd "$cwd" 2>/dev/null && "$@" >>"$LOG" 2>&1; then
    echo "  ✅ $label 完成"
    add_step "$label" "OK"
    return 0
  else
    local ec=$?
    echo "  ❌ $label 失败（code=$ec），继续后续步骤"
    add_step "$label" "FAIL"
    return 1
  fi
}

# 依赖检查：前置步骤全部 OK 才执行，否则跳过
require_ok() {
  local missing=""
  for dep in "$@"; do
    _step_ok "$dep" || missing="$missing $dep"
  done
  if [ -n "$missing" ]; then
    echo "  ⏭️ 前置失败:$missing，跳过"
    return 1
  fi
  return 0
}

# 关键步骤重试版：失败后最多重试 2 次，30s/60s 递增间隔
retry_step() {
  local label="$1" cwd="$2"; shift 2
  local max_retries=2
  for attempt in 0 1 2; do
    echo ""
    echo "=== [$label] $(date +%H:%M:%S) ==="
    [ $attempt -gt 0 ] && echo "  🔄 第 ${attempt}/2 次重试..."
    if cd "$cwd" 2>/dev/null && "$@" >>"$LOG" 2>&1; then
      echo "  ✅ $label 完成"
      add_step "$label" "OK"
      return 0
    fi
    local ec=$?
    if [ $attempt -lt $max_retries ]; then
      local wait_sec=$((30 * (attempt + 1)))
      echo "  ⚠️ $label 失败 (exit=$ec)，${wait_sec}s 后重试..."
      sleep $wait_sec
    fi
  done
  echo "  ❌ $label 重试 ${max_retries} 次仍失败，放弃"
  add_step "$label" "FAIL"
  return 1
}

# ═══════════════════════════════════════════════════════════
# Phase 1: 数据底座（串行 — 日线→衍生回填→衍生物化）
# ═══════════════════════════════════════════════════════════
echo ""
echo "═══ Phase 1: 数据底座 ═══"
TODAY=$(date +%Y-%m-%d)

retry_step "股价日线" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --catch-up-panel --workers 6 --panel-date "$TODAY"

if require_ok "股价日线"; then
  retry_step "衍生-回填" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" scripts/enrich_daily_fields.py
else
  echo "  ⏭️ 衍生-回填 跳过（日线未就绪）"; add_step "衍生-回填" "SKIP"
fi
if require_ok "衍生-回填"; then
  retry_step "衍生-物化" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" scripts/materialize_daily_features.py
else
  echo "  ⏭️ 衍生-物化 跳过（回填未就绪）"; add_step "衍生-物化" "SKIP"
fi

# 检查核心数据是否就绪（后续复盘/预测的闸门）
CORE_OK=true
require_ok "股价日线" "衍生-物化" || CORE_OK=false

# ═══════════════════════════════════════════════════════════
# Phase 2: 辅助数据（并行 — 估值 ∥ 筹码 ∥ 指数，均独立）
# ═══════════════════════════════════════════════════════════
echo ""
echo "═══ Phase 2: 辅助数据（并行） ═══"

run_step "估值截面" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --snapshot &
PID_VAL=$!
run_step "筹码峰ETL" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --chip &
PID_CHIP=$!
run_step "指数日线" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" -m zplan_shared.etl_index &
PID_IDX=$!

wait $PID_VAL $PID_CHIP $PID_IDX
echo "  ── Phase 2 全部结束 ──"

# ═══════════════════════════════════════════════════════════
# Phase 3: 衍生计算（串行 — 季报→资讯→规则打分）
# ═══════════════════════════════════════════════════════════
echo ""
echo "═══ Phase 3: 衍生计算 ═══"

if $LITE; then
  echo ""; echo "=== [季报财务] $(date +%H:%M:%S) ==="
  echo "  ⏭️ 季报财务 跳过（--lite）"; add_step "季报财务" "SKIP"
else
  run_step "季报财务" "$PRICE_ROOT" "$PRICE_ROOT/.venv/bin/python" main.py --financial
fi

run_step "资讯同步" "$NEWS_ROOT" "$NEWS_ROOT/.venv/bin/python" -m sentiment_etl.runner

if require_ok "衍生-物化"; then
  run_step "规则打分" "$PICK_ROOT" "$PICK_ROOT/.venv/bin/python" main.py init-rule
else
  echo "  ⏭️ 规则打分 跳过（衍生数据未就绪）"; add_step "规则打分" "SKIP"
fi

# ═══════════════════════════════════════════════════════════
# Phase 4: LLM 选股（需规则打分 OK）
# ═══════════════════════════════════════════════════════════
echo ""
echo "═══ Phase 4: LLM 选股 ═══"

LLM_RUN=false
if require_ok "规则打分"; then
  run_step "LLM简评TOP300" "$PICK_ROOT" "$PICK_ROOT/.venv/bin/python" main.py llm-top --top 300
  _step_ok "LLM简评TOP300" && LLM_RUN=true
else
  echo "  ⏭️ LLM简评TOP300 跳过（规则打分未就绪）"; add_step "LLM简评TOP300" "SKIP"
fi

if $LLM_RUN; then
  echo ""; echo "=== [LLM消耗] $(date +%H:%M:%S) ==="
  "$PICK_ROOT/.venv/bin/python" -c "
import json
from zplan_shared.models import init_db, SessionLocal
from sqlalchemy import text
init_db(); s=SessionLocal()
r=s.execute(text(\"SELECT id, summary_json FROM pick_runs WHERE run_kind='llm_top300' ORDER BY created_at_utc DESC LIMIT 1\")).fetchone()
if r and r[1]:
    usage=json.loads(r[1]).get('llm_usage') or {}
    inp=int(usage.get('prompt_tokens') or 0)
    total=int(usage.get('total_tokens') or inp)
    out=int(usage.get('completion_tokens') or usage.get('output_tokens') or max(0,total-inp))
    model=usage.get('model','?')
    usd=round((inp/1e6*0.27)+(out/1e6*1.10),4)
    cny=round(usd*7.2,2)
    print(f'LLM_COST: run={r[0]} model={model} in={inp} out={out} total={total} usd={usd} cny={cny}')
s.close()
" 2>>"$LOG" || echo "LLM_COST: 查询失败"
fi

# ═══════════════════════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════
# Phase 5: 推送（数据播报永远发，复盘/预测需核心数据就绪）
# ═══════════════════════════════════════════════════════════
echo ""
echo "═══ Phase 5: 推送 ═══"

# 5a. 数据状态播报 — 永远发送
echo ""; echo "=== [数据状态播报] $(date +%H:%M:%S) ==="
"$NEWS_ROOT/.venv/bin/python" "$PRICE_ROOT/scripts/pipeline_notify.py" --mode done "$LOG" 2>/dev/null \
  && echo "  ✅ 播报完成" || echo "  ⚠️ 播报失败"

# 5b. 综合复盘 — 需核心数据就绪（$CORE_OK）
echo ""; echo "=== [综合复盘] $(date +%H:%M:%S) ==="
if $CORE_OK; then
  "$NEWS_ROOT/.venv/bin/python" "$NEWS_ROOT/scripts/evening_combined_review.py" 2>/dev/null \
    && echo "  ✅ 综合复盘完成" || echo "  ⚠️ 综合复盘失败"
else
  echo "  ⏭️ 综合复盘 跳过（核心数据未就绪）"
fi

# 5c. 大盘预测 — 需核心数据就绪
echo ""; echo "=== [大盘预测] $(date +%H:%M:%S) ==="
if $CORE_OK; then
  "$NEWS_ROOT/.venv/bin/python" "$NEWS_ROOT/scripts/market_forecast.py" 2>/dev/null \
    && echo "  ✅ 大盘预测完成" || echo "  ⚠️ 大盘预测失败"
else
  echo "  ⏭️ 大盘预测 跳过（核心数据未就绪）"
fi

# 5d. TOP10 选股+操作建议 — 需核心数据就绪
echo ""; echo "=== [TOP10选股] $(date +%H:%M:%S) ==="
if $CORE_OK; then
  "$NEWS_ROOT/.venv/bin/python" "$NEWS_ROOT/scripts/evening_top10_push.py" 2>/dev/null \
    && echo "  ✅ TOP10选股完成" || echo "  ⚠️ TOP10选股失败"
else
  echo "  ⏭️ TOP10选股 跳过（核心数据未就绪）"
fi

find "$LOG_DIR" -name "full_pipeline_*.log" -mtime +30 -delete 2>/dev/null || true
[ "$fail_n" -eq 0 ] || exit 1
