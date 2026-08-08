# 中小综指微盘策略说明

本文档描述横截面共享资金策略 `small_cap_zz399101`：在中小综指（399101）成分股中，选取流通市值最小的 N 只（默认 5），按决策日等权换仓。

实现代码：`backend/app/strategies/cross_section/small_cap_zz399101.py`
统一执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

## 1. 核心思想

一句话规则：**中小综指成分股里，流通市值最小的 5 只**。

它把“小市值溢价”约束在中小盘股票池内，比全 A 微盘更聚焦，也比菜场大妈更简单（无股息率/PEG/股价带硬过滤）。

默认参数示例：

```json
{
  "decision_interval": 20,
  "top_n": 5
}
```

## 2. 选股与调仓

每个调仓日 `T`：

1. 股票池：中小综指（399101）当前成分（可用 `symbols` 覆盖）
2. 可交易过滤：剔除 ST/退、疑似停牌、疑似涨跌停
3. 按 **流通市值** 升序排序（缺失时回退总市值）
4. 取前 `top_n` 只，等权持有

当前策略用 `strategy_params.decision_interval` 生成决策日，单位为**交易日**：

- `20`：约月频（策略默认）
- `5`：约周频
- `1`：每个交易日调仓

## 3. 示例请求参数

对照 `backend/examples/backtest_shared_small_cap_zz399101.json`。公共字段总表见 [策略总览](./strategy-guide.md)。

### 3.1 请求级字段

| 字段 | 示例值 | 含义 |
| --- | --- | --- |
| `strategy_id` | `small_cap_zz399101` | 本横截面策略 id |
| `mode` | `universe` | `universe`=共享资金回测；`screen`=仅截面选股 |
| `data_source` | `a_stock_data` | 行情数据源 |
| `universe` | `zz399101` | 股票池为中小综指成分（`symbols` 为空时生效） |
| `symbols` | `[]` | 显式代码列表；非空时覆盖 `universe` |
| `max_universe` | `80` | 股票池上限（演示抽样；正式可加大） |
| `seed` | `42` | 超出上限时随机抽样的种子，保证可复现 |
| `start_date` / `end_date` | `2020-01-01` / `2026-12-31` | 回测区间 |
| `initial_cash` | `100000` | 初始资金（元） |
| `commission` | `0.0003` | 佣金费率（万三） |
| `min_commission` | `5.0` | 单笔最低佣金（元） |
| `slippage` | `0.01` | 单边滑点 1%（微盘流动性差，建议保留） |
| `lot_size` | `100` | 买入整手数（股） |
| `use_cache` | `true` | 使用本地估值缓存 |
| `force_refresh` | `false` | 不强制重新拉取 |
| `max_workers` | `8` | 并行拉取线程数 |
| `output_options.output` | `out/backtest_shared_small_cap_zz399101.json` | 结果 JSON 路径 |
| `output_options.plot` | `out/backtest_shared_small_cap_zz399101.svg` | 权益曲线图路径 |
| `output_options.report` | `out/backtest_shared_small_cap_zz399101.html` | 交互 HTML 报告（调仓买卖/区间收益）；未写时若有 `plot` 则自动派生同名 `.html` |
| `output_options.json` | `true` | 是否向 stdout 打印完整 JSON |

### 3.2 `strategy_params`

| 参数 | 示例值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `decision_interval` | `30` | 20 | 当前策略生成决策日的交易日间隔；示例沿用原 30 日配置，并非引擎强制定时 |
| `top_n` | `5` | 5 | 持仓只数（流通市值最小的 N 只） |
| `exclude_st` | `true` | true | 剔除名称含 ST/退 的标的 |
| `exclude_limit` | `true` | true | 剔除疑似涨跌停（按涨跌幅阈值） |
| `exclude_suspended` | `true` | true | 剔除疑似停牌（当日无估值行情） |
| `limit_pct_threshold` | `9.5` | 9.5 | 涨跌停近似阈值（%） |
| `min_price` | `0.0` | 0 | 最低价过滤；`0` 表示不限制 |
| `max_price` | `null` | null | 最高价过滤；`null` 表示不限制 |

## 4. 局限

- 成分股为**当前**中小综指成分，历史回测有幸存者偏差
- 微盘股流动性差，冲击成本高；默认建议保留 1% 滑点
- 全成分（约 900+）首次拉估值会较慢；演示可用 `max_universe` + `seed` 抽样

## 5. 运行示例

```bash
cd backend
python -m app.script backtest --list-strategies

# 截面选股
python -m app.script backtest --strategy small_cap_zz399101 --mode screen \
  --max-universe 80 --seed 42 --json

# 共享资金回测（同时写出 SVG/PNG 与交互 HTML 报告）
python -m app.script backtest --request examples/backtest_shared_small_cap_zz399101.json

# 已有 JSON 时离线生成报告
python -m app.cli_backtest_report out/backtest_shared_small_cap_zz399101.json
```
