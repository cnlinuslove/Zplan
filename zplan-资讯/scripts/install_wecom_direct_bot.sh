#!/usr/bin/env bash
# 安装 Z-Plan 企微直连 Bot 为 LaunchAgent（开机自启，不依赖 OpenClaw Agent）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ai.zplan.wecom-direct"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/.openclaw/logs"
BOT_SCRIPT="$ROOT/scripts/wecom_zplan_bot.mjs"
NODE_BIN="$(command -v node || true)"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "请先: cd $ROOT && ./scripts/bootstrap_env.sh" >&2
  exit 1
fi
if [[ ! -f "$ROOT/.env" ]]; then
  echo "缺少 $ROOT/.env" >&2
  exit 1
fi
if [[ -z "$NODE_BIN" ]]; then
  echo "需要 node 命令" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

PROXY="$(
  cd "$ROOT" && .venv/bin/python - <<'PY'
from outbound_http import resolve_effective_proxy_url
url, _ = resolve_effective_proxy_url()
print(url or "")
PY
)"

# 与 OpenClaw 网关互斥
if command -v openclaw >/dev/null 2>&1; then
  openclaw gateway stop 2>/dev/null || true
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${NODE_BIN}</string>
    <string>${BOT_SCRIPT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/zplan-wecom-direct.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/zplan-wecom-direct.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>USE_SYSTEM_PROXY</key>
    <string>true</string>
$(if [[ -n "$PROXY" ]]; then cat <<PROXYEOF
    <key>HTTP_PROXY</key>
    <string>${PROXY}</string>
    <key>HTTPS_PROXY</key>
    <string>${PROXY}</string>
PROXYEOF
fi)
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

sleep 2
if tail -n 3 "$LOG_DIR/zplan-wecom-direct.log" 2>/dev/null | grep -q "已连接企微"; then
  echo "✅ 企微直连 Bot 已启动（LaunchAgent: ${LABEL}）"
  tail -n 2 "$LOG_DIR/zplan-wecom-direct.log"
else
  echo "⚠️  已加载 LaunchAgent，请查看日志:"
  echo "   tail -f $LOG_DIR/zplan-wecom-direct.log"
  tail -n 5 "$LOG_DIR/zplan-wecom-direct.err.log" 2>/dev/null || true
fi
echo ""
echo "群内 @Zplan 帮助 测试。停用: launchctl bootout gui/\$(id -u)/${LABEL}"
