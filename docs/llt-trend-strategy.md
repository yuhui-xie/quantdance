# LLT 趋势拐点策略说明

本文档描述单票 `llt_trend` 策略。策略只使用收盘价计算的低延迟趋势线
（Low-Lag Trendline，LLT），不叠加其他技术指标或基本面条件。

实现代码：`backend/app/strategies/llt_trend.py`  
回测示例：`backend/examples/backtest_llt_trend.json`

## 1. LLT 序列计算

LLT 按经典二阶递推公式从收盘价 `P_t` 平滑得到趋势线 `L_t`。设平滑周期
`period`，令 `alpha = 2 / (period + 1)`：

$$
L_t =
\left(\alpha - \frac{\alpha^2}{4}\right)P_t
+ \frac{\alpha^2}{2}P_{t-1}
- \left(\alpha - \frac{3\alpha^2}{4}\right)P_{t-2}
+ 2(1-\alpha)L_{t-1}
- (1-\alpha)^2 L_{t-2}
$$

- 前两根有效 K 线以原值初始化：`L_t = P_t`（前两项）。
- 遇到 NaN/Inf 时输出 NaN，并在下一段有效数据重新初始化。
- `alpha` 越小（`period` 越大）曲线越平滑、滞后越大；相比同周期 EMA，
  LLT 通过二阶递推对近期价格变化更敏感、相位滞后更小。

实现见 `backend/app/factors/llt.py`。

## 2. 信号规则

设 LLT 序列为 `L_t`，斜率回看天数为 `K`：

$$
D_t = L_t - L_{t-K}
$$

- **买入**：`D_t > 0` 且 `D_{t-1} <= 0`，即 LLT 由走平或下降转为上升。
- **卖出**：`D_t < 0` 且 `D_{t-1} >= 0`，即 LLT 由走平或上升转为下降。
- 前 `period` 根 K 线作为初始化阶段，不产生交易信号。

信号触发当根 K 线按收盘价全仓买入或全仓卖出。

## 3. 策略参数

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `period` | 20 | 2~400 | LLT 平滑周期；越大越平滑，信号越慢 |
| `slope_lookback` | 1 | 1~60 | 计算 LLT 斜率时回看的交易日数 |
| `slope_threshold` | 0.0 | ≥0 | 斜率阈值（价格单位）；0 表示仅按过零拐点触发 |

最少 K 线数为 `period + slope_lookback + 1`。

> **进阶调参：斜率阈值过滤噪音。** `slope_threshold` 在拐点附近引入一段
> 死区：只有当斜率由 `≤ +threshold` 上穿至 `> +threshold` 时才买入，由
> `≥ -threshold` 下穿至 `<-threshold` 时才卖出，其余区间观望。因为 LLT
> 在趋势拐点附近斜率常在 0 值上下反复震荡，若不设阈值会频繁反向交易、
> 徒增手续费；设阈值后只捕捉足够强劲的趋势行情。注意阈值以**价格单位**
> 计，量级应视标的绝对股价而定（例如 10~30 元的股票，0.1~0.5 量级常见）。

## 4. 示例请求参数

对照 `backend/examples/backtest_llt_trend.json`。公共字段总表见
[策略总览](./strategy-guide.md)。

- `strategy_id`：`llt_trend`
- `symbol`：`600000`
- `start_date` / `end_date`：回测起止日期
- `initial_cash`：初始资金
- `commission`：买卖手续费率
- `output_options.report`：权益曲线输出路径

## 5. 调参建议与局限

- 短周期 LLT 对转折更敏感，但震荡行情中容易频繁反向交易。
- 增大 `period` 或 `slope_lookback` 可以过滤噪声，但会推迟进出场。
- 设 `slope_threshold` 可在不动周期的情况下过滤拐点噪音；代价是进出场
  更滞后、且错过小幅但连续的趋势（量级需随股价调整）。
- 该策略没有止损、趋势强度和成交量过滤，适合作为纯 LLT 基准策略。

## 6. 运行示例

```bash
cd backend
python -m app.script backtest --request examples/backtest_llt_trend.json
```
