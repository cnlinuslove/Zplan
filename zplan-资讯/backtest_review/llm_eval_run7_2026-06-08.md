# LLM Top10 回测诊断（run_id=7，as_of=2026-05-20）

- 样本：**5** | 失败 **5** | 通过 **0** | 待验证 **0**
- 失败率：**100%**

## 失败标签统计
- `momentum_chase`（20日涨幅过高仍强推（追高风险））：**5** 次
- `buy_unreachable`（收盘价远高于建议买价，短期难回踩成交）：**5** 次
- `forward_loss`（验证期内收盘收益为负）：**3** 次

## 逐只明细
- **#1 尚太科技 (001301)** verdict=fail | LLM None vs 规则 84.3 | ret20=17.0216% | 收盘/买价 gap=23.3832% | fwd=7.0382%
  - 推荐：None | 标签：momentum_chase, buy_unreachable
- **#2 华宏科技 (002645)** verdict=fail | LLM None vs 规则 84.0 | ret20=34.9619% | 收盘/买价 gap=42.2296% | fwd=2.8562%
  - 推荐：None | 标签：momentum_chase, buy_unreachable
- **#3 粤海饲料 (001313)** verdict=fail | LLM None vs 规则 84.0 | ret20=16.1329% | 收盘/买价 gap=20.4182% | fwd=-11.8488%
  - 推荐：None | 标签：momentum_chase, buy_unreachable, forward_loss
- **#4 滨海能源 (000695)** verdict=fail | LLM None vs 规则 78.2 | ret20=8.0053% | 收盘/买价 gap=13.2168% | fwd=-2.0383%
  - 推荐：None | 标签：momentum_chase, buy_unreachable, forward_loss
- **#5 伟隆股份 (002871)** verdict=fail | LLM None vs 规则 73.0 | ret20=19.4185% | 收盘/买价 gap=26.2761% | fwd=-12.9536%
  - 推荐：None | 标签：momentum_chase, buy_unreachable, forward_loss

## 后续优化（改哪里）
### 1. Prompt（`llm_research.py` 简评/深度）— **优先**
- 简评 prompt：必须引用 ret_20d；若 ret_20d>8% 须写追高风险，composite_score 不得高于规则分+3，recommendation 最高「观望」
- 深度研报 prompt：buy_price 不得高于 close*0.99；须说明与 suggested_buy 关系
### 2. strategy.yaml（规则过滤，不耗 API）
- strategy.yaml filters.max_ret_20d: 12（规则层直接剔除过热标的）
- technical.suggested_price_levels：MA20 折扣由 0.98 调至 0.96，或增加「可成交价」字段
### 3. 规则引擎代码（`technical.py` / `scanner.py`）
- scanner/llm_top300：ret_20d>8% 时 composite 上限 75

## 请你 Review（可勾选后改配置）

| # | 层级 | 文件 | 建议动作 | 原因 |
|---|------|------|----------|------|
| 1 | strategy | `zplan-选股/config/strategy.yaml → filters.max_ret_20d` | 启用或调低 max_ret_20d（如 10），减少过热股进入 Top300 | momentum_chase=5 |
| 2 | strategy | `zplan-选股/config/strategy.yaml → ranking` | 试 ranking.mode=blend 且 llm_weight 0.8；或 prompt 要求 close_vs_buy_gap 大时降分 | buy_unreachable=5 |
| 3 | rule_engine | `zplan-共享/src/zplan_shared/features.py → suggested_price_levels` | MA20 折扣 0.98→0.96，或增加 actionable_price≈close 供 LLM 参考 | 建议买价系统性低于现价，导致「买不到」 |
| 4 | strategy | `zplan-选股/config/strategy.yaml → ranking` | 确认 ranking.mode=llm_primary、resort_after_llm=true；回测后微调 llm_weight | 最终 Top 应由 LLM 分排序，规则只做候选池与约束 |
| 5 | workflow | `重跑 llm-top → llm-eval` | 改完配置/prompt 后必须重新 llm-top 生成新 run，再 llm-eval 对比 fail_rate | 旧 run 不会自动应用新 prompt |

## 设计说明（规则 vs LLM）

- **init-rule**：全市场轻量技术分（`quick_technical_score`），偏动量/多头排列，**不含**财报/资讯。
- **llm-top deepen**：对 Top300 才算完整规则分（技术+财务+资讯+行业）。
- **你的目标**：规则只做「候选池 + 硬约束」，**排序与推荐以 LLM 为主**（`strategy.yaml` → `ranking`）。
- **校正闭环**：改 prompt/权重 → 重跑 llm-top → `llm-eval` → 看 fail_rate 与上表。