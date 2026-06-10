#!/usr/bin/env bash
# Z-Plan 盘中执行层 macOS launchd 自动安装
#
# 由 setup_all_agents.sh 自动调用，也可单独运行:
#     bash scripts/install_execution_launchagents.sh
#     bash scripts/install_execution_launchagents.sh --uninstall
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
NEWS_VENV="$SCRIPT_DIR/../../zplan-资讯/.venv/bin/python"

JOBS=(
  "com.zplan.execution.pre-market:8:28:盘前检查"
  "com.zplan.execution.auction:9:25:集合竞价"
  "com.zplan.execution.opening:9:32:开盘决策"
  "com.zplan.execution.intraday:10:00:盘中监控"
)

generate_plist() {
  local label="$1" hour="$2" minute="$3" desc="$4" script_name

  case "$label" in
    *pre-market) script_name="pre_market_briefing.py" ;;
    *auction)    script_name="auction_check.py" ;;
    *opening)    script_name="opening_guidance.py" ;;
    *intraday)   script_name="intraday_watch.py" ;;
    *) echo "未知 job: $label" >&2; return 1 ;;
  esac

  # 盘中监控用多个时间点
  local intervals=""
  if [ "$label" = "com.zplan.execution.intraday" ]; then
    for h in 10 10 11 13 14 14; do
      for m in 0 30 0 30 0 30; do
        [ "$h" = "10" ] && [ "$m" = "0" ] || [ "$h" = "10" ] && [ "$m" = "30" ] || \
        [ "$h" = "11" ] && [ "$m" = "0" ] || [ "$h" = "13" ] && [ "$m" = "30" ] || \
        [ "$h" = "14" ] && [ "$m" = "0" ] || [ "$h" = "14" ] && [ "$m" = "30" ] || continue
        for wd in 1 2 3 4 5; do
          intervals+="        <dict><key>Hour</key><integer>${h}</integer><key>Minute</key><integer>${m}</integer><key>Weekday</key><integer>${wd}</integer></dict>
"
        done
      done
    done
  else
    for wd in 1 2 3 4 5; do
      intervals+="        <dict><key>Hour</key><integer>${hour}</integer><key>Minute</key><integer>${minute}</integer><key>Weekday</key><integer>${wd}</integer></dict>
"
    done
  fi

  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${NEWS_VENV}</string>
        <string>${SCRIPT_DIR}/${script_name}</string>
        <string>--top</string>
        <string>10</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}/..</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>ZPLAN_ROOT</key>
        <string>${SCRIPT_DIR}/../../zplan-资讯</string>
    </dict>
    <key>StartCalendarInterval</key>
    <array>
${intervals}    </array>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/../../zplan-资讯/logs/execution_${label##*.}.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/../../zplan-资讯/logs/execution_${label##*.}.err</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF
}

install_all() {
  mkdir -p "$LAUNCHD_DIR"
  local installed=0 skipped=0

  for entry in "${JOBS[@]}"; do
    IFS=':' read -r label hour minute desc <<< "$entry"
    local plist="$LAUNCHD_DIR/${label}.plist"

    generate_plist "$label" "$hour" "$minute" "$desc" > "$plist"
    chmod 644 "$plist"

    # 先卸载旧的再加载新的
    launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
      echo "  ✅ ${label} (${desc} ${hour}:${minute})"
      ((installed++))
    else
      echo "  ⚠️  ${label} 加载失败，请手动检查"
      ((skipped++))
    fi
  done

  echo ""
  echo "执行层定时任务: ${installed} 已安装"
  echo "查看: launchctl list | grep zplan.execution"
  echo "日志: ~/my_stock_ai/zplan-资讯/logs/execution_*.log"
}

uninstall_all() {
  for entry in "${JOBS[@]}"; do
    IFS=':' read -r label hour minute desc <<< "$entry"
    local plist="$LAUNCHD_DIR/${label}.plist"
    if [ -f "$plist" ]; then
      launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
      rm -f "$plist"
      echo "  ✅ ${label} 已卸载"
    fi
  done
}

case "${1:-install}" in
  install)   install_all ;;
  uninstall) uninstall_all ;;
  *)
    echo "用法: $0 [install|uninstall]"
    exit 1
    ;;
esac
