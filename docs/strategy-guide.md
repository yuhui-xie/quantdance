# 回测策略总览

本文档说明回测引擎的统一约定，并索引各内置策略的独立说明文档。参数、公式与调参细节见对应策略文档。

## 一、BACKTEST 的两类执行语义

统一入口为 `python -m app.script backtest`，注册表中包含两类策略：

- **时序策略 `StrategySpec`**：`mode=single` 时运行单票；`mode=universe` 时把同一策略逐票独立运行并统计汇总。每只股票使用完整且互不共享的 `initial_cash`。
- **横截面策略 `CrossSectionStrategySpec`**：`mode=universe` 时让所有标的共享一个账户，策略在自己生成的决策日做截面选股，执行器据此换仓；`mode=screen` 只返回指定截面的选股结果。

时序回测采用全仓买入/全仓卖出的简化模型，信号触发当根 K 线按 `close`
成交，手续费由 `commission` 控制。横截面共享资金回测还支持滑点、整手、
最低佣金和仓位管理。两类回测都要求可用的 OHLCV 行情；现实中的冲击成本、
停牌与涨跌停成交限制仅按各模块已实现的规则处理。

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

### 股票池独立资金批量回测

`backtest` 也支持把同一个单票策略和参数应用到一批股票。参考
`backend/examples/backtest_universe_llt_trend.json`：

- 设置 `mode: "universe"`，并提供 `universe`，或用 `symbols` 显式指定代码。
- 批量模式必须同时提供 `start_date` 和 `end_date`。
- 每只股票都独立使用完整的 `initial_cash`，交易互不占用资金。
- `aggregate` 将各票净值除以 `initial_cash` 后按日期等权平均；它是结果汇总，
  不模拟共享资金、换仓或持仓上限。
- `runs` 按股票池顺序输出，成功项包含收益 `rank`；单票失败或 K 线不足不会中断整批。
- `include_equity`、`include_trades`、`include_price` 控制逐票明细体积，
  其中 `include_price` 默认关闭。
- `max_workers` 控制并发数；`max_universe` 和 `seed` 控制股票池规模与抽样复现。

命令行示例：

```bash
cd backend
python -m app.script backtest --mode universe --universe hs300 \
  --strategy llt_trend --start-date 2023-01-01 --end-date 2024-12-31 \
  --max-universe 30 --plot out/backtest_universe_llt_trend.svg
```

两类 `mode=universe` 能力的资金语义不同：

- 时序 `StrategySpec`：逐票独立资金运行相同策略，再统计性等权汇总。
- 横截面 `CrossSectionStrategySpec`：所有股票共享一个账户，在策略生成的决策日进行截面选股与实际换仓。

### 横截面共享资金 example 公共字段（`backtest --request`）

以 `backend/examples/backtest_shared_*.json` 为例，除 `strategy_params` 外各字段含义：

| 字段 | 含义 |
| --- | --- |
| `strategy_id` | 横截面策略 id（见 `backtest --list-strategies`） |
| `mode` | `universe` 共享资金回测；`screen` 仅做截面选股 |
| `data_source` | 行情源，目前仅支持 `a_stock_data` |
| `universe` | `symbols` 为空时的股票池：`all_a`（全A）/ `hs300` / `zz500` / `zz399101`（中小综指）/ `zz1000`（中证1000）/ `gz2000`（国证2000）/ `star50`（科创50）/ `star_board`（科创板全板块） |
| `symbols` | 显式股票列表；非空时覆盖 `universe` |
| `max_universe` | 股票池上限（组合请求 1~10000）；超出时截取或按 `seed` 抽样；全 A 约设 `6000` |
| `seed` | 抽样随机种子，便于复现 |
| `start_date` / `end_date` | 回测区间；`screen` 时 `end_date` 为截面日 |
| `initial_cash` | 初始资金（元） |
| `commission` | 佣金费率 |
| `min_commission` | 单笔最低佣金（元） |
| `slippage` | 单边滑点比例（默认 `0.01` = 1%） |
| `lot_size` | 买入整手数（股，A 股通常 100） |
| `take_profit_arm_pct` | 通用止盈启动阈值 x：相对成本浮盈达到后继续持有（如 `0.2`）；与 exit 成对出现 |
| `take_profit_exit_pct` | 通用止盈回落阈值 y：启动后浮盈回落到该比例则卖出（须 `< arm`，如 `0.1`）；仅非调仓日生效 |
| `stop_loss_pct` | 通用止损：相对成本浮亏达到该比例则卖出（如 `0.1`=跌 10%）；仅非调仓日生效 |
| `rebalance_mode` | 换仓模式：`full`（默认，目标变动时全仓清空重建）/ `incremental`（只交易差异，保留共同持仓，权重自然漂移） |
| `use_cache` | 是否使用本地基本面/财报缓存 |
| `force_refresh` | 是否强制重新拉取并覆盖缓存 |
| `max_workers` | 并行拉取估值/财报的线程数 |
| `strategy_params` | 该横截面策略专属参数，见对应策略文档；决策频率由 `decision_frequency`（`daily`/`weekly`/`monthly`）+ `decision_every_n`（步长）统一控制，`daily` 下 `decision_warmup` 控制冷启动，`prosperity_resonance` 另用 `hysteresis_rank_threshold` 防抖 |
| `output_options.*` | `output` / `plot` / `json`；另支持 `report`（交互 HTML，含调仓买卖与区间收益；未写时若有 `plot` 则自动派生同名 `.html`） |

