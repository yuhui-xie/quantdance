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
| `output_options.report` | 可选，交互报告路径（`.html`） |
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
  --max-universe 30 --report out/backtest_universe_llt_trend.html
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
| `universe` | `symbols` 为空时的股票池：`all_a`（全A）/ `hs300` / `zz500` / `zz399101`（中小综指）/ `zz1000`（中证1000）/ `gz2000`（国证2000）/ `star50`（科创50）/ `star_board`（科创板全板块）/ `etf`（场内 ETF）。`etf_dynamic` / `etf_asof` 为 `etf` 的别名：带 `start_date` 时按 K 线覆盖重构「当时已存在」的动态池（避开幸存者偏差，见 `etf-filter-strategy.md`）。也支持 `backend/config/*_pool.json` 里的预设池，用文件名前缀即可，如 `etf_core`（读 `config/etf_core_pool.json`），或显式 `config:etf_core`；config 池同样在带 asof 时按存在性过滤 |
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
| `strategy_params` | 该横截面策略专属参数，见对应策略文档；决策频率由 `decision_frequency`（`daily`/`weekly`/`biweekly`/`monthly`）+ `decision_every_n`（步长）统一控制，`decision_anchor`（`start`/`end`）控制 monthly 锚在月初/月末，`daily` 下 `decision_warmup` 控制冷启动，`prosperity_resonance` 另用 `hysteresis_rank_threshold` 防抖 |
| `output_options.*` | `output` / `plot` / `json`；另支持 `report`（交互 HTML，含调仓买卖与区间收益；未写时若有 `plot` 则自动派生同名 `.html`）。`report_top_k`（正整数，universe 模式）限制 HTML 内嵌的逐票明细图数量：仅前 N 名保留可点击的净值/K 线/指标图，排行榜仍保留全部标的指标；省略或 0 表示全部。`report_top_k` 越小，报告生成越快、HTML 越小（500 只全量内嵌需序列化数百万个点，是大池子报告慢的主因）。`report_price_top_k`（正整数）限制从行情缓存补拉 K 线的股票数：省略或 0=全部（默认），正数=仅前 N 名；补拉越多需访问数据源越久，建议大池子按需调小。`pool_dump`（路径）指定决策日池成员 JSON 的落盘路径，见下 |
| `output_options.pool_dump` | 决策日池成员 JSON 的落盘路径。**默认开启**：横截面策略每次回测都会写一份到 `out/pool_<strategy>_<mode>_<start>_<end>.json`（可被 CLI `--pool-dump FILE.json` 覆盖、`--no-pool-dump` 关闭）。内容为 `{asof: {source, count, members:[{symbol,name}]}}`——`source=pool_discovery` 表示该日由 `discover_industry_pool` 从全市场发现（见 `etf-rotation-strategy.md` §2.1 的动态行业池），`source=eligibility` 表示该日通过全部闸门、进入打分的候选集合。时序策略无此产物 |

**终端进度**：回测过程写 stderr——加载行情与逐票回测为单行原地刷新（`加载行情: 823/1681 (48.9%) 512480`，海量标的不刷屏），**每个决策日打印一行永久输出**（`[  3/36] 2024-06-03 选中 512880,512480 | 通过筛选 42 只`），便于滚动回看调仓轨迹。stdout 只留结果本身，`--json` 可安全重定向。

#### 基本面过滤股票池（`fundamental_filter`）

对时序 `mode=universe` 与横截面回测/选股均生效：解析出股票池后、运行前，
用**估值字段**在单一 as-of 时点过滤掉不满足条件的股票。字段与参数：

| 字段 | 含义 |
| --- | --- |
| `fundamental_filter` | 规则列表，**全部规则须同时满足**。每条规则：`field` 取估值字段，`min`/`max` 为含边界的上下限（可只填一个） |
| `fundamental_asof` | 基本面评估时点 `YYYY-MM-DD`；默认取 `start_date`（回测开始时点当时可知的最新估值），`screen` 无起日时取面板最新日期 |

可用的 `field`：`pe_ttm`、`pb`、`ps_ttm`、`peg`、`market_cap`、`float_market_cap`、
`close`、`dividend_yield`（股息率）。缺数据（NaN）的股票按不满足处理，会被剔除，
并计入结果 `warnings`。

示例（只让 pe_ttm 在 0~40 之间的 hs300 参与回测）：

```json
{
  "mode": "universe",
  "strategy_id": "ma_crossover",
  "universe": "hs300",
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "fundamental_filter": [
    { "field": "pe_ttm", "min": 0, "max": 40 }
  ]
}
```

