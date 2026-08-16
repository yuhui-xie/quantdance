# 多头排列策略说明

本文档描述项目中的 `bullish_alignment` 策略。该策略属于趋势跟随类：当短中长期均线呈
**多头排列**（短 > 中 > 长）时满仓买入，排列走坏时卖出离场。

实现代码：`backend/app/strategies/bullish_alignment.py`  
因子实现：`backend/app/factors/bullish_alignment.py`

## 1. 核心思想

多头排列是经典技术面看涨形态：短期均线在中期之上、中期在长期之上，说明价格处于
逐级抬升的上升趋势。本策略把这种排列量化为 `[0, 1]` 的强度分数——满足“短 > 中 > 长”
的相邻均线对所占比例，再用带滞回的上下阈值把它切换为持仓状态。

## 2. 因子定义

设收盘价序列为 `C_t`，按周期升序排列的均线为 `MA_{p_1} <= MA_{p_2} <= ... <= MA_{p_k}`。
相邻均线对共有 `k-1` 个，多头排列强度为

$$
\text{score}(t) = \frac{1}{k-1}\sum_{i=1}^{k-1} \mathbb{1}\!\left[MA_{p_i}(t) > MA_{p_{i+1}}(t)\right]
$$

`score = 1` 表示完全多头排列，`score = 0` 表示完全空头排列（长 > 中 > 短）。
最长均线尚未形成，或任一均线窗口含 NaN/Inf 时分数为 NaN。

## 3. 信号规则

采用带滞回（hysteresis）的双阈值，避免排列临界处反复切换：

- **买入**：空仓时 `score(t) >= enter_threshold`（默认 1.0，即完全多头排列）
- **卖出**：持仓时 `score(t) <= exit_threshold`（默认 0.5，即多数排列走坏）

## 4. `strategy_params`

| 参数 | 示例值 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `periods` | `[5, 10, 20]` | `(5, 10, 20)` | 至少 2 个互异正整数 | 按升序排列的均线周期 |
| `enter_threshold` | `1.0` | 1.0 | 0~1 | 空仓转持仓的强度阈值 |
| `exit_threshold` | `0.5` | 0.5 | 0~1 | 持仓转空仓的强度阈值 |

要求 `exit_threshold < enter_threshold`，以避免持仓状态反复切换。

最少 K 线数：`max(periods)`。

## 5. 适用场景与局限

- **适用**：趋势较强、均线有序发散的上涨行情。
- **局限**：均线滞后，趋势转折初期反应偏慢；横盘震荡时排列频繁切换，需靠滞回阈值控制。
- 默认 `enter_threshold=1.0` 只要求在完全对齐时入场，过滤弱排列；`exit_threshold=0.5`
  允许一定程度的松动后才离场。

## 6. 运行示例

```bash
cd backend
python -m app.script backtest --strategy bullish_alignment \
  --strategy_params '{"periods": [5, 10, 20], "enter_threshold": 1.0, "exit_threshold": 0.5}'
```
