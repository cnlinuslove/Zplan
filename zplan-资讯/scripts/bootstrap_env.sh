#!/usr/bin/env bash
# 一键创建项目 .venv 并安装依赖（优先 uv，否则 python3 -m venv）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
REQ="${ROOT}/requirements.txt"
SHARED="${ROOT}/../zplan-共享"

if [[ ! -f "$REQ" ]]; then
  echo "缺少 requirements.txt: $REQ" >&2
  exit 1
fi
if [[ ! -f "${SHARED}/pyproject.toml" ]]; then
  echo "缺少 zplan-共享: ${SHARED}" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  if [[ ! -x .venv/bin/python ]]; then
    uv venv
  fi
  uv pip install -e "$SHARED" -r "$REQ" --python .venv/bin/python
  # 提供 pip 可执行文件，便于用户习惯用 .venv/bin/pip
  uv pip install pip --python .venv/bin/python
  echo ""
  echo "完成：虚拟环境 ${ROOT}/.venv"
  echo "  source \"${ROOT}/.venv/bin/activate\""
  echo "  或: \"${ROOT}/.venv/bin/python\" openclaw_bridge.py diag"
else
  echo "未检测到 uv，使用 python3 -m venv（建议安装 uv: https://docs.astral.sh/uv/installation/）"
  if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
  fi
  .venv/bin/python -m ensurepip --upgrade 2>/dev/null || true
  .venv/bin/python -m pip install -U pip wheel
  .venv/bin/python -m pip install -e "$SHARED" -r "$REQ"
  echo ""
  echo "完成：虚拟环境 ${ROOT}/.venv"
  echo "  source \"${ROOT}/.venv/bin/activate\""
fi
