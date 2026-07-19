# 随机指标交叉策略说明

本文档描述项目中的 `stochastic_cross` 策略。该策略属于振荡反转类：在超卖区 `%K` 上穿 `%D` 买入，在超买区 `%K` 下穿 `%D` 卖出。

实现代码：`backend/app/strategies/stochastic_cross.py`

## 1. 核心思想

随机指标衡量收盘价在近期高低区间中的相对位置。策略要求交叉发生在极值区附近：上穿前 `%K` 仍在超卖区下方，下穿前 `%K` 仍在超买区上方，以过滤中性区域的噪音交叉。

默认参数示例：

```json
{
  "k_period": 14,
  "d_period": 3,
  "smooth": 3,
  "oversold": 20,
  "overbought": 80
}
```

## 2. 变量定义

设参数为 `N_k`、`N_d`、`N_s`（smooth）。

### 区间与原始 %K

$$
LL_t = \min(L_{t-N_k+1}, \ldots, L_t)
$$

$$
HH_t = \max(H_{t-N_k+1}, \ldots, H_t)
$$

$$
RAWK_t = 100 \cdot \frac{C_t - LL_t}{HH_t - LL_t}
$$

当 `HH_t = LL_t` 时实现中记为无效值（不产生信号）。

### 平滑 %K 与 %D

$$
K_t = SMA(RAWK, N_s)
$$

$$
D_t = SMA(K, N_d)
$$

## 3. 信号规则

与实现一致，阈值用上一根 `%K` 判断：

- **买入**：`K_t > D_t` 且 `K_{t-1} <= D_{t-1}` 且 `K_{t-1} < oversold`
- **卖出**：`K_t < D_t` 且 `K_{t-1} >= D_{t-1}` 且 `K_{t-1} > overbought`

## 4. 参数说明

本策略暂无独立 example 文件；CLI 回测时 `strategy_params` 与下方一致。请求级公共字段见 [策略总览](./strategy-guide.md)。

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `k_period` | 14 | 2~200 | 高低区间回看周期 |
| `d_period` | 3 | 1~100 | `%D` 为 `%K` 的 SMA 周期 |
| `smooth` | 3 | 1~50 | 原始 `%K` 的平滑周期 |
| `oversold` | 20 | 1~50 | 超卖阈值 |
| `overbought` | 80 | 50~99 | 超买阈值，必须大于 `oversold` |

最少 K 线数：`k_period + smooth + d_period + 5`。

## 5. 适用场景与局限

- **适用**：短周期震荡中捕捉拐点，适合较灵敏的振荡策略。
- **局限**：过于灵敏时噪音很大，容易出现高频反复交易。

## 6. 调参建议

- 增大 `smooth` 或 `d_period` 可以降低噪音。
- 可只在波动收敛后的区间环境启用该策略。

## 7. 运行示例

```bash
cd backend
python -m app.script backtest --strategy stochastic_cross --symbol 600000 \
  --start-date 2023-01-01 --end-date 2024-12-31
```