各策略文档的「示例请求参数」一节会对照其 example 文件逐字段说明（含
`strategy_params`）。决策频率由 `decision_frequency` 统一控制，取 `daily` /
`weekly` / `monthly` 之一，默认见各策略（周期策略默认 `monthly`，
`prosperity_resonance` 默认 `daily`）：

- `monthly` 在**自然月月末**锚定决策日（`decision_every_n=3` 即季末）；
- `weekly` 在 **ISO 周周末**锚定决策日（`decision_every_n=2` 即双周）；
- `daily` 每个交易日都是决策日，`decision_warmup`（默认 20）跳过前 N 根
  冷启动期，保证 MA/LLT 等指标有足够历史。

weekly/monthly 均锚定日历周期而非回测起始日，回测结果不随起始日相位漂移。
共享资金引擎只执行策略返回的决策日，并不强制定时调仓。换仓执行方式由
`rebalance_mode` 决定（默认 `full` 全清重建，可设 `incremental` 只交易差异）。

#### 系统化仓位管理（可选）

共享资金请求可设置 `position_management`。启用后不再在每个决策日全卖全买：
仍在目标池中的仓位会保留，移出目标池时卖出，新标的先建立初始仓位。
该字段不能与旧版顶层 `stop_loss_pct`、`take_profit_arm_pct`、
`take_profit_exit_pct` 同时使用。

- `max_positions`：最大持仓数 `x`；每只股票的固定资金上限为初始资金 `N / x`。
- `initial_allocation_pct`：首次买入占单票资金上限的比例。
- `add_allocation_pct`：每次加仓占单票资金上限的比例。
- `add_trigger_pct`：相对首次成交价每上涨一个档位加仓一次；每天最多加一份，
  且加仓后的市值不会主动超过单票资金上限。
- `stop_loss_pct`：相对加权平均成本的止损比例；设为 `null` 可关闭。
- `take_profit_mode`：`none` 关闭；`fixed` 达到 `take_profit_pct` 直接卖出；
  `trailing` 达到该阈值后，按最高价回撤 `trailing_drawdown_pct` 卖出。

每日处理时止损/止盈优先于加仓。止损或止盈当天即使仍被策略选中，也不会重新买入。
价格上涨本身可能使持仓市值被动超过 `N / x`，模块不会为此强制减仓。

## 二、策略文档索引

| strategy_id | 名称 | 类型 | 文档 |
| --- | --- | --- | --- |
| `ma_crossover` | 双均线交叉 | 趋势跟随 | [ma-crossover-strategy.md](./ma-crossover-strategy.md) |
| `ema_crossover` | 双 EMA 交叉 | 趋势跟随 | [ema-crossover-strategy.md](./ema-crossover-strategy.md) |
| `llt_trend` | LLT 趋势拐点 | 趋势跟随 | [llt-trend-strategy.md](./llt-trend-strategy.md) |
| `higher_moment` | 高阶矩自适应 EMA | 分布形态 | [higher-moment-strategy.md](./higher-moment-strategy.md) |
| `macd` | MACD 交叉 | 趋势跟随 | [macd-strategy.md](./macd-strategy.md) |
| `bollinger_reversion` | 布林带均值回归 | 均值回归 | [bollinger-reversion-strategy.md](./bollinger-reversion-strategy.md) |
| `donchian_breakout` | 唐奇安突破 | 趋势突破 | [donchian-breakout-strategy.md](./donchian-breakout-strategy.md) |
| `rsi_reversal` | RSI 反转 | 振荡反转 | [rsi-reversal-strategy.md](./rsi-reversal-strategy.md) |
| `stochastic_cross` | 随机指标交叉 | 振荡反转 | [stochastic-cross-strategy.md](./stochastic-cross-strategy.md) |
| `volume_ma_pulse` | 量比放量/缩量脉冲 | 量价触发 | [volume-ma-pulse-strategy.md](./volume-ma-pulse-strategy.md) |

