# Z-Plan 交接文档（粘贴给 Claude Code 即可）

你是 Z-Plan monorepo 的结对工程师。仓库路径示例：`/Users/richard/my_stock_ai`。请按本文接管开发与迭代闭环。

---

## 1. 立即执行（新环境 / 迁移后）

```bash
cd /Users/richard/my_stock_ai
./scripts/setup_vscode_claude.sh
code zplan.code-workspace
```

确认：

```bash
cd zplan-回测 && .venv/bin/python main.py check-data
cd zplan-回测 && .venv/bin/python main.py smoke --code 000001
```

---

## 2. 仓库结构

```
my_stock_ai/
├── zplan.code-workspace      # VS Code 多根工作区（必开此文件）
├── CLAUDE.md                 # Claude Code 自动读取
├── AGENTS.md                 # Agent 职责地图
├── scripts/
│   ├── setup_all_agents.sh   # Python 依赖 + DB 迁移 + 烟测
│   └── setup_vscode_claude.sh # VS Code + Claude Code 迁移
├── zplan-资讯/               # ZPLAN_ROOT，zplan.db，.env
├── zplan-共享/               # zplan_shared 包 + DATA_ARCHITECTURE.md
├── zplan-股价/               # 唯一写入 daily_prices
├── zplan-选股/               # 选股 + Gemini LLM
└── zplan-回测/               # 预测验证 + iterate 闭环
```

软链（兼容旧路径）：`zplan` → `zplan-资讯`，`zplan-shared` → `zplan-共享`

---

## 3. 环境与 API（两套 LLM，勿混淆）

### 3.1 业务选股 LLM（Gemini）

- 配置：`zplan-资讯/.env`
- 变量：`GEMINI_API_KEY`, `GEMINI_MODEL=gemini-2.5-pro`
- 代码：`zplan-共享/src/zplan_shared/llm/gemini.py`
- 调用方：`zplan-选股` 扫描简评、深度研报

### 3.2 Claude Code 本体（DeepSeek V4）

- 配置：`.claude/settings.json`（仓库内，无密钥）+ `.claude/settings.local.json`（本地，gitignore）
- 或：`zplan-资讯/.env` 中 `DEEPSEEK_API_KEY`
- 官方端点：`https://api.deepseek.com/anthropic`

`.claude/settings.local.json` 示例：

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "你的DeepSeek_API_Key",
    "ANTHROPIC_API_KEY": "你的DeepSeek_API_Key"
  }
}
```

VS Code 终端验证：`claude` → `/status`

---

## 4. 数据架构要点

详见 `zplan-共享/docs/DATA_ARCHITECTURE.md`。

| 层 | 表/存储 | 写入者 |
|----|---------|--------|
| 日线 | `daily_prices` | 仅 zplan-股价 |
| 选股结果 | `pick_runs`, `pick_entries` | zplan-选股 |
| 预测验证 | `pick_prediction_outcomes`, `pick_llm_evaluations` | zplan-回测 |
| 迭代 ledger | `backtest_review/iterations/*.json` | zplan-回测 iterate |

只读 API：

```python
from zplan_shared.market import get_bars, get_panel, latest_trade_date
```

`latest_trade_date()` 取 **截面完整** 的最新日（≥1000 只），避免自选增量污染 max(date)。

---

## 5. 日常运维命令

### 股价

```bash
cd zplan-股价
.venv/bin/python main.py --catch-up-panel --workers 8   # 补齐截面
.venv/bin/python main.py --a1                           # 全市场（耗时长）
```

### 选股

```bash
cd zplan-选股
.venv/bin/python main.py init-rule          # 全市场规则分 → stock_rule_scores
.venv/bin/python main.py llm-top --top 300  # Top300 + Gemini 简评
.venv/bin/python main.py --list-runs
.venv/bin/python main.py --show-run 8
```

策略配置：`zplan-选股/config/strategy.yaml`（weights、filters.max_ret_20d、ranking）

### 回测迭代闭环

```bash
cd zplan-回测
.venv/bin/python main.py iterate verify     # 每日：审计 + 落库 + 对比上轮
.venv/bin/python main.py iterate full       # 每周：init-rule + llm-top + 审计
.venv/bin/python main.py iterate history
.venv/bin/python main.py audit --run-id 8 --top 10
.venv/bin/python main.py llm-eval --run-id 8 --top 10
```

报告目录：`zplan-资讯/backtest_review/`

---

## 6. 迭代闭环（用户核心诉求）

```text
行情补齐 → init-rule → llm-top → 等 3~5 交易日
    → iterate verify → 看 fail_rate / mean_fwd_return
    → 按 Review 改 prompt(strateg.yaml) → iterate full → 再 verify
```

关键指标（存在 `iterations.jsonl`）：

- `fail_rate` — Top N 失败比例（越低越好）
- `mean_fwd_return` — 选股后 forward 收益
- `mean_delta` — LLM 分 − 规则分（过高说明 LLM 抬分）

已知失败标签（`pick_llm_eval.py`）：

- `momentum_chase`, `score_inflation`, `buy_unreachable`, `generic_bullish`, `forward_loss`

---

## 7. 修改指南（改哪里）

| 目标 | 文件 |
|------|------|
| LLM 简评纪律 | `zplan-选股/src/pick_agent/llm_research.py` → `_LLM_BRIEF_RULES` |
| 规则权重/过滤 | `zplan-选股/config/strategy.yaml` |
| 动量扣分 | `zplan-选股/src/pick_agent/scoring.py` |
| 建议买价 | `zplan-共享/src/zplan_shared/features.py` → `suggested_price_levels` |
| 回测标签/闭环 | `zplan-共享/src/zplan_shared/pick_llm_eval.py`, `zplan-回测/src/backtest_agent/iterate.py` |
| ORM/迁移 | `zplan-共享/src/zplan_shared/models.py` |

**原则：** 规则 = 候选池 + 硬约束；排序与推荐以 LLM 为主（`ranking.mode: llm_primary`）。

---

## 8. VS Code 推荐

- 扩展见 `.vscode/extensions.json`（Python、Ruff、Claude Code）
- 任务：`Terminal → Run Task` → Z-Plan 分组
- Python 解释器：各 Agent 文件夹选对应 `.venv/bin/python`

---

## 9. 禁止事项

- 不要 commit `.env`、`.db`、真实 API Key
- 不要在选股/回测里写 `daily_prices`
- 不要 force push main
- 改完 prompt/strategy 后必须用新 `llm-top` run 再评估，旧 run 不会变

---

## 10. 当前状态快照（交接时）

- 最近 LLM 选股 run：**run_id=8**（llm_top300，as_of 2026-05-21）
- Top10 审计：fail_rate 曾 100%，主因 buy_unreachable + momentum_chase + LLM 抬分
- 有效行情截面：**2026-05-27**（约 5498 只）；max(date) 可能仅部分自选，以 `check-data` 为准
- 迭代记录：`zplan-资讯/backtest_review/iterations/`

接到新任务时，先 `iterate history` 看曲线，再决定改 prompt 还是 strategy。
