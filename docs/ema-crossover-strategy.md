# 双 EMA 交叉策略说明

本文档描述项目中的 `ema_crossover` 策略。该策略属于趋势跟随类：用收盘价的快慢指数移动平均（EMA）交叉产生全仓买卖信号，比 SMA 对近期价格更敏感。

实现代码：`backend/app/strategies/ema_crossover.py`

## 1. 核心思想

EMA 对近期价格赋予更高权重，金叉/死叉通常比同等周期的 SMA 更早出现，也更容易在震荡中产生噪音交易。

默认参数示例：

```json
{
  "fast_period": 10,
  "slow_period": 30
}
```

## 2. 变量定义

设快慢周期为 `N_f`、`N_s`，平滑系数：

$$
\alpha_f = \frac{2}{N_f + 1},\quad \alpha_s = \frac{2}{N_s + 1}
$$

与 pandas `ewm(span=N, adjust=False)` 一致：

- 初值：`EMA_f(0) = C_0`，`EMA_s(0) = C_0`
- 递推：

$$
EMA_f(t) = \alpha_f \cdot C_t + (1 - \alpha_f) \cdot EMA_f(t-1)
$$

$$
EMA_s(t) = \alpha_s \cdot C_t + (1 - \alpha_s) \cdot EMA_s(t-1)
$$

## 3. 信号规则

- **买入**：`EMA_f(t) > EMA_s(t)` 且 `EMA_f(t-1) <= EMA_s(t-1)`
- **卖出**：`EMA_f(t) < EMA_s(t)` 且 `EMA_f(t-1) >= EMA_s(t-1)`

## 4. 参数说明

本策略暂无独立 example 文件；CLI 回测时 `strategy_params` 与下方一致。请求级公共字段（`data_source`、`symbol`、`start_date`、`end_date`、`initial_cash`、`commission`、`output_options` 等）见 [策略总览](./strategy-guide.md)。

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `fast_period` | 10 | 2~200 | 短期 EMA 周期 |
| `slow_period` | 30 | 3~400 | 长期 EMA 周期，必须大于 `fast_period` |

最少 K 线数：`slow_period`。

## 5. 适用场景与局限

- **适用**：与双均线类似，但更希望指标对近期价格变化更敏感。
- **局限**：比 SMA 交叉更灵敏，也可能带来更多噪音交易。

## 6. 调参建议

- 先以 `10/30` 起步，对比 `ma_crossover` 的交易次数与回撤。
- 若噪音偏大，可增大周期或与成交量条件组合过滤。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --strategy ema_crossover --symbol 600000 \
  --start-date 2023-01-01 --end-date 2024-12-31
```
