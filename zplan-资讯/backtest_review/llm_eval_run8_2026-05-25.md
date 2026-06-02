# LLM Top3 回测诊断（run_id=8，as_of=2026-05-21）

- 样本：**3** | 失败 **3** | 通过 **0** | 待验证 **0**
- 失败率：**100%**

## 失败标签统计
- `momentum_chase`（20日涨幅过高仍强推（追高风险））：**3** 次
- `score_inflation`（LLM 分显著高于规则分且理由空洞）：**3** 次
- `buy_unreachable`（收盘价远高于建议买价，短期难回踩成交）：**3** 次
- `forward_loss`（验证期内收盘收益为负）：**3** 次
- `over_recommendation`（推荐档位偏积极（推荐/积极关注）但存在多项风险）：**3** 次
- `generic_bullish`（趋势描述为套话，未引用具体信号）：**1** 次

## 逐只明细
- **#1 昭衍新药 (603127)** verdict=fail | LLM 92.0 vs 规则 84.9 | ret20=13.5688% | 收盘/买价 gap=18.8056% | fwd=-3.3201%
  - 推荐：积极关注 | 标签：momentum_chase, score_inflation, buy_unreachable, forward_loss, over_recommendation
  - LLM：技术指标强势，均线多头排列，呈上行趋势。
- **#2 百奥赛图 (688796)** verdict=fail | LLM 92.0 vs 规则 84.6 | ret20=8.049% | 收盘/买价 gap=18.5476% | fwd=-1.1817%
  - 推荐：推荐 | 标签：momentum_chase, score_inflation, buy_unreachable, forward_loss, over_recommendation
  - LLM：技术面满分，均线多头排列，短期上涨趋势强劲。
- **#3 五方光电 (002962)** verdict=fail | LLM 92.0 vs 规则 84.6 | ret20=8.9049% | 收盘/买价 gap=20.6495% | fwd=-3.6059%
  - 推荐：积极关注 | 标签：momentum_chase, score_inflation, generic_bullish, buy_unreachable, forward_loss, over_recommendation
  - LLM：技术形态强劲，均线多头排列，呈突破上涨趋势。

## 后续优化（改哪里）
### 1. Prompt（`llm_research.py` 简评/深度）— **优先**
- 简评 prompt：必须引用 ret_20d；若 ret_20d>8% 须写追高风险，composite_score 不得高于规则分+3，recommendation 最高「观望」
- 简评 prompt：composite_score 默认=规则分；仅当 signals 含具体突破/放量时最多+5；vs_rule_engine 须说明加分/减分理由，禁止「符合规则引擎高分」
- 深度研报 prompt：buy_price 不得高于 close*0.99；须说明与 suggested_buy 关系
- 简评 prompt：recommendation 与风险挂钩——存在 momentum_chase 或 buy_unreachable 时不得输出「推荐/积极关注」
- 当前 LLM 平均分比规则高 7.3 分，prompt 加硬约束：「你的 composite_score 中位数应接近规则分，勿集体抬到 90+」
### 2. strategy.yaml（规则过滤，不耗 API）
- strategy.yaml filters.max_ret_20d: 12（规则层直接剔除过热标的）
- technical.suggested_price_levels：MA20 折扣由 0.98 调至 0.96，或增加「可成交价」字段
### 3. 规则引擎代码（`technical.py` / `scanner.py`）
- scanner/llm_top300：ret_20d>8% 时 composite 上限 75

## 请你 Review（可勾选后改配置）

| # | 层级 | 文件 | 建议动作 | 原因 |
|---|------|------|----------|------|
| 1 | prompt | `zplan-选股/src/pick_agent/llm_research.py → _LLM_BRIEF_RULES` | 收紧简评：默认分=规则分，禁止套话，有追高风险必须降 recommendation | score_inflation=3 generic=1 |
| 2 | strategy | `zplan-选股/config/strategy.yaml → filters.max_ret_20d` | 启用或调低 max_ret_20d（如 10），减少过热股进入 Top300 | momentum_chase=3 |
| 3 | strategy | `zplan-选股/config/strategy.yaml → ranking` | 试 ranking.mode=blend 且 llm_weight 0.8；或 prompt 要求 close_vs_buy_gap 大时降分 | buy_unreachable=3 |
| 4 | rule_engine | `zplan-共享/src/zplan_shared/features.py → suggested_price_levels` | MA20 折扣 0.98→0.96，或增加 actionable_price≈close 供 LLM 参考 | 建议买价系统性低于现价，导致「买不到」 |
| 5 | prompt | `zplan-选股/src/pick_agent/llm_research.py` | recommendation 与 risk 挂钩：有 buy_unreachable/momentum 时最高「观望」 | over_recommendation=3 |
| 6 | strategy | `zplan-选股/config/strategy.yaml → ranking` | 确认 ranking.mode=llm_primary、resort_after_llm=true；回测后微调 llm_weight | 最终 Top 应由 LLM 分排序，规则只做候选池与约束 |
| 7 | workflow | `重跑 llm-top → llm-eval` | 改完配置/prompt 后必须重新 llm-top 生成新 run，再 llm-eval 对比 fail_rate | 旧 run 不会自动应用新 prompt |

## 设计说明（规则 vs LLM）

- **init-rule**：全市场轻量技术分（`quick_technical_score`），偏动量/多头排列，**不含**财报/资讯。
- **llm-top deepen**：对 Top300 才算完整规则分（技术+财务+资讯+行业）。
- **你的目标**：规则只做「候选池 + 硬约束」，**排序与推荐以 LLM 为主**（`strategy.yaml` → `ranking`）。
- **校正闭环**：改 prompt/权重 → 重跑 llm-top → `llm-eval` → 看 fail_rate 与上表。