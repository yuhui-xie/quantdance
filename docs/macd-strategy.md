# MACD 交叉策略说明

本文档描述项目中的 `macd` 策略。该策略属于趋势/动量确认类：用 MACD 线与信号线的金叉、死叉产生全仓买卖信号。

实现代码：`backend/app/strategies/macd.py`  
回测示例：`backend/examples/backtest_macd.json`

## 1. 核心思想

先用快慢 EMA 差得到 MACD 线（DIF），再对 MACD 做 EMA 得到信号线（DEA）。金叉表示动量转强，死叉表示动量转弱。

默认参数示例：

```json
{
  "fast_period": 12,
  "slow_period": 26,
  "signal_period": 9
}
```

## 2. 变量定义

设参数为 `N_f`（fast）、`N_s`（slow）、`N_sig`（signal）。

- `EMA_f(t)`、`EMA_s(t)` 定义与 [双 EMA 交叉](./ema-crossover-strategy.md) 相同。
- `MACD(t) = EMA_f(t) - EMA_s(t)`
- 信号线：

$$
\alpha_{sig} = \frac{2}{N_{sig} + 1}
$$

$$
SIG(0) = MACD(0)
$$

$$
SIG(t) = \alpha_{sig} \cdot MACD(t) + (1 - \alpha_{sig}) \cdot SIG(t-1)
$$

- 柱状图：`HIST(t) = MACD(t) - SIG(t)`

## 3. 信号规则

- **买入**：`MACD(t) > SIG(t)` 且 `MACD(t-1) <= SIG(t-1)`
- **卖出**：`MACD(t) < SIG(t)` 且 `MACD(t-1) >= SIG(t-1)`

## 4. 示例请求参数

对照 `backend/examples/backtest_macd.json`。公共字段总表见 [策略总览](./strategy-guide.md)。

### 4.1 请求级字段

| 字段 | 示例值 | 含义 |
| --- | --- | --- |
| `data_source` | `a_stock_data` | 行情数据源 |
| `strategy_id` | `macd` | 本策略 id |
| `symbol` | `600519` | 回测标的（贵州茅台） |
| `start_date` / `end_date` | `2022-01-01` / `2024-12-31` | 回测区间 |
| `initial_cash` | `100000` | 初始资金（元） |
| `commission` | `0.0003` | 手续费率（万三） |
| `output_options.json` | `false` | 是否向 stdout 打印完整 JSON |
| `output_options.plot` | `out/backtest_macd.svg` | 权益曲线图路径 |

### 4.2 `strategy_params`

| 参数 | 示例值 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `fast_period` | `12` | 12 | 2~200 | 快线 EMA 周期 |
| `slow_period` | `26` | 26 | 3~400 | 慢线 EMA 周期，须大于 `fast_period` |
| `signal_period` | `9` | 9 | 2~200 | 信号线 EMA 周期 |

最少 K 线数：`slow_period + signal_period + 5`。

## 5. 适用场景与局限

- **适用**：既希望抓趋势，又希望用动量变化做确认。
- **局限**：快速反转行情里，仍可能在“金叉后不久又死叉”。

## 6. 调参建议

- 更短参数组合可提高灵敏度但增加换手。
- 可观察 `macd_hist` 的强弱变化辅助判读信号质量。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --request examples/backtest_macd.json
```
