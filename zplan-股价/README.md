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
```
