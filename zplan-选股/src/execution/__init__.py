"""盘中执行层 — T-1 盘后推荐 → T 日盘中执行 → T+1 验证 滚动循环。

模块职责：
- plan: ExecutionPlan 数据模型，从 pick_entries 加载并贯穿执行全流程
- pre_market: T 日 8:28 盘前检查（隔夜外盘/新闻/买入价调整）
- auction: T 日 9:25 集合竞价快照 + 决策
- opening: T 日 9:30 开盘行动清单
- intraday: T 日盘中价位监控 + 触发提醒
- t1_planner: T+1 日规划（明日卖出计划 + 新一轮推荐预览）
"""
