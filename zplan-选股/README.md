# zplan-选股（选股 Agent）

独立工作区。只读行情，统一使用 `zplan_shared.market`（见 `zplan-共享/docs/DATA_ARCHITECTURE.md`）。

依赖 `zplan-共享` 与 `zplan-资讯/zplan.db`。无数据时请先运行 **zplan-股价**。

```bash
./scripts/bootstrap_env.sh
.venv/bin/python main.py --top 20
```
