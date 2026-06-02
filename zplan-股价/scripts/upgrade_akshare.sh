#!/usr/bin/env bash
# 升级 AkShare（东财接口变更时优先执行）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -x .venv/bin/python ]] || ./scripts/bootstrap_env.sh

if command -v uv >/dev/null 2>&1; then
  uv pip install -U 'akshare>=1.16.0' --python .venv/bin/python
else
  .venv/bin/python -m pip install -U 'akshare>=1.16.0'
fi

echo "AkShare 版本:"
.venv/bin/python -c "from zplan_shared.etl_akshare import get_akshare_version; print(get_akshare_version())"
echo ""
echo "建议接着运行: .venv/bin/python scripts/check_akshare_connectivity.py --quick"