各策略文档的「示例请求参数」一节会对照其 example 文件逐字段说明（含
`strategy_params`）。决策频率由 `decision_frequency` 统一控制，取 `daily` /
`weekly` / `biweekly` / `monthly` 之一，默认见各策略（周期策略默认 `monthly`，
`prosperity_resonance` 默认 `daily`）：

- `monthly` 在**自然月**锚定决策日，锚点由 `decision_anchor` 控制（默认 `end`
  取月末，`start` 取每月首个交易日；`etf_rotation` 默认 `start`，其余周期策略
  默认 `end`）。`decision_every_n=3` 即季末/季初；
- `weekly` 在 **ISO 周周末**锚定决策日（`decision_every_n=2` 即双周）；
- `biweekly` 每 **2 个 ISO 周**（固定双周）的周末锚定决策日，等价于
  `weekly` + `decision_every_n=2`，忽略 `decision_every_n`；
- `daily` 每个交易日都是决策日，`decision_warmup`（默认 20）跳过前 N 根
  冷启动期，保证 MA/LLT 等指标有足够历史。

weekly/biweekly/monthly 均锚定日历周期而非回测起始日，回测结果不随起始日相位漂移。
（`decision_anchor` 仅 `monthly` 生效。）
共享资金引擎只执行策略返回的决策日，并不强制定时调仓。换仓执行方式由
`rebalance_mode` 决定（默认 `full` 全清重建，可设 `incremental` 只交易差异）。

**报告中的指标对比（通用基座）**：共享资金交互报告（`backtest --report`）的选股表会展示
每个调仓日的**全部候选**（不止 `top_n`）及其指标列，并高亮当次入选持仓；另有
"指标对比（调仓日 × 标的）"热力图，可切换指标字段观察排名随时间的演化。
这些指标来自策略 `select` 返回的候选 detail：报告模型 `shared_report_model.py` 会把其中
除元字段（`symbol/name/asof/close/float_market_cap/rank_market_cap/rank/selected`）外的
数值字段**自动透传**并动态渲染，因此**新增指标只需在策略候选 detail 里加一个字段**，
无需改动报告模型或 HTML 模板。注意逐候选 detail 中勿塞入常量参数（属请求配置而非逐标的指标）。

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
| `bullish_alignment` | 多头排列 | 趋势跟随 | [bullish-alignment-strategy.md](./bullish-alignment-strategy.md) |
| `first_limit_up` | 首板超短线（次日开盘进） | 首板事件 + 次日进场 + 5日离场 | [first-limit-up-strategy.md](./first-limit-up-strategy.md) |
| `my_strategy` | 我的策略（LLT 斜率动量） | 趋势跟随 + 震荡过滤 + 可选 VPT 量价过滤 | [my-strategy.md](./my-strategy.md) |
| `limit_up_pullback_lowbuy` | 涨停回调低吸 | 涨停回调 + 缩量十字星 + 放量收阳买点 | [limit-up-pullback-lowbuy-strategy.md](./limit-up-pullback-lowbuy-strategy.md) |

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
| `etf_rotation` | ETF 动量轮动 | LLT 拟合趋势（斜率×R²）最强，可选 VPT 量价确认，惰性调仓降低轮动频率 | [etf-rotation-strategy.md](./etf-rotation-strategy.md) |
| `etf_rotation_3factor` | 三因子ETF轮动 | 乖离+斜率+效率三因子加权评分，1.5×阈值防抖 | [etf-rotation-3factor-strategy.md](./etf-rotation-3factor-strategy.md) |
| `etf_filter` | ETF 动态池筛选 | 从历史时点存在的 ETF 池按流动性筛一批，可选行业均衡 | [etf-filter-strategy.md](./etf-filter-strategy.md) |
| `cyclical_rotation` | 顺周期行业轮动 | 商品信号驱动的有色/能源/农业轮动 | [cyclical-rotation-strategy.md](./cyclical-rotation-strategy.md) |
| `prosperity_resonance` | 景气共振 | PEG + 趋势确认 + 回调入场三重共振 | [prosperity-resonance-strategy.md](./prosperity-resonance-strategy.md) |
| `new_stock_ice_reversal` | 次新情绪冰点反转 | 次新跌停潮冰点 → 次日反转次新篮 | [new-stock-ice-reversal-strategy.md](./new-stock-ice-reversal-strategy.md) |
| `chip_accumulation` | 筹码单峰密集突破 | 筹码集中 + 获利盘适中 + 突破筹码峰 | [chip-accumulation-strategy.md](./chip-accumulation-strategy.md) |

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
# 决策日池默认落盘到 out/pool_<strategy>_<mode>_<start>_<end>.json；不需要时关闭
python -m app.script backtest --request examples/backtest_shared_small_cap_zz399101.json --no-pool-dump
# 或指定落盘路径
python -m app.script backtest --strategy etf_rotation --pool-dump out/etf_pool_trace.json
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
