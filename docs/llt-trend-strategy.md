# LLT 趋势拐点策略说明

本文档描述单票 `llt_trend` 策略。策略只使用收盘价计算的低延迟趋势线
（Low-Lag Trendline，LLT），不叠加其他技术指标或基本面条件。

实现代码：`backend/app/strategies/llt_trend.py`  
回测示例：`backend/examples/backtest_llt_trend.json`

## 1. 信号规则

设 LLT 序列为 `L_t`，斜率回看天数为 `K`：

$$
D_t = L_t - L_{t-K}
$$

- **买入**：`D_t > 0` 且 `D_{t-1} <= 0`，即 LLT 由走平或下降转为上升。
- **卖出**：`D_t < 0` 且 `D_{t-1} >= 0`，即 LLT 由走平或上升转为下降。
- 前 `period` 根 K 线作为初始化阶段，不产生交易信号。

信号触发当根 K 线按收盘价全仓买入或全仓卖出。

## 2. 策略参数

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `period` | 20 | 2~400 | LLT 平滑周期；越大越平滑，信号越慢 |
| `slope_lookback` | 1 | 1~60 | 计算 LLT 斜率时回看的交易日数 |

最少 K 线数为 `period + slope_lookback + 1`。

## 3. 示例请求参数

对照 `backend/examples/backtest_llt_trend.json`。公共字段总表见
[策略总览](./strategy-guide.md)。

- `strategy_id`：`llt_trend`
- `symbol`：`600000`
- `start_date` / `end_date`：回测起止日期
- `initial_cash`：初始资金
- `commission`：买卖手续费率
- `output_options.plot`：权益曲线输出路径

## 4. 调参建议与局限

- 短周期 LLT 对转折更敏感，但震荡行情中容易频繁反向交易。
- 增大 `period` 或 `slope_lookback` 可以过滤噪声，但会推迟进出场。
- 该策略没有止损、趋势强度和成交量过滤，适合作为纯 LLT 基准策略。

## 5. 运行示例

```bash
cd backend
python -m app.script backtest --request examples/backtest_llt_trend.json
```
