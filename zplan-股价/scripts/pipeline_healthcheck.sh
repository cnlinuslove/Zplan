#!/bin/bash
# Z-Plan 管道健康检查 — 验证当日数据是否就绪，缺失则触发补跑
# 用法:
#   pipeline_healthcheck.sh            # 检查 + 补跑
#   pipeline_healthcheck.sh --alert    # 仅告警（19:00 兜底）
#   pipeline_healthcheck.sh --dry-run  # 仅打印状态，不执行
set -u

MONO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PRICE_ROOT="$MONO_ROOT/zplan-股价"
NEWS_ROOT="$MONO_ROOT/zplan-资讯"
export ZPLAN_ROOT="${ZPLAN_ROOT:-$NEWS_ROOT}"

DRY=false; ALERT=false
for a in "$@"; do
  case "$a" in --dry-run) DRY=true ;; --alert) ALERT=true ;; esac
done

TODAY=$(date +%Y-%m-%d)
LOG_DIR="$NEWS_ROOT/logs"
mkdir -p "$LOG_DIR"
THRESHOLD_PCT=1

# ── 交易日检查 ──
DOW=$(date +%u)
if [ "$DOW" -ge 6 ]; then
  echo "⏭️ 周末，健康检查跳过 ($(date))"
  exit 0
fi
MD=$(date +%m-%d)
case "$MD" in
  01-01|01-02|01-03|01-04|01-05|01-06|01-07|\
  02-16|02-17|02-18|02-19|02-20|02-21|\
  04-06|04-07|\
  05-01|05-02|05-03|05-04|05-05|\
  06-19|06-20|06-21|\
  10-01|10-02|10-03|10-04|10-05|10-06|10-07|10-08)
  echo "⏭️ 节假日，健康检查跳过 ($(date))"
  exit 0
  ;;
esac

# ── 查 DB ──
check_db() {
  cd "$PRICE_ROOT"
  "$PRICE_ROOT/.venv/bin/python" << PYEOF
from zplan_shared.models import init_db, SessionLocal
from sqlalchemy import text
init_db(); s = SessionLocal()

checks = [
    ("daily_prices", "trade_date"),
    ("daily_features", "trade_date"),
    ("daily_snapshot", "trade_date"),
    ("daily_chip", "trade_date"),
]
issues = []
for table, col in checks:
    r = s.execute(text(f"SELECT MAX({col}) FROM {table}")).fetchone()
    latest = str(r[0])[:10] if r[0] else "无"
    date_ok = (latest == "$TODAY")
    # 截面数量
    cnt = 0
    cnt_prev = 0
    if date_ok:
        cnt = s.execute(text(f"SELECT COUNT(DISTINCT ts_code) FROM {table} WHERE {col}='$TODAY'")).fetchone()[0]
        prev_date = s.execute(text(f"SELECT MAX({col}) FROM {table} WHERE {col}<'$TODAY'")).fetchone()
        cnt_prev = 0
        if prev_date and prev_date[0]:
            cnt_prev = s.execute(text(f"SELECT COUNT(DISTINCT ts_code) FROM {table} WHERE {col}=:pd"), {"pd": str(prev_date[0])}).fetchone()[0]
    print(f"{table}: latest={latest} today=$TODAY date_ok={date_ok} cnt={cnt} prev={cnt_prev}")
    if not date_ok:
        issues.append(f"{table}日期滞后")
    elif cnt_prev > 0 and cnt < cnt_prev * (100 - $THRESHOLD_PCT) / 100:
        issues.append(f"{table}截面异动:{cnt}vs前日{cnt_prev}(-{cnt_prev-cnt}只)")

s.close()
print(f"ISSUES={len(issues)}")
if issues:
    for x in issues:
        print(f"ISSUE: {x}")
PYEOF
}

# ── 发送通知 ──
notify() {
  local msg="$1"
  echo "$msg"
  if ! $DRY; then
    "$NEWS_ROOT/.venv/bin/python" -c "
from wechat_push import push_wechat_text
push_wechat_text('$msg')
" 2>/dev/null || true
  fi
}

# ── 主逻辑 ──
echo "=== Z-Plan 健康检查 $(date) ==="
OUTPUT=$(check_db 2>&1)
echo "$OUTPUT"

ISSUE_COUNT=$(echo "$OUTPUT" | grep "ISSUES=" | tail -1 | cut -d= -f2)

if [ "${ISSUE_COUNT:-99}" -eq 0 ]; then
  echo "✅ 今日数据已就绪"
  if $ALERT; then
    notify "✅ Z-Plan 今日数据已全部就绪 ($TODAY)"
  fi
  exit 0
fi

# ── 有缺失 ──
echo "⚠️ 发现 ${ISSUE_COUNT} 个模块数据缺失"

if $ALERT; then
  # 19:00 告警模式：可能 17:35 主跑 和 18:30 补跑 都失败了
  notify "🚨 Z-Plan 数据告警 ($TODAY) | 至今仍有 ${ISSUE_COUNT} 个模块未更新 | 日志: $LOG_DIR"
  exit 1
fi

# 18:30 修复模式：触发补跑
LOCK_FILE="$LOG_DIR/.pipeline_running"
if [ -f "$LOCK_FILE" ]; then
  PID=$(cat "$LOCK_FILE" 2>/dev/null || true)
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    echo "⚠️ 管道正在运行中 (PID $PID)，跳过补跑"
    exit 0
  fi
  echo "⚠️ 残留锁文件，清理后补跑"
  rm -f "$LOCK_FILE"
fi

if $DRY; then
  echo "[DRY-RUN] 将执行: $PRICE_ROOT/scripts/run_full_pipeline.sh --lite"
  exit 0
fi

notify "🔄 Z-Plan 健康检查发现数据缺失，自动触发补跑 ($TODAY)"
echo "触发补跑..."
exec "$PRICE_ROOT/scripts/run_full_pipeline.sh" --lite
