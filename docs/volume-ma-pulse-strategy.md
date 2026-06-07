# 量比放量/缩量脉冲策略说明

本文档描述项目中的 `volume_ma_pulse` 策略逻辑。该策略属于量价突破类策略：先用成交活动量相对历史均量识别放量脉冲，再用价格均线、前高突破和 K 线方向过滤噪音，最后用缩量、止损或止盈退出。

实现代码：`backend/app/strategies/volume_ma_pulse.py`  
回测示例：`backend/examples/backtest_volume_ma_pulse.json`  
策略总览：[strategy-guide.md](./strategy-guide.md)

## 1. 核心思想

策略假设：当股票在价格走强的同时出现明显放量，说明资金参与度提升，后续可能延续上涨；如果后续缩量走弱，或价格触及止损/止盈阈值，则退出。

当前单票回测示例（`backend/examples/backtest_volume_ma_pulse.json`）使用：

```json
{
  "volume_ma_period": 20,
  "volume_metric": "amount",
  "threshold_mode": "fixed",
  "high_ratio": 1.6,
  "low_ratio": 0.8,
  "require_bull_bar": true,
  "require_bear_bar": true,
  "price_ma_period": 20,
  "trend_ma_period": 60,
  "breakout_period": 20,
  "require_price_above_ma": true,
  "require_trend_up": false,
    "require_breakout": true,
    "entry_confirm_bars": 1,
    "stop_loss_pct": 0.08,
    "take_profit_pct": 0.25
  }
```

沪深300发掘示例（`discover_hs300_volume_ma_pulse.json`）在同样口径上改用分位阈值：`threshold_mode=percentile`，`high_percentile=0.8`，`low_percentile=0.2`，`percentile_lookback=120`。

相对旧版的三项改进：

1. **均量不含当日**：放量日不会抬高分母、压低量比。
2. **默认用成交额**：跨股价水平更可比；无 `amount` 时回退到成交量。
3. **可选分位阈值**：按个股自身量比分布自适应放量/缩量线，减少固定倍数过拟合。

`entry_confirm_bars` 代码默认 `0`（放量日当天买）。当前单票示例设为 `1`：放量日只记候选，次日若收盘站稳放量日收盘价则买入，若跌破放量日最低价则候选作废。止损/止盈仍当日生效，不延迟。

## 2. 变量定义

设第 `t` 根日 K 线的开盘价、收盘价为 `O_t`、`C_t`。活动量 `A_t` 由 `volume_metric` 决定：

- `amount`：优先取成交额；缺失时回退成交量。
- `volume`：取成交量。

成交活动量均线周期为 `N_v`，价格均线周期为 `N_p`，趋势均线周期为 `N_m`，突破观察窗口为 `N_b`。

### 活动量均线（不含当日）

$$
AM_t = \frac{1}{N_v}\sum_{i=1}^{N_v} A_{t-i}
$$

当前示例中 `N_v = 20`。

### 量比

当 `AM_t > 0` 时：

$$
VR_t = \frac{A_t}{AM_t}
$$

### 阈值

#### 固定倍数模式（`threshold_mode = fixed`）

$$
H_t = \texttt{high\_ratio},\quad L_t = \texttt{low\_ratio}
$$

#### 分位模式（`threshold_mode = percentile`）

对过去 `N_q` 日量比（不含当日）取分位：

$$
H_t = Q_{\texttt{high\_percentile}}(VR_{t-N_q},\ldots,VR_{t-1})
$$

$$
L_t = Q_{\texttt{low\_percentile}}(VR_{t-N_q},\ldots,VR_{t-1})
$$

沪深300发掘示例中 `N_q = 120`，`high_percentile = 0.8`，`low_percentile = 0.2`。单票回测示例使用固定倍数：`H=1.6`，`L=0.8`。

### 价格均线

$$
PM_t = \frac{1}{N_p}\sum_{i=0}^{N_p-1} C_{t-i}
$$

当前示例中 `N_p = 20`。

### 趋势均线

$$
TM_t = \frac{1}{N_m}\sum_{i=0}^{N_m-1} C_{t-i}
$$

当前示例中 `N_m = 60`。沪深300示例里 `require_trend_up = false`，趋势均线只作为输出指标。

### 前高突破线

策略使用前 `N_b` 日收盘价高点作为突破线，且不包含当天：

$$
BH_t = \max(C_{t-1}, C_{t-2}, ..., C_{t-N_b})
$$

当前示例中 `N_b = 20`。

## 3. 买入条件

策略只在空仓时考虑买入。买入信号记为：

$$
S_t = 1
$$

### 3.1 量比上穿放量阈值

$$
VR_{t-1} \le H_t \quad \text{且} \quad VR_t > H_t
$$

### 3.2 阳线过滤

当前示例中 `require_bull_bar = true`：

$$
C_t > O_t
$$

### 3.3 站上价格均线

当前示例中 `require_price_above_ma = true`：

$$
C_t > PM_t
$$

### 3.4 突破前高

当前示例中 `require_breakout = true`：

$$
C_t > BH_t
$$

### 3.5 放量后确认延迟（可选）

当 `entry_confirm_bars = D > 0` 时，放量日 `t0` 不买入，只记录候选：

- 参考收盘：`C_{t0}`
- 失效低点：`L_{t0}`（当日最低价）
- 确认窗口：`t0+1 .. t0+D`

