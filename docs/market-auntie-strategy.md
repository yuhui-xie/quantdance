# 菜场大妈选股策略说明

本文档描述项目中的 `market_auntie` **多标的组合**策略：质好、价低、市值小，按月等权调仓。

实现代码：

- 选股插件：`backend/app/portfolio/market_auntie.py`
- 统一执行：`backend/app/portfolio/runner.py`
- 组合再平衡引擎：`backend/app/portfolio_engine.py`
- 基本面数据：`backend/app/data_sources/em_fundamentals.py`（东财 `stock_value_em` + 巨潮分红）

> 该策略属于低频组合类目（`portfolio`），不是单票 `StrategySpec`；请用 `portfolio`，不要用 `backtest --strategy`。

## 1. 核心思想

把选股类比成买菜：优先买「质量尚可、价格便宜、盘子更小」的股票，并利用 A 股长期存在的小市值与低价股溢价（并非永远有效，且容量有限）。

主流规则可概括为七个字：**质好价低市值小**。

默认参数示例：

```json
{
  "top_n": 10,
  "min_price": 2.0,
  "max_price": 9.0,
  "max_peg": 1.0,
  "require_dividend": true,
  "require_peg": true
}
```

## 2. 选股规则（每个调仓日）

在股票池内，对调仓日 `T`：

1. **价低**：收盘价 ∈ `[min_price, max_price]`（默认 2~9 元；下限对应面值退市缓冲）。
2. **质量（质好）**  
   - 股息率：近 365 日现金分红合计 / `T` 日收盘价，且（默认）严格大于 `min_dividend_yield`  
   - PEG：要求 `min_peg < PEG <= max_peg`（默认 `0 < PEG <= 1`）
3. **可交易过滤**：剔除名称含 ST/退；剔除疑似停牌（`T` 日无估值行情）；剔除涨跌幅绝对值 ≥ `limit_pct_threshold`（默认 9.5%）的疑似涨跌停。
4. **市值小**：在上述过滤后，按总市值升序取前 `top_n` 只，等权作为目标持仓。

调仓间隔由请求参数 `rebalance_freq` 控制，单位为**交易日**（默认 `20`≈月频；`5`≈周频）。

## 3. 交易与费用模型

- 卖出旧持仓后再等权买入新目标。
- 成交价基于调仓日收盘价，并叠加单边 `slippage`（默认 1%）。
- 佣金按成交金额比例收取，并应用 `min_commission`（默认 5 元/笔）。
- 买入数量按 `lot_size`（默认 100 股）向下取整。
- 每日权益按收盘价盯市。

## 4. 示例请求参数

对照 `backend/examples/portfolio_market_auntie.json`。公共字段总表见 [策略总览](./strategy-guide.md)。

### 4.1 请求级字段

| 字段 | 示例值 | 含义 |
| --- | --- | --- |
| `strategy_id` | `market_auntie` | 本组合策略 id |
| `mode` | `backtest` | `backtest`=周期调仓回测；`screen`=仅截面选股 |
| `data_source` | `a_stock_data` | 行情数据源 |
| `universe` | `all_a` | `symbols` 为空时用全 A 股票池 |
| `symbols` | `[]` | 显式代码列表；非空时覆盖 `universe` |
| `max_universe` | `60` | 股票池上限（演示用；全市场复现需加大） |
| `seed` | `42` | 抽样种子，保证可复现 |
| `start_date` / `end_date` | `2023-01-01` / `2024-12-31` | 回测区间 |
| `rebalance_freq` | `20` | 调仓间隔（交易日），`20`≈月频 |
| `initial_cash` | `100000` | 初始资金（元） |
| `commission` | `0.0003` | 佣金费率 |
| `min_commission` | `5.0` | 单笔最低佣金（元） |
| `slippage` | `0.01` | 单边滑点 1% |
| `lot_size` | `100` | 买入整手数（股） |
| `use_cache` | `true` | 使用本地估值/分红缓存 |
| `force_refresh` | `false` | 不强制重新拉取 |
| `max_workers` | `8` | 并行拉取线程数 |
| `output_options.output` | `out/portfolio_market_auntie.json` | 结果 JSON 路径 |
| `output_options.plot` | `out/portfolio_market_auntie.svg` | 权益曲线图路径 |
| `output_options.json` | `false` | 是否向 stdout 打印完整 JSON |

### 4.2 `strategy_params`

| 参数 | 示例值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `top_n` | `10` | 10 | 最终持仓只数（过滤后按市值升序取前 N） |
| `min_price` | `2.0` | 2 | 最低价（元）；对应面值退市缓冲 |
| `max_price` | `9.0` | 9 | 最高价（元）；「价低」上界 |
| `min_dividend_yield` | `0.0` | 0 | 股息率下限；见下项 |
| `require_dividend` | `true` | true | 为 true 时要求股息率**严格大于** `min_dividend_yield` |
| `min_peg` / `max_peg` | `0.0` / `1.0` | 0 / 1 | PEG 区间：`min_peg < PEG <= max_peg` |
| `require_peg` | `true` | true | 是否启用 PEG 过滤 |
| `exclude_st` | `true` | true | 剔除名称含 ST/退 |
| `exclude_limit` | `true` | true | 剔除疑似涨跌停 |
| `exclude_suspended` | `true` | true | 剔除疑似停牌 |
| `limit_pct_threshold` | `9.5` | 9.5 | 涨跌停近似阈值（%） |

## 5. 数据说明与局限

- **PEG / 市值 / 调仓日价格**：东财 `akshare.stock_value_em`，本地缓存于 `backend/data/em_fundamentals/`。
- **股息率**：巨潮分红折算 trailing 现金股息 / 现价，属于近似，不是交易所官方股息率 TTM。
- **ST**：使用当前股票池名称启发式过滤，**不是**历史 ST 日历。
- **涨跌停 / 停牌**：用涨跌幅与当日是否有行情近似，并非交易所正式状态。
- **容量**：小市值 + 低价股承载资金有限；文章回测也强调滑点对收益影响大，默认已加 1% 滑点。
- **股票池**：`max_universe` 默认较小（如 60~80）以便演示；全市场精确复现需更大池与更长拉取时间。

## 6. 运行示例

```bash
cd backend

# 截面选股（最新/指定日）
python -m app.script portfolio --mode screen --max-universe 80 --seed 42 --json

# 组合回测（建议先小股票池验证）
python -m app.script portfolio --request examples/portfolio_market_auntie.json

# CLI 等价写法
python -m app.script portfolio --mode backtest --universe all_a --max-universe 60 --seed 42 \
  --start-date 2023-01-01 --end-date 2024-12-31 \
  --slippage 0.01 --top-n 10 --max-price 9 --plot out/market_auntie.svg
```

若分红数据稀疏导致持仓过少，可临时加 `--no-dividend-filter`，或放宽 `--max-peg`。

## 7. 改进方向

- 更多质量因子（盈利质量、杠杆、机构持仓占比等）
- 涨跌停后的闲置资金再配置
- 历史 ST / 停牌正式日历
- 点时基本面严格对齐财报公告日，降低前视偏差
