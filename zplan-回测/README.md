# zplan-回测（回测 Agent）

独立工作区。只读 `zplan_shared.market`；将 **选股时的预测买入价** 与 **后续实际行情** 比对，支持历史回填与持续跟踪（见 `pick_prediction_outcomes`）。

```bash
./scripts/bootstrap_env.sh

# 烟测行情
.venv/bin/python main.py smoke --code 000001

# 验证选股预测（默认 horizon 5/10/20 交易日，自动回填缺失预测价）
.venv/bin/python main.py validate
.venv/bin/python main.py validate --run-id 12 --horizon 10

# 校准报告（偏差统计 + 优化建议）
.venv/bin/python main.py calibrate --horizon 10

# LLM Top10 失败诊断（针对 llm_top300，含改 prompt / strategy 建议）
.venv/bin/python main.py llm-eval --run-id 8 --top 10

# 行情完整性 + 上次打分偏差综合审计（写入 backtest_review/）
.venv/bin/python main.py check-data
.venv/bin/python main.py audit --run-id 8 --top 10
```

## 迭代闭环（推荐）

```bash
# 每日：验证已有选股 run，记录指标并与上轮对比
.venv/bin/python main.py iterate verify
bash scripts/iterate_loop.sh verify

# 每周：补齐行情 → init-rule → llm-top → 验证
.venv/bin/python main.py iterate full

# 历史与对比
.venv/bin/python main.py iterate history
.venv/bin/python main.py iterate diff
```

记录目录：`zplan-资讯/backtest_review/iterations/`（JSONL + 每轮快照）

crontab 示例（工作日 18:00 验证，周一 17:00 全量）：

```cron
0 18 * * 1-5 cd /path/zplan-回测 && ./scripts/iterate_loop.sh verify
0 17 * * 1 cd /path/zplan-回测 && ./scripts/iterate_loop.sh full
```

```bash
# 最近验证明细
.venv/bin/python main.py list --horizon 10 --limit 20
```

**推荐工作流**：每日选股入库后执行 `validate`；定期看 `calibrate` 调整 `technical.price_levels` / LLM 提示。数据架构见 `zplan-共享/docs/DATA_ARCHITECTURE.md`。
