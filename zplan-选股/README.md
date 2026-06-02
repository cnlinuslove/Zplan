# zplan-选股（选股 Agent）

独立工作区。只读行情与资讯，统一使用 `zplan_shared.market` / `features` / `pick_context`（见 `zplan-共享/docs/DATA_ARCHITECTURE.md`）。

## 能力

- **全市场扫描**：规则引擎预筛 → Top N；默认 **Gemini 2.5 Pro 批量简评**（走势一句话 + LLM 综合分）
- **单票研报**：默认 **Gemini 深度研究**（股价走势、技术/财务/资讯打分、买卖价）
- **规则引擎**：`--no-llm` 可完全关闭 LLM

## 依赖

`zplan-共享` 与 `zplan-资讯/zplan.db`；LLM 需在 `zplan-资讯/.env` 配置：

```bash
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-pro   # 可选，默认即此模型
```

## 使用

```bash
./scripts/bootstrap_env.sh

# 成本估算（不调用 API）
.venv/bin/python main.py --estimate-cost --top 20

# 全市场扫描 Top20（含 LLM 简评，1 次 API）
.venv/bin/python main.py --top 20

# 单票研报（默认 LLM）
.venv/bin/python main.py -s 爱普股份

# 仅规则引擎
.venv/bin/python main.py -s 爱普股份 --no-llm

# JSON 导出
.venv/bin/python main.py -s 603020 --format json -o report.json
```

配置：`config/strategy.yaml` → `llm.enabled` / `llm.scan_brief`

## Gemini 2.5 Pro 成本粗算（2026-05）

| 场景 | 约 tokens | 约 USD | 约 CNY |
|------|-----------|--------|--------|
| 单票深度研报 | 6.5K | $0.026 | ¥0.18 |
| 扫描 Top20 简评（1 次批量） | 7.7K | $0.034 | ¥0.25 |
| Top20 每只深度研报 | 130K | $0.51 | ¥3.7 |

实际以响应末尾 `usage` 或 `--estimate-cost` 为准。免费档常见 **~20 次/天**，超出需计费。

Monorepo 烟测：`./scripts/smoke.sh`（使用 `--no-llm` 保证无 Key 也能过）

## 打分与研报存储（默认开启）

每次扫描 / 单票研报写入 **`zplan.db`** 表 `pick_runs`、`pick_entries`（含日期、规则分、LLM 分、**analysis_process_json** 分析过程、完整 **report_json** / **markdown**）。

```bash
# 跑完后会打印 run_id
.venv/bin/python main.py --top 20
.venv/bin/python main.py -s 爱普股份

# 查看历史运行
.venv/bin/python main.py --list-runs 20

# 某次扫描/研报详情（含每只 entry_id）
.venv/bin/python main.py --show-run 12

# 查看完整研报 Markdown
.venv/bin/python main.py --show-entry 45

# 某只股票历次打分
.venv/bin/python main.py --history 603020

# 不入库
.venv/bin/python main.py --top 20 --no-save
```

也可用 SQLite 客户端直接查：`zplan-资讯/zplan.db`。

每条 `pick_entries` 会保存 **预测买入价 / 目标价 / 止损**（规则或 LLM）。事后用 **回测 Agent** 对比实际走势并生成校准建议：

```bash
cd ../zplan-回测
.venv/bin/python main.py iterate verify          # 每日闭环（推荐）
.venv/bin/python main.py iterate full            # 每周：选股+验证
.venv/bin/python main.py iterate history         # 看各轮 fail_rate 走势
```

可与 `watch daily` 串联：选股入库 → `iterate verify` → 按 Review 改 prompt/strategy → `iterate full` 开下一轮。

## 企业微信 / 微信一句话选股

在 `zplan-资讯` 侧已接入（与「最新 / 7天 / 资讯问答」同一入口）：

| 用户发送 | 行为 |
|----------|------|
| `选股 爱普股份` / `打分 603020` / `分析 爱普` | 规则打分 + 可选 Gemini 简评，回复摘要并入库 |
| `爱普股份`（短名称，无问句词） | 同上 |
| `筛选 脑机接口` / `题材 脑机` | 按概念成份列表（需先 `screen sync-concept`） |
| `帮助` | 含选股指令说明 |

本地调试：

```bash
cd ../zplan-资讯
.venv/bin/python -c "from wechat_interact import handle_inbound_text; print(handle_inbound_text('选股 爱普股份')['reply_text'])"

# HTTP 桥（OpenClaw / 自建网关）
.venv/bin/python openclaw_bridge.py wechat-serve
# POST {"text":"选股 爱普股份","push":false}  → /v1/wechat/reply
```

环境变量（`zplan-资讯/.env`）：

- `PICK_WECHAT_USE_LLM=false` — 仅规则分，回复更快
- `PICK_WECHAT_FULL_RESEARCH=true` — 单票走完整深度研报（慢、耗 API）

企业微信应用回调：配置 `wework` 后用户在企业微信里直接发消息即可（见 `zplan-资讯/README`）。

