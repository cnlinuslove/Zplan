#!/usr/bin/env bash
# 修复 OpenClaw 企微机器人：完成 bootstrap、注入代理、关闭 thinking 占位、清理卡死 session
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
GATEWAY_ENV="$HOME/.openclaw/service-env/ai.openclaw.gateway.env"
SESSIONS="$HOME/.openclaw/agents/main/sessions"

echo "==> 1/4 完成 OpenClaw workspace bootstrap（Zplan 财经助手）"
mkdir -p "$WS"
cat > "$WS/IDENTITY.md" <<'EOF'
# IDENTITY.md

- **Name:** Zplan
- **Creature:** 财经资讯助手
- **Vibe:** 简洁、准确、带来源
- **Emoji:** 📈
EOF

cat > "$WS/USER.md" <<'EOF'
# USER.md

- **Name:** Z-Plan 用户
- **Timezone:** Asia/Shanghai
- **Notes:** 企微群内 @Zplan 提问；优先调用 zplan-qa 技能 exec wechat-reply，不要编造行情。
EOF

cat > "$WS/SOUL.md" <<'EOF'
# SOUL.md

- 只做 Z-Plan 财经资讯：摘要、topic、多源问答。
- 收到任何问题时，**立即** exec `openclaw_bridge.py wechat-reply --text "<用户原话去@>"`，把 JSON 的 reply_text 原样回复。
- 禁止 web_search；禁止 BOOTSTRAP 寒暄；禁止「正在搜索」后不跟进。
EOF

rm -f "$WS/BOOTSTRAP.md"
echo "    已写入 IDENTITY/USER/SOUL，删除 BOOTSTRAP.md"

echo "==> 2/4 为 OpenClaw LaunchAgent 注入系统代理（Gemini 需走代理）"
PROXY="$(
  cd "$ROOT" && .venv/bin/python - <<'PY'
from outbound_http import resolve_effective_proxy_url
url, _ = resolve_effective_proxy_url()
print(url or "")
PY
)"
if [[ -n "$PROXY" && -f "$GATEWAY_ENV" ]]; then
  grep -q '^export HTTP_PROXY=' "$GATEWAY_ENV" 2>/dev/null || echo "export HTTP_PROXY='$PROXY'" >> "$GATEWAY_ENV"
  grep -q '^export HTTPS_PROXY=' "$GATEWAY_ENV" 2>/dev/null || echo "export HTTPS_PROXY='$PROXY'" >> "$GATEWAY_ENV"
  grep -q '^export NO_PROXY=' "$GATEWAY_ENV" 2>/dev/null || echo "export NO_PROXY='127.0.0.1,localhost'" >> "$GATEWAY_ENV"
  echo "    HTTP(S)_PROXY=$PROXY"
else
  echo "    未检测到代理或 gateway.env 不存在，跳过"
fi

echo "==> 3/4 OpenClaw 配置：flash 模型、关闭 thinking 占位"
if command -v openclaw >/dev/null 2>&1; then
  openclaw config set agents.defaults.model.primary "google/gemini-2.5-flash"
  openclaw config set channels.wecom.sendThinkingMessage false
  echo "    已设置 gemini-2.5-flash + sendThinkingMessage=false"
else
  echo "    未找到 openclaw CLI，跳过 config"
fi

echo "==> 4/4 清理卡死的 wecom group session"
if [[ -d "$SESSIONS" ]]; then
  find "$SESSIONS" -maxdepth 1 \( -name '*.jsonl' -o -name '*.trajectory.jsonl' \) -mtime -2 -print0 2>/dev/null | while IFS= read -r -d '' f; do
    if grep -q 'wecom:group' "$f" 2>/dev/null && grep -q 'fetch failed' "$f" 2>/dev/null; then
      bak="${f}.bak-stuck-$(date +%s)"
      mv "$f" "$bak"
      echo "    已归档 $(basename "$f") -> $(basename "$bak")"
    fi
  done
fi

if command -v openclaw >/dev/null 2>&1; then
  echo ""
  echo "重启网关: openclaw gateway restart"
  openclaw gateway restart 2>/dev/null || openclaw gateway start
fi

echo ""
echo "完成。推荐改用直连 Bot（更快、可并行）："
echo "  openclaw gateway stop"
echo "  cd $ROOT && ./scripts/start_wecom_direct_bot.sh"
