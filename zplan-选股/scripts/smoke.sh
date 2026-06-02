#!/usr/bin/env bash
# 选股 Agent 端到端烟测：指标层 → 扫描 → 单票研报（Markdown + JSON）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || bash "${ROOT}/scripts/bootstrap_env.sh"

echo ">> features + 行情"
"$PY" -c "
from zplan_shared.features import enrich_bars, latest_features
from zplan_shared.market import get_bars, get_panel, latest_trade_date

d = latest_trade_date()
assert d is not None, '无日线：请先运行 zplan-股价'
panel = get_panel()
assert len(panel) >= 1, '截面为空'
code = panel.iloc[0]['ts_code']
bars = get_bars(code)
assert len(bars) >= 60, f'{code} 日线不足'
feat = enrich_bars(bars)
for col in ('kdj_k', 'close_vs_ma20', 'ma5_cross_ma20', 'high_60d_pct', 'atr_pct'):
    assert col in feat.columns, col
snap = latest_features(feat)
assert snap.get('close_vs_ma20') is not None
print('ok panel=', len(panel), 'sample=', code, 'bars=', len(bars), 'p0=', len(snap))
"

echo ">> 全市场扫描"
"$PY" "${ROOT}/main.py" --top 3 --min-score 40 --no-llm

echo ">> 单票研报 Markdown（规则引擎，300058 蓝色光标）"
CODE="300058"
if ! "$PY" -c "
from zplan_shared.market import get_bars
from zplan_shared.market import resolve_ts_code
c = resolve_ts_code('${CODE}')
import sys
sys.exit(0 if len(get_bars(c)) >= 60 else 1)
" 2>/dev/null; then
  CODE="$("$PY" -c "from zplan_shared.market import get_panel; print(get_panel().iloc[0]['ts_code'])")"
  echo "   (300058 不可用，改用 ${CODE})"
fi
"$PY" "${ROOT}/main.py" -s "$CODE" --no-llm | head -20

echo ">> 单票研报 JSON"
"$PY" "${ROOT}/main.py" -s "$CODE" --no-llm --format json | "$PY" -c "import json,sys; json.load(sys.stdin); print('ok json')"

echo ">> 导出扫描结果"
"$PY" "${ROOT}/main.py" --top 3 --min-score 40 --no-llm -o "${ROOT}/.smoke_picks.json"
test -f "${ROOT}/.smoke_picks.json"

echo ">> 单元测试"
"$PY" -m pytest "${ROOT}/tests" -q

echo ">> 完成"