### 横截面共享资金策略（`backtest` 子命令）

这类策略负责生成决策日并执行截面选股，共享资金引擎负责成交、持仓和权益核算。
决策频率由 `strategy_params.decision_frequency` 统一控制（`daily`/`weekly`/
`monthly`，配合 `decision_every_n` 步长），均锚定自然周期（日/ISO 周/自然月末）
使回测结果不随起始日相位漂移；周期策略默认 `monthly`，`prosperity_resonance`
默认 `daily` + 防抖滞后带。未来策略也可实现事件驱动或其他决策日规则。

| strategy_id | 名称 | 类型 | 文档 |
| --- | --- | --- | --- |
| `market_auntie` | 菜场大妈（质好价低市值小） | 多因子选股 + 周期等权 | [market-auntie-strategy.md](./market-auntie-strategy.md) |
| `small_cap_zz399101` | 中小综指微盘 | 399101 最小流通市值 TopN | [small-cap-zz399101-strategy.md](./small-cap-zz399101-strategy.md) |
| `limit_up_pullback` | 涨停回落埋伏 | 涨停事件 + 低位整理 | [limit-up-pullback-strategy.md](./limit-up-pullback-strategy.md) |
| `order_inflection` | 订单开工拐点 | 合同负债→毛利→现金流→存货五步法 | [order-inflection-strategy.md](./order-inflection-strategy.md) |
| `etf_rotation` | ETF 动量轮动 | 趋势向上且中期动量最强的 ETF | — |
| `etf_rotation_3factor` | 三因子ETF轮动 | 乖离+斜率+效率三因子加权评分，1.5×阈值防抖 | [etf-rotation-3factor-strategy.md](./etf-rotation-3factor-strategy.md) |
| `cyclical_rotation` | 顺周期行业轮动 | 商品信号驱动的有色/能源/农业轮动 | [cyclical-rotation-strategy.md](./cyclical-rotation-strategy.md) |
| `prosperity_resonance` | 景气共振 | PEG + 趋势确认 + 回调入场三重共振 | [prosperity-resonance-strategy.md](./prosperity-resonance-strategy.md) |
| `new_stock_ice_reversal` | 次新情绪冰点反转 | 次新跌停潮冰点 → 次日反转次新篮 | [new-stock-ice-reversal-strategy.md](./new-stock-ice-reversal-strategy.md) |

查看全部已注册策略：

```bash
cd backend
python -m app.script backtest --list-strategies
```

运行横截面共享资金策略：

```bash
cd backend
python -m app.script backtest --strategy small_cap_zz399101 --mode screen --json
python -m app.script backtest --request examples/backtest_shared_small_cap_zz399101.json
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

新增**横截面共享资金**策略时：

1. 在 `backend/app/strategies/cross_section/` 新建模块并导出 `STRATEGY`（`CrossSectionStrategySpec`），系统会自动扫描注册。
2. 策略实现自己的截面选择与 `decision_dates`；决策频率参数 `decision_frequency`（`daily`/`weekly`/`monthly`）+ `decision_every_n`（步长）+ `decision_warmup`（冷启动，仅 `daily` 生效）放在该策略的 `strategy_params` 中，`decision_dates` 委托 `decision_dates_by_frequency(calendar, frequency=..., every_n=..., warmup=...)` 生成锚定自然周期（日/ISO 周/自然月末）的决策日，避免对回测起始日相位敏感；不要把它当成引擎级调仓参数。
3. 在 `docs/` 下新增专文（含完整 example 参数释义），并更新本页横截面策略索引。
4. 共享资金成交与持仓复用 `backend/app/backtest/shared_engine.py`，横截面准备与执行复用 `backend/app/backtest/cross_section_runner.py`。

推荐沿用现有参数模型（Pydantic）与 `run_*` 风格，确保 API/CLI 可直接复用。
