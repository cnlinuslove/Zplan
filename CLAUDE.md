# Z-Plan Monorepo — Claude Code 项目说明

> **给 Claude Code 的第一份文档。** 接手后先读本文件，再读 `docs/VSCODE_CLAUDE_HANDOFF.md` 做环境确认。

## 项目是什么

Z-Plan 是 A 股多 Agent 量化流水线（monorepo），数据根目录 **`zplan-资讯/`**（`ZPLAN_ROOT`），SQLite 库 **`zplan-资讯/zplan.db`**。

| 目录 | Agent | 读写 |
|------|-------|------|
| `zplan-资讯/` | 资讯、企微、Gemini 摘要 | 读行情；写资讯表 |
| `zplan-共享/` | `zplan_shared` 包 | ORM、market 只读 API、AkShare ETL |
| `zplan-股价/` | 股价 ETL | **唯一**写 `daily_prices` |
| `zplan-选股/` | 规则 + LLM 选股 | 只读 market；写 `pick_runs` |
| `zplan-回测/` | 预测价验证、迭代闭环 | 只读 market + pick 历史 |

**架构必读：** `zplan-共享/docs/DATA_ARCHITECTURE.md`  
**Agent 地图：** `AGENTS.md`

## 打开方式（VS Code）

```bash
code /path/to/my_stock_ai/zplan.code-workspace
```

多根工作区五文件夹：资讯 / 共享 / 股价 / 选股 / 回测。

## 环境与密钥（不要提交 Git）

| 用途 | 配置文件 | 变量 |
|------|----------|------|
| 业务 LLM（选股 Gemini） | `zplan-资讯/.env` | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| Claude Code 本体 | `zplan-资讯/.env` 或 `.claude/settings.local.json` | `DEEPSEEK_API_KEY` → 见下方 |
| 数据库 | 默认即可 | `DB_URL` 或 `{ZPLAN_ROOT}/zplan.db` |

一键初始化（新机器 / 迁移后）：

```bash
cd /path/to/my_stock_ai
./scripts/setup_vscode_claude.sh
```

## Claude Code × DeepSeek V4

项目已配置 **Anthropic 兼容端点** → DeepSeek（见 `.claude/settings.json`）。  
**API Key 放本地，勿写入仓库：**

1. 复制 `zplan-资讯/.env.example` → `zplan-资讯/.env`（若尚无）
2. 填入 `DEEPSEEK_API_KEY=sk-...`
3. 或复制 `.claude/settings.local.json.example` → `.claude/settings.local.json`

启动 Claude Code 前确认：

```bash
source zplan-资讯/.env   # 含 DEEPSEEK_API_KEY
claude                   # 或 VS Code 内 Claude Code 扩展
/status                  # 应显示 DeepSeek 端点
```

## 数据流（不可破坏）

```
AkShare → zplan-股价 ETL → zplan.db
                ↓
         zplan_shared.market（只读）
                ↓
         选股 / 回测 / 资讯
```

- 选股、回测 **禁止** 直连 AkShare 拉 K 线  
- 选股、回测 **禁止** 直接 SQL 摸 `daily_prices`（用 `get_bars` / `get_panel`）

## 数据管道（自动运行）

**定时任务（launchd）**：`launchctl list | grep zplan`

| Job | 时间 | 内容 |
|-----|------|------|
| `com.zplan.pipeline-daily` | 每天 17:35 | 日线+衍生+估值+资讯+企微播报 |
| `com.zplan.pipeline-weekly` | 周五 18:00 | 全量含季报+规则打分 |
| `com.zplan.news-intraday` | 8:00/12:00/16:00/21:00 | 东财快讯+新闻关联 |
| `ai.zplan.wecom-direct` | 常驻 | 企微 Bot 直连 |

```bash
# 一键查看所有数据板块状态
~/my_stock_ai/zplan-股价/scripts/run_full_pipeline.sh --status

# 手动跑一次（日常精简版）
~/my_stock_ai/zplan-股价/scripts/run_full_pipeline.sh --lite

# 手动跑全量
~/my_stock_ai/zplan-股价/scripts/run_full_pipeline.sh

# 看日志
tail ~/my_stock_ai/zplan-资讯/logs/full_pipeline_*.log
```

