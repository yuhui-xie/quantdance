# RSI 反转策略说明

本文档描述项目中的 `rsi_reversal` 策略。该策略属于振荡反转类：使用 Wilder 平滑 RSI，从超卖区上穿买入、从超买区下穿卖出。

实现代码：`backend/app/strategies/rsi_reversal.py`

## 1. 核心思想

RSI 衡量一段时间内涨跌动能的相对强弱。策略在 RSI 离开超卖区时做多、离开超买区时平仓/做空信号（本引擎为全仓买卖），适合震荡市中的超跌反弹与超涨回落。

默认参数示例：

```json
{
  "period": 14,
  "oversold": 30,
  "overbought": 70
}
```

## 2. 变量定义

设周期为 `N`，价格变化量：

$$
\Delta_t = C_t - C_{t-1}\quad (t \ge 1)
$$

$$
Gain_t = \max(\Delta_t, 0),\quad Loss_t = \max(-\Delta_t, 0)
$$

### Wilder 初始化（首个有效 RSI 在 `t = N`）

$$
AvgGain_N = \frac{1}{N}\sum_{i=1}^{N} Gain_i
$$

$$
AvgLoss_N = \frac{1}{N}\sum_{i=1}^{N} Loss_i
$$

### Wilder 递推（`t > N`）

$$
AvgGain_t = \frac{(N-1)\cdot AvgGain_{t-1} + Gain_t}{N}
$$

$$
AvgLoss_t = \frac{(N-1)\cdot AvgLoss_{t-1} + Loss_t}{N}
$$

### RSI

- 若 `AvgLoss_t = 0`：`RSI_t = 100`（当 `AvgGain_t > 0`），否则 `RSI_t = 50`
- 否则：

$$
RS_t = \frac{AvgGain_t}{AvgLoss_t},\quad RSI_t = 100 - \frac{100}{1 + RS_t}
$$

## 3. 信号规则

- **买入**：`RSI_t > oversold` 且 `RSI_{t-1} <= oversold`
- **卖出**：`RSI_t < overbought` 且 `RSI_{t-1} >= overbought`

## 4. 参数说明

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `period` | 14 | 2~200 | RSI 周期 |
| `oversold` | 30 | 1~50 | 超卖阈值 |
| `overbought` | 70 | 50~99 | 超买阈值，必须大于 `oversold` |

公共回测参数：`initial_cash`、`commission`（见 [策略总览](./strategy-guide.md)）。

最少 K 线数：`period + 5`。

## 5. 适用场景与局限

- **适用**：震荡市中的超跌反弹 / 超涨回落。
- **局限**：强趋势里 RSI 可长期钝化，逆势信号连续失败。

## 6. 调参建议

- 若信号过多可把阈值调整为 `25/75` 或 `20/80`。
- 若信号过少可缩窄阈值区间，但需关注回撤变化。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --strategy rsi_reversal --symbol 600000 \
  --start-date 2023-01-01 --end-date 2024-12-31
```
