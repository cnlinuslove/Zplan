# Z-Plan

Phase 1：资讯 Agent（本地持续运行，每 2 小时抓取/汇总/推送）。

**本机 Python 约定：** 依赖只装进项目 **`.venv`**。不要用系统自带的 `/usr/bin/python3` 直接跑本项目（会缺包）。

- **用 Cursor / VSCode 打开本仓库**：已配置 **`.vscode/settings.json`** —— 默认解释器为 `.venv/bin/python`，**新开集成终端**时会自动激活虚拟环境，一般可直接输入 `python …`（无需每次 `source .venv/bin/activate`）。
- **系统自带终端（Terminal.app / iTerm）**：可选安装 [direnv](https://direnv.net/)，在本目录执行一次 `direnv allow` 后，**cd 进项目**会自动把 `python` 指到 `.venv`（见根目录 `.envrc`）。不装 direnv 时，仍可用 **`./.venv/bin/python …`** 或手动 `source .venv/bin/activate`。

**一键初始化（推荐）：** 在项目根目录执行 **`./scripts/bootstrap_env.sh`**（有 `uv` 时会 `uv venv` + `uv pip sync requirements.txt`；无 `uv` 时用 `python3 -m venv` + pip）。

## 架构边界

- Z-Plan：Python + **SQLite（默认，WAL）或 PostgreSQL（可选）**，负责 ETL、摘要、历史检索。
- OpenClaw：只做调度编排、消息路由、微信交互入口。
- ETL 与 Agent 解耦：数据库做中转层，后续股价/投资/回测 Agent 可直接复用。

### 多 Agent 并行开发

| 目录 | 说明 | 单独打开 |
|------|------|----------|
| `zplan/` | 资讯 Agent + 微信/OpenClaw 入口 | 本仓库根目录 |
| `zplan-共享/` | 配置、ORM、`market` 查询、`daily_prices` ETL | 随各 Agent 依赖 |
| `zplan-股价/` | 股价 Agent（**唯一写入**行情） | File → Open Folder |
| `zplan-选股/` | 选股 Agent | 同上 |
| `zplan-回测/` | 回测 Agent | 同上 |

行情存储架构：**[`../zplan-共享/docs/DATA_ARCHITECTURE.md`](../zplan-共享/docs/DATA_ARCHITECTURE.md)**（各 Agent 必读）。

一次打开全部：用 **`zplan.code-workspace`**（多根工作区）。各 Agent 目录有独立 `.venv`；共享 `.env` 与 `zplan.db` 仍在 `zplan/`。

## 快速开始

1. 安装依赖（项目虚拟环境 `.venv`）

   `./scripts/bootstrap_env.sh`

   手动等价流程（有 uv）：`uv venv` → `uv pip sync requirements.txt --python .venv/bin/python`（`sync` 会按清单增删包，与 `pip install -r` 仅追加不同。）

   也可：`make bootstrap`。

   下文步骤 3 起：在 **Cursor 集成终端**里通常已自动激活，可直接 `python …`；否则请 **`source .venv/bin/activate`** 或使用 **`.venv/bin/python`**。

2. 配置环境变量

   `cp .env.example .env`

3. 启动本地常驻任务（2 小时一轮）

   `python daily_update.py`

4. 启动单轮测试（不常驻）

   `python daily_update.py --once`

5. 一键烟测（推荐每次改动后执行）

   `python smoke_test.py`

6. 网络与 X 可达性诊断（拿不到真实推文时先跑这个）

   `python openclaw_bridge.py diag`

   Gemini 连通性（需已配置 `GEMINI_API_KEY`，成功为 exit 0）：

   `python openclaw_bridge.py gemini-check`

7. 自动探测本机常见 HTTP 代理端口（可选）

   `python proxy_probe.py`

8. 一键探测（含 lsof 扫描 + SOCKS 尝试，OpenClaw 友好）

   `python openclaw_bridge.py probe`

   若发现可用代理并写入 `.env`：

   `python openclaw_bridge.py probe --write-env`

## 拿不到 X 真实推文时（排障）

1. 安装 PAC 支持（谢公屐等「仅 PAC、远端 HTTPS 代理」依赖 JS 解析）：

   `uv pip install -r requirements.txt`（或 `pip install pypac dukpy`）

2. 运行 `python openclaw_bridge.py diag`，查看：
   - `x_outbound_mode`：`pac` 表示已用 **pypac + PAC**；`env_proxy` 表示走 `HTTP(S)_PROXY`；`direct_no_pypac` 表示未装 pypac。
   - `can_reach_x_api`：必须为 `true` 才会拉真实推文（否则整轮会占位）。
3. 可选：在 `.env` 显式写 `PAC_URL=...`（不填则从系统 `scutil` 读取 PAC 地址）。
4. 若 PAC 对 `api.twitter.com` 仍返回 `DIRECT`：只能改 PAC/换节点，或在 VPN 中开启 **本地 HTTP/SOCKS 端口** 并配置 `HTTP_PROXY`/`HTTPS_PROXY`。
5. 有本地端口时可用 `python openclaw_bridge.py probe` 自动探测并 `--write-env`。

## Gemini 摘要失败时

1. 运行 `python openclaw_bridge.py gemini-check`（或 `make gemini-check`），查看 `http_status` 与 `error` 片段（403 多为 Key 无效/受限；超时多为访问不到 Google API）。
2. VPN 需能访问 **`generativelanguage.googleapis.com`**（与能否打开 X 无必然关系）。
3. **`GEMINI_API_KEY` 只放在本机 `.env`**，勿提交 Git、勿发到聊天；一旦外泄请到 [AI Studio](https://aistudio.google.com/apikey) **删除旧 Key 并新建**，再更新 `.env`。
4. **`run-once` 多 topic 连续打 Gemini 易 429**：已默认在 topic 之间与每次 HTTP 之间做节流（`.env` 中 `GEMINI_MIN_SECONDS_BETWEEN_TOPICS`、`GEMINI_MIN_SECONDS_BETWEEN_CALLS`）。若仍大量 429，多为 **免费档 RPM/RPD 或计费配额**，需在 Google AI / Cloud **开通计费或换档**，单靠加大休眠无法突破硬上限。
5. 报错 **`missing json object`**：多为返回体被安全策略清空或模型包了 markdown 围栏；已做 **markdown 剥离**、**安全阈值放宽**、**更大 `GEMINI_MAX_OUTPUT_TOKENS`**；仍失败请看日志里的 `text_head=` / `finishReason`。

## 数据存储与 Web 浏览

- **可靠性（SQLite）**：连接使用 **WAL + foreign_keys + busy_timeout**，单文件 `zplan.db` 可复制备份；启动时自动补建 `(topic_key, created_at)`、`(run_id, published_at)` 索引。
- **大容量（PostgreSQL）**：在 `.env` 设置 `DB_URL=postgresql+psycopg://...`（需已安装 `psycopg`）。本地可起库：`docker compose -f docker-compose.postgres.yml up -d`。
- **前端按 Topic 查看**：`make ui` 或 `./.venv/bin/streamlit run viewer_app.py`，浏览器打开提示的地址（默认 `http://localhost:8501`），侧栏选 topic，查看 `news_runs` 与对应 `news_items_raw`。
- **摘要与 X 费用**：每条帖子全文在 `news_items_raw.text`；配置 `GEMINI_API_KEY` 后摘要为「综述 + 要点」中文归纳。X 查询默认带营销排除词，可用 `X_QUERY_EXCLUDE_SUFFIX` 追加；A 股 topic 默认 `lang:zh` 以减噪。

## Phase 1 已实现能力

- 调度：固定 2 小时轮询（`NEWS_SCHEDULE_HOURS`）。
- 默认 topics（7 个）自动种子写入数据库。
- 主题动态配置：增删改查无需改代码（`topic_admin.py`）。
- 数据沉淀：
  - `news_runs(topic_key, window_start, window_end, summary, sentiment, created_at, dedupe_key)`
  - `news_items_raw(run_id, source, post_id, author, published_at, text, url)`
- 微信推送：`WECHAT_PUSH_WEBHOOK` 配置后自动推送每个 topic 最新 summary。
- 历史查询：支持“最新/7天/按 topic”（`history_query.py`）。
- X 抓取：有 `X_BEARER_TOKEN` 时走 X API；出站顺序为 **`HTTP(S)_PROXY` > `pypac`+PAC > 直连`**；不可达时可降级占位（`X_FAILOVER_TO_PLACEHOLDER`）。
- X 抓取策略：默认追加 `-is:retweet` 与 `lang:en OR lang:zh`，支持分页拉取与限流退避。

## 命令示例

### 1) topic 动态管理

- 列出 topic：

  `python topic_admin.py list`

- 新增 topic：

  `python topic_admin.py add --topic-key fed_rate --display-name "美联储利率" --query "Fed interest rate"`

- 更新 topic：

  `python topic_admin.py update --topic-key fed_rate --query "Fed policy OR Powell" --enabled true`

- 删除 topic：

  `python topic_admin.py delete --topic-key fed_rate`

### 2) 查询历史 summary

- 最新摘要（全部）：

  `python history_query.py --mode latest`

- 最近 7 天（按 topic）：

  `python history_query.py --mode 7d --topic trump_updates`

### 3) OpenClaw 对接命令（推荐）

- 触发单轮执行（返回 JSON）：

  `python openclaw_bridge.py run-once`

- 历史查询（返回 JSON）：

  `python openclaw_bridge.py history --mode latest`
  `python openclaw_bridge.py history --mode 7d --topic us_market_hotspots`

- topic 动态管理（返回 JSON）：

  `python openclaw_bridge.py topic --action list`
  `python openclaw_bridge.py topic --action add --topic-key fed_rate --display-name "美联储利率" --query "Fed policy OR Powell"`
  `python openclaw_bridge.py topic --action update --topic-key fed_rate --enabled false`

## X API 接入说明

- 在 `.env` 配置：
  - `X_BEARER_TOKEN`
  - `X_MAX_RESULTS_PER_PAGE`（单页条数）
  - `X_MAX_PAGES_PER_TOPIC`（每 topic 最大分页）
  - `X_RATE_LIMIT_SLEEP_SECONDS`（429 默认退避秒数）
  - `X_FAILOVER_TO_PLACEHOLDER`（X不可达时是否自动降级占位源）
- 程序行为：
  - 429 限流：读取 `x-rate-limit-reset`，自动休眠并重试（指数退避）
  - 5xx：自动重试
  - 4xx：快速失败并记录日志（便于修正 token/权限/查询语法）
  - 网络超时：可返回 `X_NETWORK_TIMEOUT`；若开启 failover 则自动降级占位抓取，保障任务不中断

### 代理网络（VPN）说明

- 若你本机开了代理但 Python 仍连不上 X，请在 `.env` 增加：
  - `HTTPS_PROXY=http://127.0.0.1:7890`（示例）
  - `HTTP_PROXY=http://127.0.0.1:7890`
- 连通性自检：
  - `curl -I https://api.twitter.com/2/tweets/search/recent`
  - `.venv/bin/python openclaw_bridge.py run-once`

## OpenClaw 错误映射

- `openclaw_bridge.py run-once` 在失败时返回：
  - `ok: false`
  - `error.code`（如 `X_AUTH_INVALID` / `X_QUERY_INVALID` / `X_RATE_LIMITED`）
  - `error.message`（中文可读提示）
  - `error.action`（下一步操作建议）
- 常见错误码：
  - `X_AUTH_INVALID`：token 无效或权限不足
  - `X_QUERY_INVALID`：topic 查询语法不合法
  - `X_RATE_LIMITED`：触发限流，需要等待窗口恢复
  - `X_SERVER_ERROR`：X 服务端异常
