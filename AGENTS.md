# Z-Plan 多 Agent 仓库地图

开发任意 Agent 前，**必读**行情架构文档：[`zplan-共享/docs/DATA_ARCHITECTURE.md`](zplan-共享/docs/DATA_ARCHITECTURE.md)。

| 目录 | 职责 |
|------|------|
| `zplan-资讯/` | 资讯抓取、摘要、微信；数据根 `zplan.db` |
| `zplan-共享/` | `zplan_shared` 包：配置、ORM、`market` 查询、`etl_akshare` |
| `zplan-股价/` | **唯一**写入 `daily_prices` |
| `zplan-选股/` | 选股策略（只读 `zplan_shared.market`） |
| `zplan-回测/` | 回测（只读 `zplan_shared.market`） |

多根工作区：打开 monorepo 根目录的 `zplan.code-workspace`。

## VS Code + Claude Code（DeepSeek V4）

```bash
./scripts/setup_vscode_claude.sh
code zplan.code-workspace
```

- 交接文档：`CLAUDE.md`（Claude Code 自动读）、`docs/VSCODE_CLAUDE_HANDOFF.md`（完整版）
- Claude Code 用 DeepSeek：`zplan-资讯/.env` 的 `DEEPSEEK_API_KEY` → `.claude/settings.local.json`
- 选股/资讯 LLM 已切换 DeepSeek（同 `.env` 的 `DEEPSEEK_API_KEY`），API Key 从 https://platform.deepseek.com/api_keys 获取

## 一键初始化（已替你跑过可跳过）

在 monorepo 根目录：

```bash
./scripts/setup_all_agents.sh
```

会：装齐各 Agent 依赖、迁移 Phase A 字段、写入演示行情、烟测选股/回测。正式 AkShare 同步（需能访问东财）：

```bash
cd zplan-股价 && .venv/bin/python main.py
```