在窗口内任一交易日 `t`：

- 若 `C_t < L_{t0}`：候选作废
- 若 `C_t \ge C_{t0}` 且当日仍满足阳线/均线/突破等过滤：买入
- 若窗口结束仍未确认：候选作废

止损、止盈不走确认延迟，持仓后仍按收盘价当日触发。

### 3.6 当前完整买入公式

当 `entry_confirm_bars = 0`（示例默认即时模式）时：

$$
S_t = 1
\iff
\neg Position_{t-1}
\land (VR_{t-1} \le H_t)
\land (VR_t > H_t)
\land (C_t > O_t)
\land (C_t > PM_t)
\land (C_t > BH_t)
$$

当 `entry_confirm_bars = D > 0` 时，放量日只产生候选；真正买入发生在确认日（见 3.5）。

## 4. 卖出条件

策略只在持仓时考虑卖出。卖出信号记为：

$$
S_t = -1
$$

卖出由三类条件触发：缩量退出、止损退出、止盈退出。任意一个满足即可卖出。

### 4.1 缩量退出

$$
VR_{t-1} \ge L_t \quad \text{且} \quad VR_t < L_t
$$

当前示例中 `require_bear_bar = true`，因此还要求：

$$
C_t < O_t
$$

### 4.2 止损退出

设入场价格为 `P_entry`，止损比例为 `SL`。当前示例中 `SL = 0.08`：

$$
C_t \le P_{entry}(1 - SL) = 0.92P_{entry}
$$

### 4.3 止盈退出

设止盈比例为 `TP`。当前示例中 `TP = 0.25`：

$$
C_t \ge P_{entry}(1 + TP) = 1.25P_{entry}
$$

### 4.4 当前完整卖出公式

$$
S_t = -1
\iff
Position_{t-1}
\land
\left[
\left((VR_{t-1} \ge L_t) \land (VR_t < L_t) \land (C_t < O_t)\right)
\lor
\left(C_t \le 0.92P_{entry}\right)
\lor
\left(C_t \ge 1.25P_{entry}\right)
\right]
$$

## 5. 可选趋势过滤

策略代码支持趋势过滤，但当前沪深300示例未启用。

如果 `require_trend_up = true`，买入时还会要求：

$$
(C_t > TM_t) \land (TM_t > TM_{t-1})
$$

## 6. 回测执行假设

策略生成 `S_t` 后，项目统一回测引擎按以下方式执行：

- `S_t = 1` 且当前空仓：按当天收盘价全仓买入。
- `S_t = -1` 且当前持仓：按当天收盘价全仓卖出。
- 买卖均按 `commission` 扣除手续费，当前示例为 `0.0003`。
- 不考虑滑点、冲击成本、涨跌停无法成交、停牌、印花税等现实约束。

买入时，设当前现金为 `Cash_t`，手续费率为 `f`，买入股数为：

$$
Shares_t = \frac{Cash_t(1-f)}{C_t}
$$

卖出时，卖出后现金为：

$$
Cash_t = Shares_{t-1} \cdot C_t \cdot (1-f)
$$

持仓期间的权益为：

$$
Equity_t = Shares_t \cdot C_t
$$

空仓期间的权益为：

$$
Equity_t = Cash_t
$$

## 7. 策略解释

- 用相对**过去** `N_v` 日均活动量的量比捕捉真正放量，而不是被当日成交稀释的假象。
- 用成交额作活动量，降低股价高低对股数成交量的扭曲。
- 用个股量比分位代替全局固定 `1.6`，让不同流动性股票使用各自的放量标准。
- 用 `C_t > PM_t` 与前高突破过滤弱势放量。
- 可选 `entry_confirm_bars`：把放量视为分歧点燃，延后 1–2 日看多方是否胜出再进场。
- 用止损/止盈控制失败突破与锁定脉冲利润。

## 8. 风险与局限

- 分位阈值对 `percentile_lookback` 敏感，窗口过短会抖动，过长会滞后。
- 对单只股票回测调出的参数可能过拟合，应该用更多股票、更多时间窗口验证。
- 策略使用当天收盘价和当天成交量/成交额生成信号，并假设当天收盘价成交，真实交易中存在不可提前知道完整日成交的问题。
- 全仓买卖会放大单次错误信号的影响。
- 在震荡市中，放量突破可能是假突破，容易触发止损。

## 9. 运行示例

沪深300股票池批量发掘：

```powershell
cd d:\book\quantdance\backend
python -m app.script discover --request "examples/discover_hs300_volume_ma_pulse.json"
```

单只股票回测（分位阈值 + 成交额）：

```powershell
cd d:\book\quantdance\backend
python -m app.script backtest --strategy volume_ma_pulse --symbol 600368 --start-date 2024-07-11 --end-date 2026-07-11 --volume-metric amount --threshold-mode percentile --require-price-above-ma --require-breakout --stop-loss-pct 0.08 --take-profit-pct 0.25
```

固定倍数模式示例：

```powershell
python -m app.script backtest --strategy volume_ma_pulse --symbol 600368 --start-date 2024-07-11 --end-date 2026-07-11 --volume-metric amount --threshold-mode fixed --high-ratio 1.6 --low-ratio 0.8 --require-price-above-ma --require-breakout
```
