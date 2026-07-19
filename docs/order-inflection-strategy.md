# 订单开工拐点策略说明

本文档描述组合策略 `order_inflection`：用财报五步法捕捉「订单回暖 → 开工备货 → 盈利与现金流跟上」的拐点，按周期等权调仓。

实现代码：

- 选股插件：`backend/app/portfolio/order_inflection.py`
- 财报数据：`backend/app/data_sources/financial_reports.py`（东财资产负债表 / 利润表 / 现金流量表）
- 估值行情：`backend/app/data_sources/em_fundamentals.py`
- 统一执行：`backend/app/portfolio/runner.py`

> 该策略属于低频组合类目（`portfolio`），不是单票 `StrategySpec`；请用 `portfolio`，不要用 `backtest --strategy`。

## 1. 核心思想

把基本面拆成五步，对应经营周期的先后顺序：

| 步骤 | 指标 | 含义 | 量化近似 |
| --- | --- | --- | --- |
| 1 | 合同负债 | 明天有活干 | 合同负债 > 0 且同比增速 ≥ 阈值 |
| 2 | 毛利率 | 明天的活赚不赚钱 | 毛利率足够高，且同比变动不太差 |
| 3 | 经营现金流 | 干活的钱够不够 | 经营现金流净额 > 0（可选同比） |
| 4 | 存货结构 | 马上要开工 | 存货同比落在「备货」区间（非滞销暴增） |
| 5 | 综合研判 | 拐点在哪 | 加权拐点分排序，取 TopN |

默认参数示例：

```json
{
  "top_n": 10,
  "min_contract_liab_yoy": 0.0,
  "min_gross_margin": 0.15,
  "min_gross_margin_yoy_delta": -0.02,
  "require_positive_ocf": true,
  "min_inventory_yoy": 0.0,
  "max_inventory_yoy": 80.0
}
```

## 2. 选股规则（每个调仓日）

在股票池内，对调仓日 `T`：

1. **可交易**：剔除 ST/退、疑似停牌、当日疑似涨跌停；价格 ∈ `[min_price, max_price]`；可选市值上限。
2. **时点财报**：取 `notice_date ≤ T` 的最近一期已公告报表（**按公告日，不是报告期末**），且公告距 `T` 不超过 `max_notice_age_days`。
3. **合同负债**：`require_contract_liab` 时要求合同负债 ≥ `min_contract_liab`，同比（%）≥ `min_contract_liab_yoy`。
4. **毛利率**：毛利率 = (营业收入 − 营业成本) / 营业收入；要求 ≥ `min_gross_margin`，且同比变动 ≥ `min_gross_margin_yoy_delta`。
5. **经营现金流**：默认要求净额 > 0；若设置 `min_ocf_yoy` 则额外过滤同比。
6. **存货**：存货 > 0，且同比 ∈ `[min_inventory_yoy, max_inventory_yoy]`（东财百分数口径）。
7. **综合评分**后取前 `top_n`：

```text
score = w_contract * 合同负债同比
      + w_margin   * 毛利率同比变动 * 100
      + w_ocf      * 经营现金流同比
      + w_inventory* soft(存货同比)
```

同分时市值更小者优先。调仓间隔由 `rebalance_freq` 控制（默认 20 交易日≈月频）。

## 3. 示例请求参数

对照 `backend/examples/portfolio_order_inflection.json`。公共字段总表见 [策略总览](./strategy-guide.md)。

### 3.1 请求级字段

| 字段 | 示例值 | 含义 |
| --- | --- | --- |
| `strategy_id` | `order_inflection` | 本组合策略 id |
| `mode` | `backtest` | `backtest`=周期调仓回测；`screen`=仅截面选股 |
| `data_source` | `a_stock_data` | 行情数据源 |
| `universe` | `all_a` | `symbols` 为空时用全 A 股票池 |
| `symbols` | `[]` | 显式代码列表；非空时覆盖 `universe` |
| `max_universe` | `40` | 股票池上限（三大报表拉取较慢，演示建议 30~60） |
| `seed` | `42` | 抽样种子，保证可复现 |
| `start_date` / `end_date` | `2023-01-01` / `2024-12-31` | 回测区间 |
| `rebalance_freq` | `20` | 调仓间隔（交易日）；财报低频更新，不宜过高频 |
| `initial_cash` | `100000` | 初始资金（元） |
| `commission` | `0.0003` | 佣金费率 |
| `min_commission` | `5.0` | 单笔最低佣金（元） |
| `slippage` | `0.01` | 单边滑点 1% |
| `lot_size` | `100` | 买入整手数（股） |
| `use_cache` | `true` | 使用本地财报/估值缓存（强烈建议开启） |
| `force_refresh` | `false` | 不强制重新拉取 |
| `max_workers` | `4` | 并行拉取线程数（报表接口宜保守） |
| `output_options.output` | `out/portfolio_order_inflection.json` | 结果 JSON 路径 |
| `output_options.plot` | `out/portfolio_order_inflection.svg` | 权益曲线图路径 |
| `output_options.json` | `false` | 是否向 stdout 打印完整 JSON |

