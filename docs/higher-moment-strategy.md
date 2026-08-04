# 高阶矩自适应 EMA 策略

本文档描述单票 `higher_moment` 策略。它用收益率分布的不对称尾部特征产生信号，
并按历史样本内夏普率定期重选 EMA 平滑系数。

实现代码：`backend/app/strategies/higher_moment.py`  
回测示例：`backend/examples/backtest_higher_moment.json`

## 1. 指标与信号

先计算对数收益率：

$$
r_t = \ln(P_t / P_{t-1})
$$

在最近 `window` 个收益率上计算 `order` 阶标准化中心矩：

$$
M_t = \frac{1}{N}\sum_{i=t-N+1}^{t}
\left(\frac{r_i-\bar r_t}{\sigma_t}\right)^{order}
$$

再使用系数 `alpha` 进行 EMA 平滑：

$$
E_t = \alpha M_t + (1-\alpha)E_{t-1}
$$

- `E_t` 上穿 0：全仓买入。
- `E_t` 下穿 0：全仓卖出。
- 指标首次出现时以 0 为基线，因此首个正值会触发买入。

## 2. alpha 的 walk-forward 优化

默认从第 252 个交易日开始，每隔 90 个交易日重新选择一次 `alpha`：

1. 只使用优化日之前的 252 根 K 线，不使用优化日及之后的数据。
2. 遍历 0.05、0.10、……、0.50。
3. 对每个候选值执行样本内回测，选择夏普率最高者。
4. 夏普率相同时，选择最接近当前值的候选项。
5. 新值从优化日当日开始用于 EMA 递推，EMA 状态不中断。

不足 252 根 K 线时使用初始 `alpha=0.20`，不会执行优化。

## 3. 策略参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `order` | 5 | 标准化中心矩阶数，范围 3~10 |
| `window` | 20 | 收益率滚动窗口，范围 5~252 |
| `alpha` | 0.20 | 首次优化前的 EMA 系数 |
| `optimize_interval` | 90 | alpha 重选间隔（交易日） |
| `optimize_lookback` | 252 | 每次优化使用的历史交易日数 |
| `alpha_min` | 0.05 | 候选 alpha 下限 |
| `alpha_max` | 0.50 | 候选 alpha 上限 |
| `alpha_step` | 0.05 | 候选 alpha 步长 |

策略至少需要 `window + 1` 根 K 线。奇数阶矩适合零轴方向信号；改为偶数阶时，
标准化中心矩通常不会穿越零轴，可能没有交易。

## 4. 示例请求参数

对照 `backend/examples/backtest_higher_moment.json`。公共字段见
[策略总览](./strategy-guide.md)。

- `strategy_id`：`higher_moment`
- `symbol`：`600000`
- `start_date` / `end_date`：回测起止日期；建议覆盖多个优化周期
- `initial_cash`：初始资金
- `commission`：买卖手续费率
- `strategy_params`：上表中的指标及优化参数
- `output_options.plot`：图表输出路径

## 5. 运行示例

```bash
cd backend
python -m app.script backtest --request examples/backtest_higher_moment.json
```

该优化结果是样本内选择，不能保证样本外收益；候选值越多，过拟合风险越高。
