# 布林带均值回归策略说明

本文档描述项目中的 `bollinger_reversion` 策略。该策略属于均值回归类：价格跌破下轨后重新站回时买入，涨破上轨后回落到带内时卖出。

实现代码：`backend/app/strategies/bollinger_reversion.py`

## 1. 核心思想

布林带用中轨（SMA）与标准差刻画价格波动区间。策略假设极端偏离后更可能向均值回归，因此在“穿出后重新回到带内”时交易，而不是在带内持续持有。

默认参数示例：

```json
{
  "period": 20,
  "num_std": 2.0
}
```

## 2. 变量定义

设周期为 `N`，标准差倍数为 `k`。

### 中轨与波动率

$$
MID(t) = \frac{1}{N}\sum_{j=0}^{N-1} C_{t-j}
$$

与实现一致，使用总体标准差（`ddof=0`）：

$$
STD(t) = \sqrt{\frac{1}{N}\sum_{j=0}^{N-1} (C_{t-j} - MID(t))^2}
$$

### 上下轨

$$
UPPER(t) = MID(t) + k \cdot STD(t)
$$

$$
LOWER(t) = MID(t) - k \cdot STD(t)
$$

## 3. 信号规则

- **买入**：`C_{t-1} < LOWER(t-1)` 且 `C_t >= LOWER(t)`
- **卖出**：`C_{t-1} > UPPER(t-1)` 且 `C_t <= UPPER(t)`

即：前一根收盘在轨外，当前收盘回到轨内（或刚好触及轨线）。

## 4. 参数说明

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `period` | 20 | 2~400 | 中轨与标准差窗口 |
| `num_std` | 2.0 | 0.1~10.0 | 上下轨标准差倍数 |

公共回测参数：`initial_cash`、`commission`（见 [策略总览](./strategy-guide.md)）。

最少 K 线数：`period`。

## 5. 适用场景与局限

- **适用**：区间震荡、价格围绕均值波动的市场环境。
- **局限**：单边趋势中容易“逆势抄底/摸顶”造成持续亏损。

## 6. 调参建议

- 增大 `num_std` 可减少交易次数，提高极端偏离后的入场质量。
- 可叠加趋势过滤（例如仅在大级别横盘阶段启用）。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --strategy bollinger_reversion --symbol 600000 \
  --start-date 2023-01-01 --end-date 2024-12-31
```