**机器休眠**：唤醒后 launchd 自动补跑，管道增量+补缺机制兜底。企微群会收到盘后数据状态播报。

## 常用命令

```bash
# 企微选股测试
cd zplan-资讯 && .venv/bin/python openclaw_bridge.py wechat-reply --text "选股 平安银行"

# 行情同步（东财，需网络）
cd zplan-股价 && .venv/bin/python main.py --catch-up-panel --workers 8

# 选股流水线
cd zplan-选股 && .venv/bin/python main.py init-rule
cd zplan-选股 && .venv/bin/python main.py llm-top --top 300    # LLM 简评（¥1.19/次）

# 回测迭代闭环（每日 / 每周）
cd zplan-回测 && .venv/bin/python main.py iterate verify
cd zplan-回测 && .venv/bin/python main.py iterate full
cd zplan-回测 && .venv/bin/python main.py iterate history
```

迭代记录：`zplan-资讯/backtest_review/iterations/`

## 近期修复（2026-06-05）

1. 企微 JSON 解析失败 → `ZPLAN_ROOT` env var 未解析 + 管道 flush 修复
2. 衍生指标停更 2 周 → `enrich_daily_fields.py` date bug 修复
3. LLM 不工作 → `DEEPSEEK_API_KEY` 从 Google key 改为 DeepSeek key
4. 资讯链接不可点击 → 从 markdown `[title](url)` 改为明文 URL
5. LLM 简评从 35 字扩展到 60 字，新增 📊📋💡 等多维度展示
6. 创建统一数据管道 `run_full_pipeline.sh` + 企微播报

用户在优化 **LLM 选股 Top10 失败率**（run_id=8 曾 100% fail）。主要问题：

1. 规则池偏动量（`ret_20d` 加分）→ 已加 `max_ret_20d`、`momentum_penalty`
2. LLM 集体抬分 90+ → `_LLM_BRIEF_RULES` in `zplan-选股/src/pick_agent/llm_research.py`
3. 建议买价 unreachable → `suggested_price_levels` + 回测 `buy_unreachable` 标签
4. 排序以 LLM 为主 → `zplan-选股/config/strategy.yaml` → `ranking`

改 prompt/strategy 后必须 **重跑 llm-top**，再 **iterate verify** 对比 `fail_rate`。

## 远程任务（企微 → Claude Code）

用户可通过企微 `@bot claude <任务描述>` 发送远程任务。launchd 轮询器自动调用 `claude -p` 处理。

**任务队列：** `zplan-资讯/claude_tasks/queue.jsonl`  
**轮询器：** `com.zplan.claude-task-poller`（每 60 秒）  
**日志：** `zplan-资讯/logs/claude_task_poller.log`

每次 Claude Code 会话启动时，先检查队列是否有 `pending` 任务：
```bash
cd zplan-资讯 && .venv/bin/python -c "
from claude_tasks import queue as q
pending = q.list_tasks('pending')
if pending:
    print(f'??? {len(pending)} 个待处理任务')
    for t in pending:
        print(f'  - [{t[\"id\"][:8]}] {t[\"text\"][:100]}')
else:
    print('? 无待处理任务')
"
```

有任务时主动处理并推送结果到企微。

## 编码约定

- Python ≥ 3.12，各 Agent 独立 `.venv`，共享包 `-e ../zplan-共享`
- 注释与文档中文；代码标识符英文
- 小步 diff；不提交 `.env`、`.db`、`logs/*.log`
- 改 schema 同步 `zplan-共享/src/zplan_shared/models.py` 迁移函数

## 测试

```bash
cd zplan-选股 && .venv/bin/pytest tests/ -q
cd zplan-回测 && .venv/bin/pytest tests/ -q
./scripts/setup_all_agents.sh --skip-demo   # 全链路烟测
```

## 详细交接

完整命令表、表结构、故障排查 → **`docs/VSCODE_CLAUDE_HANDOFF.md`**