## 持仓订阅（每日简报 + 自动更新行情）

订阅后，每日任务会：**同步日线/分时** → **资讯补链** → **规则+LLM 简评** → 入库 + 写入 `zplan-资讯/pick_digest/YYYY-MM-DD.md`。

```bash
# 加入持仓（名称或代码）
.venv/bin/python main.py watch add 爱普股份
.venv/bin/python main.py watch add 同有科技 --note "仓位A"

.venv/bin/python main.py watch list

# 立即跑一轮每日简报（默认同步东财行情）
.venv/bin/python main.py watch daily

# 仅简报、不拉行情（库内已有最新 K 线时）
.venv/bin/python main.py watch daily --skip-sync

# 定时（crontab 示例，工作日 18:30）
# 30 18 * * 1-5 cd /path/zplan-选股 && ./scripts/daily_watchlist.sh
```

查看历史：`main.py --list-runs` 中 `watchlist_daily`；`--show-run <id>` 的 `summary.digest_markdown` 含全文。

## 全市场规则分 + Top300 LLM（`stock_rule_scores`）

两步流水线：先**全市场向量化规则分**落表，再对**规则分前 300** 做深度规则复核 + Gemini 简评入库。

```bash
# 1）全市场规则分 → 表 stock_rule_scores（约 1–3 分钟，无 LLM）
.venv/bin/python main.py init-rule

# 2）取规则分 Top300 → 深度规则 + LLM 分批简评（约 10 次 API，默认每批 30 只）
.venv/bin/python main.py llm-top --top 300

# 一步完成
.venv/bin/python main.py pipeline --top 300

# 仅深度规则、不调 LLM
.venv/bin/python main.py llm-top --top 300 --no-llm

# 查看：pick_runs.run_kind=llm_top300
.venv/bin/python main.py --list-runs
.venv/bin/python main.py --show-run <run_id>
```

规则分表字段：`ts_code`、`composite_score`、`rank_by_composite`、`trade_date_as_of`、`rule_version`。可用 SQLite 直接查 `stock_rule_scores`。

**推荐顺序（规则分打全 → Top300 LLM）**

```bash
# 1）补齐最新交易日截面（约 3600 只缺行时；限速约 2s/只，需数小时）
cd ../zplan-股价 && .venv/bin/python main.py --catch-up-panel

# 2）全市场规则分
cd ../zplan-选股 && .venv/bin/python main.py init-rule

# 3）规则分全局 Top300 + LLM（约 ¥3，10 次 API）
.venv/bin/python main.py llm-top --top 300
```

**Top300 LLM 费用粗算**（Gemini 2.5 Pro，10 批 × 30 只）：约 **≈ $0.44（≈ ¥3.1）**；Top30 深度研报另约 **≈ $0.77（≈ ¥5.5）**。

### 一键全流程（规则全打 → Top300 简评 → Top30 深度）

```bash
# 推荐：先补股价截面（可选，耗时长）
cd ../zplan-股价 && .venv/bin/python main.py --catch-up-panel

# 三步一条命令（规则 init-rule + llm-top 300 + 深度 30）
cd ../zplan-选股
.venv/bin/python main.py pipeline-full --top 300 --deep-top 30

# 或把补齐截面写进同一条（仍会跑很久）
.venv/bin/python main.py pipeline-full --catch-up-panel --top 300 --deep-top 30

# 已有 llm-top run 后只补深度研报
.venv/bin/python main.py deep-top --top 30 --from-run <run_id>
```

整链费用粗算：**≈ $1.2（≈ ¥8–9）**（简评 $0.44 + 深度 30×$0.026）。

### 导出 Top300 LLM 简评 Excel

```bash
.venv/bin/python main.py export-top --run-id 8
# 默认输出：zplan-资讯/pick_exports/llm_top300_run8_YYYYMMDD.xlsx
.venv/bin/python main.py export-top -o ~/Desktop/top300.xlsx
```

含：排名、板块、股价、规则分、LLM 分、操作建议、走势简评等。

## 条件筛选（题材 / 行业）

```bash
# 同步东财「脑机接口」概念成份（首次需网络，写入 stock_concept_members）
.venv/bin/python main.py screen sync-concept 脑机接口

# 按题材筛选 + 导出
.venv/bin/python main.py screen run --concept 脑机接口 -o /tmp/脑机接口.xlsx

# 题材 + 规则分≥60 + 附加 run8 的 LLM 分；剔除 20日涨幅>15%（防追高）
.venv/bin/python main.py screen run --concept 脑机 --min-rule-score 60 --max-ret-20d 15 --llm-run-id 8

# 按行业
.venv/bin/python main.py screen run --industry 医疗服务

.venv/bin/python main.py screen concepts 脑   # 已缓存概念名列表
```

**说明**：LLM/规则分是**截面质量与动量**排序，不是「未来涨跌预测」；Top 榜常含近期已大涨标的，需结合 `--max-ret-20d` 或人工复核。
