#!/usr/bin/env bash
# 安装 macOS LaunchAgent：交易日收盘后自动更新 A 股日线
set -euo pipefail
PRICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NEWS_ROOT="$(cd "${PRICE_ROOT}/../zplan-资讯" && pwd)"
LABEL="ai.zplan.daily-prices"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
RUNNER="${PRICE_ROOT}/scripts/run_daily_prices.sh"

if [[ ! -x "${PRICE_ROOT}/.venv/bin/python" ]]; then
  echo "请先: cd ${PRICE_ROOT} && ./scripts/bootstrap_env.sh" >&2
  exit 1
fi
chmod +x "${RUNNER}"

# 收盘后主跑 + 次日早盘补跑（与资讯 8:00 错开几分钟）
EVENING="${DAILY_PRICES_CRON_EVENING:-17:35}"
MORNING="${DAILY_PRICES_CRON_MORNING:-08:05}"
EH=$(echo "$EVENING" | cut -d: -f1)
EM=$(echo "$EVENING" | cut -d: -f2)
MH=$(echo "$MORNING" | cut -d: -f1)
MM=$(echo "$MORNING" | cut -d: -f2)

# Weekday 1=周一 … 5=周五（A 股）
WEEKDAYS=(1 2 3 4 5)

intervals=""
for wd in "${WEEKDAYS[@]}"; do
  intervals+="
    <dict>
      <key>Weekday</key>
      <integer>${wd}</integer>
      <key>Hour</key>
      <integer>${EH}</integer>
      <key>Minute</key>
      <integer>${EM}</integer>
    </dict>"
done
for wd in "${WEEKDAYS[@]}"; do
  intervals+="
    <dict>
      <key>Weekday</key>
      <integer>${wd}</integer>
      <key>Hour</key>
      <integer>${MH}</integer>
      <key>Minute</key>
      <integer>${MM}</integer>
    </dict>"
done

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>export ZPLAN_ROOT='${NEWS_ROOT}'; ${RUNNER}</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>${intervals}
  </array>
  <key>WorkingDirectory</key>
  <string>${PRICE_ROOT}</string>
  <key>StandardOutPath</key>
  <string>${NEWS_ROOT}/logs/launchd_daily_prices.out.log</string>
  <key>StandardErrorPath</key>
  <string>${NEWS_ROOT}/logs/launchd_daily_prices.err.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "已安装 LaunchAgent: ${PLIST}"
echo "  工作日 ${MORNING}、${EVENING} 执行: ${RUNNER}"
echo "  日志: ${NEWS_ROOT}/logs/cron_daily_prices.log"
echo ""
echo "立即试跑: ${RUNNER}"
echo "卸载: launchctl bootout gui/$(id -u)/${LABEL} && rm ${PLIST}"
