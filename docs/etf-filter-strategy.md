# ETF 动态池筛选策略说明

本文档描述横截面共享资金策略 `etf_filter`：**从历史时点存在的 ETF 池（而非固定静态池）中，用近 N 日成交额/成交量做流动性过滤，筛出一批可交易的标的，再做动量排序取 top-N**，可选按行业均衡（每行业最多 K 只）。它与 `etf_rotation` 的区别在于：

- `etf_rotation`：`requires_symbols=True`，必须显式提供 `symbols`（通常用 `etf_core` 静态精选池），纯价格动量选股，无流动性过滤。
- `etf_filter`：`requires_symbols=False`，默认走 `universe=etf` 全市场预设；**按决策日 asof 重构"当时已上市"的 ETF 池**（避开幸存者偏差），再叠加流动性过滤与行业均衡。

实现代码：`backend/app/strategies/cross_section/etf_filter.py`
**可复用 as-of 池模块**：`backend/app/strategies/cross_section/asof_pool.py`（`panel_existing_at` 按调仓日 asof 用 K 线覆盖过滤出"当时已上市"的子池，池子无关，`universe="etf"` 全市场与 `universe="etf_core"`/`config:*` 精选池都适用）
as-of 池（universe 层）重构：`backend/app/data_sources/market_data.py::fetch_etf_universe_at` + `filter_etf_symbols_at`（经 `backend/app/universe.py::resolve_universe_rows` 接入 `universe="etf"` / `etf_dynamic` / `etf_asof`，config 池经 `_maybe_asof_filter_config`）

> 相关策略：`etf_rotation` / `etf_rotation_3factor` 面向已选定的显式 ETF 池；本策略面向"从全市场按历史时点动态筛一批"的场景。

## 1. 策略概述

```
┌───────────────────────────────────────────────┐
│  全市场当前 ETF 列表（fund_etf_spot_ths 备用链）│
└──────────────────┬────────────────────────────┘
                   │ 每只：K 线首根 ≤ 决策日 asof → 当时已上市
                   ▼
┌───────────────────────────────────────────────┐
│  1. 存在性  首根 K 线 ≤ asof（尚未上市则剔除） │
│  2. 可交易  非停牌 / 非涨跌停（asof_tradeable_row）│
│  3. 流动性  近 liquidity_days 日均成交额/量 ≥ 阈值│
│  4. 动量    simple_momentum(close, momentum_days)│
└──────────────────┬────────────────────────────┘
                   │ 按动量降序排名
                   ▼
┌───────────────────────────────────────────────┐
│  可选行业均衡：每行业最多 per_industry_top_k 只 │
│  取 top_n 只；无候选通过过滤 → 空/现金          │
└───────────────────────────────────────────────┘
```

**两层 as-of**：
1. **universe 层（预筛）**：解析 `universe` 时若请求带 `start_date`，`resolve_universe_rows` 走 `fetch_etf_universe_at(start_date, ...)` / `_maybe_asof_filter_config(...)`：先取当前全量 ETF（或 config 精选池）列表，再对每只用其 K 线覆盖判定是否在 `start_date` 已上市（**首根 K 线 ≤ asof 即视为已上市**），剔除首根晚于该日的标的。结果在 `universe_note` 中注明"XX 只已存在，YY 只跳过"。ETF 退市极少，用"当前列表 + K 线起始日"近似重构历史池是稳健的。
2. **决策层（逐日）**：`select()` 内用 `asof_pool.panel_existing_at(ctx.panel, asof)`，在每个决策日 asof 再次按 K 线覆盖过滤出"当时已存在"的子池（`universe_note` 的预筛是保守的，此处会按决策日精确重算）。

`universe` 取值：
- `etf` / `etf_dynamic` / `etf_asof`（全市场）：带 asof 时按上述逻辑动态重构，避开幸存者偏差；无 asof 时回退当前全量列表。
- `etf_core` 或 `config:xxx`（config 精选池）：同样按 asof 过滤出当时已存在的子集（复用 `filter_etf_symbols_at`）。

## 2. 参数说明

