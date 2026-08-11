# 筹码单峰密集突破策略说明

本文档描述横截面共享资金策略 `chip_accumulation`：在决策日对股票池中每股用近期带换手率的日线计算筹码成本分布（复用 `chip_cost_distribution`），筛选"筹码高度集中 + 获利盘适中 + 价格站上成本区并突破筹码峰"的个股，按筹码集中度优先选取 top-N，共享账户等权持有。

实现代码：`backend/app/strategies/cross_section/chip_accumulation.py`
筹码分布指标：`backend/app/indicators/chip_distribution.py`
数据源（含换手率）：`backend/app/data_sources/market_data.py::fetch_a_share_daily_turnover`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

> 筹码分布是基于历史换手率衰减的**统计估算**，并非真实持仓数据。换手率口径可能因送转/增发/解禁导致的股本变动而失真；本策略的选股结论仅供研究参考，不构成投资建议。

## 1. 核心思想

"筹码单峰密集突破"是经典的筹码理论形态：当流通筹码高度集中在狭窄的价格带内（单峰密集），说明主力吸筹已基本完成、浮筹被清洗；随后价格站上平均成本并突破筹码峰，意味着上方套牢盘已被消化、上行空间打开。

策略在决策日对每股计算筹码指标，选出同时满足以下条件的个股：

1. **筹码高度集中**：90% 成本集中度 `concentration_90` 低于阈值，筹码单峰密集；
2. **获利盘适中**：`profit_ratio` 落在 `[profit_ratio_min, profit_ratio_max]`，既非深套也非严重超买；
3. **站上成本区**：收盘价高于平均成本 `average_cost`，套牢盘基本消化；
4. **突破筹码峰**：收盘价不低于筹码峰值 `peak_price`，突破最密集成本位。

默认参数示例：

```json
{
  "decision_frequency": "monthly",
  "top_n": 10,
  "lookback_bars": 120,
  "bins": 200,
  "profit_ratio_min": 0.35,
  "profit_ratio_max": 0.90,
  "concentration_max": 0.50,
  "require_price_above_cost": true,
  "require_breakout_peak": true,
  "exclude_st": true
}
```

## 2. 选股规则（每个决策日）

对决策日 `T`、股票池内每股：

1. **可交易**：剔除 ST/退（`exclude_st`）、疑似停牌、当日疑似涨跌停。
2. **取筹码窗口**：取截止 `T` 的最近 `lookback_bars`（默认 120）根、换手率有效（>0）的日线。
3. **算筹码指标**：调 `chip_cost_distribution(slice, bins=200)` 得到末日筹码指标（获利盘 `profit_ratio`、平均成本 `average_cost`、筹码峰 `peak_price`、90% 集中度 `concentration_90`、90% 成本区间 `interval_90_low/high`）。
4. **过滤**（任一不满足则剔除）：
   - `profit_ratio_min <= profit_ratio <= profit_ratio_max`；
   - `require_price_above_cost` 时 `close > average_cost`；
   - `concentration_90 <= concentration_max`；
   - `require_breakout_peak` 时 `close >= peak_price`。
5. **打分排序**：主键为 `concentration_90` 升序（筹码越集中越优先），次键为突破幅度 `(close - peak_price) / peak_price` 降序。
6. **取 top_n 等权持有**。

决策频率默认 `monthly`（锚定自然月月末），可改 `weekly`（ISO 周周末）或 `daily`（每个交易日，配合 `decision_warmup` 冷启动）。决策频率统一锚定自然周期而非回测起始日，因此回测结果不随起始日相位漂移。

## 3. 示例请求参数

公共字段总表见 [策略总览](./strategy-guide.md)。请求级字段与 `limit_up_pullback` 等横截面策略一致（`strategy_id` / `mode` / `universe` / `max_universe` / `seed` / `start_date` / `end_date` / `initial_cash` / `commission` / `min_commission` / `slippage` / `lot_size` / `output_options.*` 等）。

### 3.1 `strategy_params`

| 参数 | 示例值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `decision_frequency` | `monthly` | `monthly` | 决策频率：`daily`=每日（冷启动期后）/ `weekly`=每周末 / `monthly`=每自然月末；均锚定自然周期，与回测起始日无关 |
| `decision_every_n` | `1` | 1 | 决策步长：`monthly`+3=季末、`weekly`+2=双周；`daily` 忽略 |
| `decision_warmup` | `20` | 20 | 冷启动期（交易日），仅 `daily` 生效 |
| `top_n` | `10` | 10 | 最终持仓只数 |
| `lookback_bars` | `120` | 120 | 筹码分布回溯的交易日 K 线数量（30~1000） |
| `bins` | `200` | 200 | 筹码分布价格网格数（50~1000，越小计算越快） |
| `profit_ratio_min` | `0.35` | 0.35 | 获利盘比例下限（过滤仍处深套的个股） |
| `profit_ratio_max` | `0.90` | 0.90 | 获利盘比例上限（过滤严重超买、追高风险的个股） |
| `concentration_max` | `0.50` | 0.50 | 90% 成本集中度上限，越小表示筹码越集中（单峰越密集） |
| `require_price_above_cost` | `true` | true | 要求收盘价站上平均成本（套牢盘基本消化） |
| `require_breakout_peak` | `true` | true | 要求收盘价不低于筹码峰（突破最密集成本位） |
| `exclude_st` | `true` | true | 剔除 ST/退市风险股票 |

> 本策略通过 `needs_turnover=True` 让横截面数据面板改用含换手率的腾讯财经日线加载，无需单独配置数据源。

## 4. 数据说明与局限

- **换手率**：来自腾讯财经 `newfqkline`（`fetch_a_share_daily_turnover`），为交易所口径的真实历史换手率（已换算小数）；价格前复权，除权缺口不会使成本分布跳变失真。
- **筹码分布口径**：与通达信/同花顺的换手衰减算法思路一致（`backend/app/indicators/chip_distribution.py`），但不同软件对换手率口径、衰减系数与区间分档处理不同，指标仅供参考。
- **局限**：
  - 算法假设换手筹码仅在当日 `[low, high]` 区间换手，忽略日内更细成交结构；
  - 换手率可能因送转/增发/解禁导致的股本变动而失真，上市早期、长期停牌等阶段偏差更大；
  - 数据拉取失败时报 `MarketDataError`（CLI 退出码 3），符合项目「行情失败硬失败」约定；
  - 股票池较小时通过全部过滤的标的可能很少，需加大 `max_universe`。

## 5. 运行示例

```bash
cd backend

# 截面选股（仅返回最近一个决策日的选股结果）
python -m app.script backtest --strategy chip_accumulation --mode screen \
  --universe zz500 --max-universe 80 --seed 42 --json

# 共享资金回测（需同时提供 start_date 与 end_date）
python -m app.script backtest --strategy chip_accumulation --mode universe \
  --universe zz500 --max-universe 80 --seed 42 \
  --start-date 2023-01-01 --end-date 2024-12-31 --json
```

若候选过少：可放宽 `concentration_max`、`profit_ratio_max`，或关闭 `require_price_above_cost` / `require_breakout_peak` 之一。

## 6. 改进方向

- 加入筹码峰下方支撑（成本区间下沿）与集中度变化趋势（吸筹斜率）作为加分项；
- 用滚动 `chip_cost_distribution` 构造获利盘/集中度的时序动量；
- 结合换手率异动（量能脉冲）确认突破的持续性。
