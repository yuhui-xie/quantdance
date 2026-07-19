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

### 单票回测 example 公共字段（`backtest --request`）

以 `backend/examples/backtest_*.json` 为例，除 `strategy_params` 外各字段含义：

| 字段 | 含义 |
| --- | --- |
| `data_source` | 行情源，目前仅支持 `a_stock_data` |
| `strategy_id` | 策略 id（见 `--list-strategies`） |
| `symbol` | 标的代码（6 位或带交易所后缀） |
| `start_date` / `end_date` | 回测区间，`YYYY-MM-DD` |
| `initial_cash` | 初始资金（元） |
| `commission` | 买卖手续费率（如 `0.0003` = 万三） |
| `strategy_params` | 该策略专属参数，见对应策略文档 |
| `output_options.output` | 可选，结果 JSON 落盘路径 |
| `output_options.plot` | 可选，权益曲线图路径（`.svg` / `.png`） |
| `output_options.json` | 是否向 stdout 打印完整 JSON |

### 组合回测 example 公共字段（`portfolio --request`）

以 `backend/examples/portfolio_*.json` 为例，除 `strategy_params` 外各字段含义：

| 字段 | 含义 |
| --- | --- |
| `strategy_id` | 组合策略 id（见 `portfolio --list-strategies`） |
| `mode` | `backtest` 周期调仓回测；`screen` 仅做截面选股 |
| `data_source` | 行情源，目前仅支持 `a_stock_data` |
| `universe` | `symbols` 为空时的股票池：`all_a` / `hs300` / `zz399101` |
| `symbols` | 显式股票列表；非空时覆盖 `universe` |
| `max_universe` | 股票池上限；超出时截取或按 `seed` 抽样（演示用） |
| `seed` | 抽样随机种子，便于复现 |
| `start_date` / `end_date` | 回测区间；`screen` 时 `end_date` 为截面日 |
| `rebalance_freq` | 调仓间隔（**交易日**）：`20`≈月频，`5`≈周频，`1`=每日 |
| `initial_cash` | 初始资金（元） |
| `commission` | 佣金费率 |
| `min_commission` | 单笔最低佣金（元） |
| `slippage` | 单边滑点比例（默认 `0.01` = 1%） |
| `lot_size` | 买入整手数（股，A 股通常 100） |
| `use_cache` | 是否使用本地基本面/财报缓存 |
| `force_refresh` | 是否强制重新拉取并覆盖缓存 |
| `max_workers` | 并行拉取估值/财报的线程数 |
| `strategy_params` | 该组合策略专属参数，见对应策略文档 |
| `output_options.*` | `output` / `plot` / `json`；另支持 `report`（交互 HTML，含调仓买卖与区间收益；未写时若有 `plot` 则自动派生同名 `.html`） |

各策略文档的「示例请求参数」一节会对照其 example 文件逐字段说明（含 `strategy_params`）。

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

### 低频组合策略（`portfolio` 子命令）

与单票 `backtest` 平行的一类能力：**截面选股 + 按交易日间隔调仓**（如每 20 日≈月频），适合小市值、基本面过滤等低频交易。

| strategy_id | 名称 | 类型 | 文档 |
| --- | --- | --- | --- |
| `market_auntie` | 菜场大妈（质好价低市值小） | 多因子选股 + 周期等权 | [market-auntie-strategy.md](./market-auntie-strategy.md) |
| `small_cap_zz399101` | 中小综指微盘 | 399101 最小流通市值 TopN | [small-cap-zz399101-strategy.md](./small-cap-zz399101-strategy.md) |
| `limit_up_pullback` | 涨停回落埋伏 | 涨停事件 + 低位整理 | [limit-up-pullback-strategy.md](./limit-up-pullback-strategy.md) |
| `order_inflection` | 订单开工拐点 | 合同负债→毛利→现金流→存货五步法 | [order-inflection-strategy.md](./order-inflection-strategy.md) |

查看已注册单票策略：

```bash
cd backend
python -m app.script backtest --list-strategies
```

查看已注册组合策略：

```bash
cd backend
python -m app.script portfolio --list-strategies
python -m app.script portfolio --request examples/portfolio_small_cap_zz399101.json
```

## 三、如何选择与组合策略

- **先分市场状态**：趋势市优先 `ma/ema/macd/donchian`，震荡市优先 `rsi/stochastic/bollinger`。
- **再看交易频率**：周期越短通常信号越密、手续费侵蚀越明显。
- **统一比较口径**：至少同时看 `total_return`、`max_drawdown`、`sharpe`、`num_trades`。
- **建议流程**：先用默认参数跑基准，再一次只改 1~2 个参数做对照。

## 四、扩展新策略（开发者）

新增**单票**策略时：

1. 在 `backend/app/strategies/` 新建模块并导出 `STRATEGY`（`StrategySpec`），系统会自动扫描注册。
2. 在 `docs/` 下新增对应的 `*-strategy.md` 专文（参数、公式、信号、调参、运行示例）。
3. 专文须含「示例请求参数」：对照 `backend/examples/` 中的请求 JSON，逐字段说明请求级字段与 `strategy_params`。
4. 在本页「策略文档索引」表中增加一行链接。

新增**组合**策略时：

1. 在 `backend/app/portfolio/` 新建模块并导出 `STRATEGY`（`PortfolioStrategySpec`），系统会自动扫描注册。
2. 在 `docs/` 下新增专文（含完整 example 参数释义），并更新本页组合策略索引。
3. 不要硬塞进单票 `StrategySpec`；成交与调仓复用 `portfolio_engine` / `portfolio/runner.py`。

推荐沿用现有参数模型（Pydantic）与 `run_*` 风格，确保 API/CLI 可直接复用。