全部参数通过 `strategy_params` 传入（Pydantic 校验）。除下列字段外，还继承 `DecisionFrequencyParams` 的 `decision_frequency` / `decision_every_n` / `decision_warmup`。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `top_n` | `10` | 最终保留的 ETF 数量上限（1–50） |
| `liquidity_days` | `20` | 流动性回看交易日数（取日均成交额/量） |
| `min_amount` | `None` | 日均成交额下限（元）；`None` 不启用成交额过滤 |
| `min_volume` | `None` | 日均成交量下限；`None` 不启用成交量过滤 |
| `momentum_days` | `60` | 动量回看交易日数 |
| `min_momentum` | `0.0` | 最低区间收益率；`0` 表示只保留正动量 ETF |
| `per_industry_top_k` | `0` | 行业均衡：每行业最多选几只；`0` 关闭（纯全局动量排序） |
| `exclude_limit` / `exclude_suspended` | `True` | 调仓日排除涨跌停 / 停牌标的 |
| `limit_pct_threshold` | `9.5` | 判定涨跌停的涨跌幅阈值（%） |

## 3. 流动性过滤口径

- 对每只候选，取截至决策日 asof 的最近 `liquidity_days` 个交易日的**日均成交额**（`amount` 列，单位元）与**日均成交量**（`volume` 列）。
- `min_amount` / `min_volume` 任一设置后，对应日均值低于阈值的标的被剔除。
- 若某标的 K 线无 `amount` 列，则 `avg_amount` 为 `None`；此时若设置了 `min_amount`，该标的因无法确认流动性而被剔除（保守 fail-closed）。`min_volume` 同理。
- 流动性是"能否实际成交"的可交易性指标，比规模/市值更适合作筛选依据，且可按日从 K 线取得。

## 4. 行业均衡与行业推断

`per_industry_top_k>0` 时，策略先按 ETF **名称关键词**推断行业（`infer_industry`），再按动量降序依次取标的，**每行业最多保留 `per_industry_top_k` 只**，直到凑满 `top_n`。关闭（`0`）时不做均衡，直接按全局动量取 top_n。

行业关键词映射（按顺序首个命中，见 `asof_pool.py::_INDUSTRY_RULES`）：宽基、半导体、科技、医药、消费、金融、新能源、资源周期、军工、贵金属、债券、红利、海外、传媒、房地产，未命中兜底「其他」。

> 说明：行业由名称关键词推断，非交易所官方板块分类；命名含板块词的行业 ETF 命中较准，宽基/跨板块指数归「宽基」，不保证与申万/中信行业一致。

## 5. 使用示例

`screen` 模式（单时点选股，asof = `start_date`）：

```bash
cd backend
.venv/Scripts/python.exe -m app.script backtest \
  --request examples/cross_section/backtest_shared_etf_filter.json --json
```

请求文件要点：`strategy_id: etf_filter`，`mode: screen`，`universe: etf`（全市场，按 asof 重构动态池），`end_date` 即截面决策日；`strategy_params` 设置 `min_amount`、`liquidity_days`、`per_industry_top_k` 等。

`screen` 用 `etf_dynamic`（等价别名）或 config 精选池：

```bash
cd backend
.venv/Scripts/python.exe -m app.script backtest --strategy etf_filter --mode screen \
  --universe etf_dynamic --start-date 2024-01-01 --end-date 2024-06-28 --max-universe 200 --json
.venv/Scripts/python.exe -m app.script backtest --strategy etf_filter --mode screen \
  --universe etf_core --start-date 2024-01-01 --end-date 2024-06-28 --json
```

`universe` 回测（按决策日逐月筛选换仓）：

```bash
cd backend
.venv/Scripts/python.exe -m app.script backtest --strategy etf_filter --mode universe \
  --universe etf --start-date 2023-01-01 --end-date 2026-08-31 \
  --max-universe 300 --top-n 10 --json
```

## 6. 局限

- as-of 池按"当前列表 + K 线起始日"近似重构，**ETF 清盘退市会被低估**（幸存者偏差残留，属可接受近似）。
- 行业靠名称关键词推断，可能与交易所/申万分类不一致。
- `min_amount` 依赖 K 线的 `amount` 列；个别源无该列时会保守剔除，需留意。
- 全市场池较大时，as-of 重构与面板加载会拉较多 K 线，受 `max_universe` / `seed` 控制。
