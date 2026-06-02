#!/usr/bin/env bash
# 安装 macOS LaunchAgent：每天 8:00、17:30 各跑一轮每日资讯
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.zplan.daily-news"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
RUNNER="${ROOT}/scripts/run_daily_news.sh"

if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
  echo "请先: cd ${ROOT} && ./scripts/bootstrap_env.sh" >&2
  exit 1
fi
chmod +x "${RUNNER}"

MORNING="${DAILY_NEWS_CRON_MORNING:-08:00}"
EVENING="${DAILY_NEWS_CRON_EVENING:-17:30}"

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
    <string>${RUNNER}</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict>
      <key>Hour</key>
      <integer>$(echo "$MORNING" | cut -d: -f1)</integer>
      <key>Minute</key>
      <integer>$(echo "$MORNING" | cut -d: -f2)</integer>
    </dict>
    <dict>
      <key>Hour</key>
      <integer>$(echo "$EVENING" | cut -d: -f1)</integer>
      <key>Minute</key>
      <integer>$(echo "$EVENING" | cut -d: -f2)</integer>
    </dict>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>StandardOutPath</key>
  <string>${ROOT}/logs/launchd_daily_news.out.log</string>
  <key>StandardErrorPath</key>
  <string>${ROOT}/logs/launchd_daily_news.err.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "已安装 LaunchAgent: ${PLIST}"
echo "  每日 ${MORNING}、${EVENING} 执行: ${RUNNER}"
echo "  日志: ${ROOT}/logs/cron_daily_news.log"
echo ""
echo "立即试跑: ${RUNNER}"
echo "卸载: launchctl bootout gui/$(id -u)/${LABEL} && rm ${PLIST}"
