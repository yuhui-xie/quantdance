# 回测策略总览

本文档说明回测引擎的统一约定，并索引各内置策略的独立说明文档。参数、公式与调参细节见对应策略文档。

## 一、回测引擎的统一约定

所有策略都复用同一套回测执行框架：

- **交易模型**：全仓买入 / 全仓卖出（不做分批建仓与分批止盈）。
- **执行价格**：信号触发当根 K 线按 `close` 成交（简化处理）。
- **费用模型**：买卖都按比率收取手续费 `commission`（默认值通常为 `0.0003`）。
- **风控与滑点**：当前不含滑点、冲击成本、停牌限制、涨跌停不可成交等现实约束。
- **输入数据要求**：必须包含 `open` / `high` / `low` / `close` / `volume` 列。

### 公共参数（所有策略共用）

- `initial_cash`：初始资金。
- `commission`：手续费率。

## 二、策略文档索引

| strategy_id | 名称 | 类型 | 文档 |
| --- | --- | --- | --- |
| `ma_crossover` | 双均线交叉 | 趋势跟随 | [ma-crossover-strategy.md](./ma-crossover-strategy.md) |
| `ema_crossover` | 双 EMA 交叉 | 趋势跟随 | [ema-crossover-strategy.md](./ema-crossover-strategy.md) |
| `macd` | MACD 交叉 | 趋势跟随 | [macd-strategy.md](./macd-strategy.md) |
| `bollinger_reversion` | 布林带均值回归 | 均值回归 | [bollinger-reversion-strategy.md](./bollinger-reversion-strategy.md) |
| `donchian_breakout` | 唐奇安突破 | 趋势突破 | [donchian-breakout-strategy.md](./donchian-breakout-strategy.md) |
| `rsi_reversal` | RSI 反转 | 振荡反转 | [rsi-reversal-strategy.md](./rsi-reversal-strategy.md) |
| `stochastic_cross` | 随机指标交叉 | 振荡反转 | [stochastic-cross-strategy.md](./stochastic-cross-strategy.md) |
| `volume_ma_pulse` | 量比放量/缩量脉冲 | 量价触发 | [volume-ma-pulse-strategy.md](./volume-ma-pulse-strategy.md) |

查看已注册策略：

```bash
cd backend
python -m app.script backtest --list-strategies
```

## 三、如何选择与组合策略

- **先分市场状态**：趋势市优先 `ma/ema/macd/donchian`，震荡市优先 `rsi/stochastic/bollinger`。
- **再看交易频率**：周期越短通常信号越密、手续费侵蚀越明显。
- **统一比较口径**：至少同时看 `total_return`、`max_drawdown`、`sharpe`、`num_trades`。
- **建议流程**：先用默认参数跑基准，再一次只改 1~2 个参数做对照。

## 四、扩展新策略（开发者）

新增策略时：

1. 在 `backend/app/strategies/` 新建模块并导出 `STRATEGY`（`StrategySpec`），系统会自动扫描注册。
2. 在 `docs/` 下新增对应的 `*-strategy.md` 专文（参数、公式、信号、调参、运行示例）。
3. 在本页「策略文档索引」表中增加一行链接。

推荐沿用现有参数模型（Pydantic）与 `run_*` 风格，确保 API/CLI 可直接复用。
