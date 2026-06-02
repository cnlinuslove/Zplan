# Z-Plan（my_stock_ai monorepo）

Agentic Stock Assistant — 多 Agent 量化资讯与行情流水线。目录职责见 [AGENTS.md](AGENTS.md)；行情架构见 [zplan-共享/docs/DATA_ARCHITECTURE.md](zplan-共享/docs/DATA_ARCHITECTURE.md)。

| 目录 | 职责 |
|------|------|
| `zplan-资讯/` | 资讯抓取、摘要、企微/OpenClaw 桥接 |
| `zplan-共享/` | `zplan_shared` 包：配置、ORM、`market`、`etl_akshare` |
| `zplan-股价/` | **唯一**写入 `daily_prices` |
| `zplan-选股/` | 选股（只读 `zplan_shared.market`） |
| `zplan-回测/` | 回测（只读 `zplan_shared.market`） |

## 一键初始化

```bash
./scripts/setup_all_agents.sh
```

## 工作区

用 VS Code / Cursor 打开 **`zplan.code-workspace`**（多根工作区）。

## OpenClaw

[OpenClaw](https://github.com/OpenClaw/OpenClaw) 为独立编排层，**不在本仓库内跟踪**。本地需要时在 monorepo 旁目录执行：

```bash
git clone https://github.com/OpenClaw/OpenClaw.git OpenClaw
```

企微/Gemini 同步脚本见 `zplan-资讯/scripts/sync_openclaw_*.sh`。
