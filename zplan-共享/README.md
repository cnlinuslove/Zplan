# zplan-共享

各 Agent 共用的配置、SQLAlchemy 模型、**行情只读 API**（`zplan_shared.market`）与 AkShare 股价 ETL。

## 必读文档

- **[docs/DATA_ARCHITECTURE.md](docs/DATA_ARCHITECTURE.md)** — 行情数据中心架构（Phase A 及后续规划）
- 仓库根目录 **[../AGENTS.md](../AGENTS.md)** — 中文目录与各 Agent 职责

## 约定

- 数据根目录：monorepo 下 `zplan-资讯/`（可通过 `ZPLAN_ROOT` 覆盖）
- 安装：`pip install -e .` 或各 Agent 的 `scripts/bootstrap_env.sh`

## 行情查询（各 Agent 只读）

```python
from zplan_shared.market import get_bars, get_panel, latest_trade_date, as_of_close
```

写入 `daily_prices` **仅**由 `zplan-股价` 通过 `zplan_shared.etl_akshare` 完成。
