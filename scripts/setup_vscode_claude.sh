#!/usr/bin/env bash
# VS Code + Claude Code（DeepSeek V4）无感迁移：软链、Python 环境、Claude 本地密钥桥接
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo ">> Z-Plan VS Code / Claude Code 迁移"
echo "   根目录: $ROOT"

# 1. 兼容软链
ln -sfn "zplan-共享" zplan-shared 2>/dev/null || true
ln -sfn "zplan-资讯" zplan 2>/dev/null || true
ln -sfn "zplan-股价" zplan-trend 2>/dev/null || true
ln -sfn "zplan-选股" zplan-pick 2>/dev/null || true
ln -sfn "zplan-回测" zplan-backtest 2>/dev/null || true

# 2. Python 各 Agent 虚拟环境 + DB 迁移（不跑选股烟测，避免行情滞后阻断迁移）
for dir in zplan-资讯 zplan-股价 zplan-选股 zplan-回测; do
  echo ">> bootstrap ${dir}"
  bash "${ROOT}/${dir}/scripts/bootstrap_env.sh"
done
PY="${ROOT}/zplan-资讯/.venv/bin/python"
echo ">> init_db + Phase A 迁移"
"$PY" -c "from zplan_shared.models import init_db; init_db(); print('ok', __import__('zplan_shared.config', fromlist=['DB_URL']).DB_URL)"

# 3. 资讯 .env 模板
ENV_FILE="${ROOT}/zplan-资讯/.env"
ENV_EXAMPLE="${ROOT}/zplan-资讯/.env.example"
if [[ ! -f "$ENV_FILE" && -f "$ENV_EXAMPLE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo ">> 已从 .env.example 创建 zplan-资讯/.env（请填入 GEMINI_API_KEY / DEEPSEEK_API_KEY）"
fi

# 4. Claude Code 本地密钥（从 zplan-资讯/.env 的 DEEPSEEK_API_KEY 同步）
CLAUDE_LOCAL="${ROOT}/.claude/settings.local.json"
CLAUDE_EXAMPLE="${ROOT}/.claude/settings.local.json.example"
if [[ ! -f "$CLAUDE_LOCAL" ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set +u
    source "$ENV_FILE" 2>/dev/null || true
    set -u
    if [[ -n "${DEEPSEEK_API_KEY:-}" ]]; then
      mkdir -p "${ROOT}/.claude"
      cat > "$CLAUDE_LOCAL" <<EOF
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "${DEEPSEEK_API_KEY}",
    "ANTHROPIC_API_KEY": "${DEEPSEEK_API_KEY}"
  }
}
EOF
      echo ">> 已从 zplan-资讯/.env 生成 .claude/settings.local.json"
    else
      cp "$CLAUDE_EXAMPLE" "$CLAUDE_LOCAL"
      echo ">> 已创建 .claude/settings.local.json（请填入 DeepSeek API Key）"
    fi
  else
    mkdir -p "${ROOT}/.claude"
    cp "$CLAUDE_EXAMPLE" "$CLAUDE_LOCAL"
    echo ">> 已创建 .claude/settings.local.json（请填入 DeepSeek API Key）"
  fi
else
  echo ">> .claude/settings.local.json 已存在，跳过"
fi

# 5. 写入 shell 启动片段（可选 source）
SHELL_RC="${ROOT}/.zplan_env.sh"
cat > "$SHELL_RC" <<'EOF'
# Z-Plan 环境变量 — 在 shell 或 VS Code terminal 中: source .zplan_env.sh
_ZPLAN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export ZPLAN_ROOT="${_ZPLAN_ROOT}/zplan-资讯"
if [[ -f "${ZPLAN_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ZPLAN_ROOT}/.env"
  set +a
fi
# Claude Code × DeepSeek（若 .env 有 DEEPSEEK_API_KEY）
if [[ -n "${DEEPSEEK_API_KEY:-}" ]]; then
  export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.deepseek.com/anthropic}"
  export ANTHROPIC_AUTH_TOKEN="${DEEPSEEK_API_KEY}"
  export ANTHROPIC_API_KEY="${DEEPSEEK_API_KEY}"
  export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-deepseek-v4-pro[1m]}"
  export CLAUDE_CODE_SUBAGENT_MODEL="${CLAUDE_CODE_SUBAGENT_MODEL:-deepseek-v4-flash}"
fi
unset _ZPLAN_ROOT
EOF
echo ">> 已写入 .zplan_env.sh（终端可 source ${ROOT}/.zplan_env.sh）"

# 6. 烟测（回测 only；行情滞后时不阻断）
echo ">> 回测烟测"
if ! "${ROOT}/zplan-回测/.venv/bin/python" "${ROOT}/zplan-回测/main.py" smoke --code 000001; then
  echo "!! 回测烟测未通过，请稍后执行: cd zplan-回测 && .venv/bin/python main.py check-data"
fi

echo ""
echo "=========================================="
echo "迁移完成。下一步："
echo "  1. code ${ROOT}/zplan.code-workspace"
echo "  2. 安装推荐扩展（Python、Ruff、Claude Code）"
echo "  3. 终端: source ${ROOT}/.zplan_env.sh && claude"
echo "  4. 将 docs/VSCODE_CLAUDE_HANDOFF.md 交给 Claude Code 阅读"
echo "=========================================="
