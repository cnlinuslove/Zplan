# zplan-股价（股价 Agent）

独立工作区。负责 **唯一写入** 共享库 `daily_prices`（见 `zplan-共享/docs/DATA_ARCHITECTURE.md`）。

## 依赖

- `zplan-共享`（editable install）
- 数据与 `.env`：默认 `../zplan-资讯/`

## 命令

```bash
./scripts/bootstrap_env.sh
.venv/bin/python main.py
.venv/bin/python main.py --limit 5   # 调试

# 选股 init-rule 前：仅补齐缺「最新交易日」截面的股票（比全量增量更快）
.venv/bin/python main.py --catch-up-panel
.venv/bin/python main.py --catch-up-panel --limit 50   # 试跑

# 概念/题材（东财 F10 → stock_concept_members；只读：get_stock_concepts / get_concepts_panel）
.venv/bin/python scripts/sync_stock_concepts.py          # 全市场同步（约 30～60 分钟）
.venv/bin/python scripts/export_stock_concepts.py -o ../zplan-资讯/exports/stock_concepts.csv
# 按单个东财概念板同步成份：见 zplan-选股 main.py screen sync-concept <名称>
```

## 每日自动更新（macOS）

收盘后自动跑（已优化）：日线增量 → **自动截面补齐**（漏跑/隔日：缺「全库最新交易日」的票会多进程补拉）→ 补缺（单票失败不阻断）→ 近 8 日涨跌幅回填 → **技术指标快照** `daily_features` → 估值截面；**每周五**财报。选股优先 `get_features_panel()`。

环境变量：`DAILY_AUTO_CATCHUP_PANEL=false` 可关闭自动补齐；`CATCHUP_PANEL_WORKERS` 默认 6。

```bash
chmod +x scripts/run_daily_prices.sh scripts/install_daily_prices_launchagent.sh
./scripts/install_daily_prices_launchagent.sh
# 或从资讯目录：make -C ../zplan-资讯 install-daily-prices
```

默认 **周一至周五 17:35、08:05**（可在 `zplan-资讯/.env` 配置 `DAILY_PRICES_CRON_*`）。  
日志：`zplan-资讯/logs/cron_daily_prices.log`。

日更结束后自动执行选股 **`main.py pipeline --top 300`**（可在 `.env` 设 `DAILY_PRICES_TRIGGER_PICK=false` 关闭）。企微会推送开始/完成（需 `WECHAT_PUSH_WEBHOOK`）。

```bash
# 手动：股价完成后只跑选股
.venv/bin/python scripts/run_pick_after_prices.py --notify

# 等待当前 cron 跑完再触发（一次性）
./scripts/wait_prices_then_pick.sh
```