> 示例文件中的 `__comments__` 仅作人类可读备注，运行时会被忽略。

### 3.2 `strategy_params`

| 参数 | 示例值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `top_n` | `10` | 10 | 持仓只数（按拐点分排序） |
| `min_contract_liab` | `0` | 0 | 合同负债绝对值下限（元） |
| `min_contract_liab_yoy` | `0.0` | 0 | 合同负债同比下限（%）；0=至少不萎缩 |
| `require_contract_liab` | `true` | true | 必须存在合同负债科目 |
| `min_gross_margin` | `0.15` | 0.15 | 毛利率下限（小数，15%） |
| `min_gross_margin_yoy_delta` | `-0.02` | -0.02 | 毛利率同比变动下限（小数，允许略降） |
| `require_gross_margin` | `true` | true | 是否启用毛利率过滤 |
| `require_positive_ocf` | `true` | true | 要求经营现金流净额 > 0 |
| `min_ocf_yoy` | `null` | null | 经营现金流同比下限（%）；`null`=不限制 |
| `min_inventory_yoy` / `max_inventory_yoy` | `0.0` / `80.0` | 0 / 80 | 存货同比备货窗口（%）；过高疑似滞销 |
| `require_inventory` | `true` | true | 是否启用存货过滤 |
| `w_contract` / `w_margin` / `w_ocf` / `w_inventory` | `1` / `1` / `0.5` / `0.3` | 同左 | 拐点分各因子权重 |
| `max_notice_age_days` | `200` | 200 | 财报公告日距调仓日最大天数（新鲜度） |
| `min_price` / `max_price` | `2.0` / `100.0` | 2 / 100 | 价格带（元） |
| `max_market_cap` | `null` | null | 总市值上限（元）；`null`=不限制 |
| `exclude_st` | `true` | true | 剔除 ST/退 |
| `exclude_limit` | `true` | true | 剔除疑似涨跌停 |
| `exclude_suspended` | `true` | true | 剔除疑似停牌 |
| `limit_pct_threshold` | `9.5` | 9.5 | 涨跌停近似阈值（%） |

## 4. 数据说明与局限

- **合同负债 / 存货**：东财 `stock_balance_sheet_by_report_em`（`CONTRACT_LIAB` / `INVENTORY` 及 YOY）。
- **毛利率**：利润表营业收入与营业成本现场计算，并与去年同期报告期对齐求变动。
- **经营现金流**：`stock_cash_flow_sheet_by_report_em` 的 `NETCASH_OPERATE`。
- **存货结构**：公开接口通常只有存货合计，无原材料/在产品/产成品拆分；本策略用**存货同比**近似「备货开工」，过高则视为滞销风险。
- **行业差异**：金融、部分消费/服务行业合同负债口径弱或缺失，会被过滤；更适合制造业、工程、设备等「接单—备货—交付」链条。
- **幸存者偏差 / ST**：股票池与名称为当前截面近似。
- **性能**：每票需拉三张报表，首次较慢；本地缓存于 `backend/data/financial_reports/`。演示请控制 `max_universe`。

## 5. 运行示例

```bash
cd backend
python -m app.script portfolio --list-strategies
# 截面选股
python -m app.script portfolio --strategy order_inflection --mode screen --max-universe 40 --seed 42 --json
# 周期调仓回测
python -m app.script portfolio --request examples/portfolio_order_inflection.json
```

## 6. 改进方向

- 接入存货明细（原材料/在产品占比上升）以更贴近「马上要开工」。
- 合同负债改为环比加速或连续两期改善，减少单期噪声。
- 叠加收入增速、应收账款/合同资产交叉验证交付质量。
- 分行业标准化阈值（毛利率、存货同比）。
