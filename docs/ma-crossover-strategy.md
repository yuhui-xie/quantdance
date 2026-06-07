# 双均线交叉策略说明

本文档描述项目中的 `ma_crossover` 策略。该策略属于趋势跟随类：用收盘价的快慢简单移动平均（SMA）交叉产生全仓买卖信号。

实现代码：`backend/app/strategies/ma_crossover.py`  
回测示例：`backend/examples/backtest_ma_crossover.json`

## 1. 核心思想

短期均线反映近期价格重心，长期均线反映更长周期趋势。快线上穿慢线（金叉）视为趋势转多；快线下穿慢线（死叉）视为趋势转空。

默认参数示例：

```json
{
  "fast_period": 10,
  "slow_period": 30
}
```

## 2. 变量定义

设收盘价序列为 `C_t`，快慢周期分别为 `N_f`、`N_s`。

### 简单移动平均（SMA）

$$
SMA_f(t) = \frac{1}{N_f}\sum_{k=0}^{N_f-1} C_{t-k}
$$

$$
SMA_s(t) = \frac{1}{N_s}\sum_{k=0}^{N_s-1} C_{t-k}
$$

仅当 `t >= N_s - 1` 时两条均线同时有效。

## 3. 信号规则

与实现一致，使用当前与前一根比较：

- **买入**：`SMA_f(t) > SMA_s(t)` 且 `SMA_f(t-1) <= SMA_s(t-1)`
- **卖出**：`SMA_f(t) < SMA_s(t)` 且 `SMA_f(t-1) >= SMA_s(t-1)`

## 4. 参数说明

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `fast_period` | 10 | 2~200 | 短期均线周期 |
| `slow_period` | 30 | 3~400 | 长期均线周期，必须大于 `fast_period` |

公共回测参数：`initial_cash`、`commission`（见 [策略总览](./strategy-guide.md)）。

最少 K 线数：`slow_period`。

## 5. 适用场景与局限

- **适用**：中长趋势较明显、方向持续性较好的行情。
- **局限**：震荡市容易来回打脸；均线滞后，转折点常“后知后觉”。

## 6. 调参建议

- 趋势更强时可拉大快慢线差距（如 10/40、20/60）降低噪音。
- 若交易频率过高，可同时上调快慢周期。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --request examples/backtest_ma_crossover.json
```
