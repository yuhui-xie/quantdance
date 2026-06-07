# 唐奇安突破策略说明

本文档描述项目中的 `donchian_breakout` 策略。该策略属于趋势突破类：收盘价突破过去 N 日（不含当日）最高价时买入，跌破过去 N 日最低价时卖出。

实现代码：`backend/app/strategies/donchian_breakout.py`

## 1. 核心思想

唐奇安通道用历史高低刻画价格区间。向上突破上轨视为趋势启动或延续，向下跌破下轨视为趋势转弱。实现会比较前一根状态，避免连续重复触发同一方向信号。

默认参数示例：

```json
{
  "period": 20
}
```

## 2. 变量定义

设通道周期为 `N`，高低价分别为 `H_t`、`L_t`。

### 通道（不含当日）

$$
DC_H(t) = \max(H_{t-N}, \ldots, H_{t-1})
$$

$$
DC_L(t) = \min(L_{t-N}, \ldots, L_{t-1})
$$

有效计算区间：`t >= N`。

## 3. 信号规则

- **买入**：`C_t > DC_H(t)` 且 `C_{t-1} <= DC_H(t-1)`
- **卖出**：`C_t < DC_L(t)` 且 `C_{t-1} >= DC_L(t-1)`

## 4. 参数说明

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `period` | 20 | 1~400 | 通道回看天数（不含当日） |

公共回测参数：`initial_cash`、`commission`（见 [策略总览](./strategy-guide.md)）。

最少 K 线数：`period + 5`。

## 5. 适用场景与局限

- **适用**：趋势启动或趋势延续的突破型行情。
- **局限**：假突破在震荡市中常见，容易连续小亏。

## 6. 调参建议

- 缩短 `period` 更敏感，拉长 `period` 更稳健但更滞后。
- 可与波动率或成交量确认结合降低假突破。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --strategy donchian_breakout --symbol 600000 \
  --start-date 2023-01-01 --end-date 2024-12-31
```
