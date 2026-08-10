# 次新情绪冰点反转策略说明

本文档描述横截面共享资金策略 `new_stock_ice_reversal`：**次新股板块连续跌停潮 → 情绪冰点 → 冰点次日买入止跌/反转的次新篮（地天板优先、超跌次之），持有固定天数后全仓退出**。

实现代码：`backend/app/strategies/cross_section/new_stock_ice_reversal.py`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

> 核心思路：次新股聚集了市场上风险偏好最高的一批短线资金，是市场情绪最敏锐的"温度计"。当次新股板块都能被连续跌停（如两个跌停），市场已到情绪冰点；而 A 股以做多赚钱，极致冰点后必有反转。反转由这批高风偏资金选择次新作为突破口，尤其是"地天板"（前一日跌停、当日涨停）次新，最能带动情绪并促使大盘反弹。

## 1. 核心思想

```
次新板块连续 N 日跌停潮（冰点）
        ↓
情绪冰点确认（次日企稳）
        ↓
买入止跌/反转的次新篮（地天板优先 → 超跌 → 当日强度）
        ↓
持有固定决策日数（逐日止盈止损）→ 全仓退出
```

策略的三个阶段：

1. **次新识别**：上市不超过 `max_listed_days` 个交易日（默认 250 ≈ 1 年）的动态次新股池，只依赖价格数据。
2. **冰点检测**：次新板块连续 `crash_days` 日（默认 2 = "两个跌停"）出现跌停潮，视为情绪冰点。
3. **反转博弈**：跌停潮结束次日买入止跌/反转的次新篮，博取冰点后的反弹。

## 2. 决策与换仓（默认日频 + 事件驱动 + 增量）

- 默认 `decision_frequency="daily"`：每个交易日检查是否冰点、是否处于持有期。
- 信号属**极端事件型**：只在次新板块出现连续跌停潮时入场，其余时间空仓（净值保持现金）。
- 持有期由策略自身的状态机管理（`ctx.cache` 记录入场日、持仓篮、入场价与持有计数），到 `hold_days` 决策日数后全仓退出；持有期内逐日按 `stop_loss_pct` / `take_profit_pct` 剔除触发标的。
- 建议请求使用 `rebalance_mode="incremental"`：入场/退出/剔除时只交易差异，避免全仓重建重置成本基准（策略内止损按自身上场价计算）。

## 3. 冰点检测与入场时机

### 3.1 单日冰点度量

对每个交易日，计算次新股池的板块情绪度量：

- `ice_metric="limit_down_ratio"`（默认）：当日池内**跌停占比**。跌停判定按板块阈值：创业板（300/301）/科创板（688/689）为 ±20%，主板 ±10%；比例 ≥ `min_limit_down_ratio`（默认 0.30）即当日为冰点日。
- `ice_metric="avg_drop"`：当日池内平均涨跌幅 ≤ `crash_pct_threshold`（默认 -6.0%）即为冰点日。

### 3.2 连续冰点与入场时机

- 需连续 `crash_days`（默认 2）个交易日满足冰点条件，即"两个跌停"。
- `entry_timing="next_day"`（默认）：**跌停潮结束次日**买入——窗口截至 `asof` 前一交易日全部为冰点日，且 `asof` 当日已非冰点（跌停潮已止），避免在冰点日接飞刀。
- `entry_timing="on_ice"`：冰点当日的**收盘**买入（窗口含 `asof` 当日），捕捉更深的位置但承担次日继续下探的风险。

## 4. 选股规则（冰点触发后）

在次新股池内，对入场日 `T`：

1. **可交易过滤**：当日停牌（无 `T` 日 K 线）、今日封死跌停（`pct_change ≤ -limit_pct`，买不进）、价格不在 `[min_price, max_price]`、ST（`exclude_st`）均排除。
2. **地天板优先**（`prefer_ditianban=True`）：前收跌停（`T-1` 日 `pct ≤ -limit_pct`）且当日涨停（`T` 日 `pct ≥ +limit_pct`）的标的排最前——这是最强烈的反转信号。
3. **超跌次之**：`crash_depth` = 最近 `crash_days` 个交易日涨跌幅之和，越负越超跌、反弹弹性越大。
4. **当日强度兜底**：当日涨幅降序。
5. 取 `top_n` 只等权买入。

## 5. 持有与退出

- 入场日记录各标的 `entry_prices`（当日收盘价）。
- 持有期每个决策日：
  - 相对买入价跌幅 ≤ `stop_loss_pct`（默认 8%）→ 剔除（止损）；
  - 相对买入价涨幅 ≥ `take_profit_pct`（默认 25%）→ 剔除（止盈）；
  - `hold_count` 达到 `hold_days`（默认 5 个决策日，日频=交易日）→ 全仓退出。
- 持仓全部剔除或达到持有期后清仓，等待下一次冰点。

## 6. 参数表

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `decision_frequency` | `daily` | 决策频率：`daily`/`weekly`/`monthly` |
| `decision_every_n` | 1 | 决策步长 |
| `decision_warmup` | 20 | 日频冷启动期（交易日） |
| `max_listed_days` | 250 | 次新定义：上市不超过 N 个交易日（约 1 年） |
| `min_listed_days` | 5 | 至少上市 N 个交易日 |
| `ice_metric` | `limit_down_ratio` | 冰点度量：跌停占比 / 板块均跌幅 |
| `crash_days` | 2 | 冰点需连续 N 日（默认 2=两个跌停） |
| `min_limit_down_ratio` | 0.30 | 跌停占比下限 |
| `crash_pct_threshold` | -6.0 | 板块均跌幅阈值（%），`avg_drop` 模式 |
| `entry_timing` | `next_day` | 入场时机：`next_day`=次日企稳 / `on_ice`=当日收盘 |
| `limit_pct_threshold` | 9.5 | 主板涨跌停阈值；创业板/科创板自动 ×2 |
| `top_n` | 5 | 单次买入只数 |
| `hold_days` | 5 | 持有决策日数（日频=交易日） |
| `min_price` / `max_price` | 2.0 / 200.0 | 价格区间 |
| `min_pool_size` | 4 | 次新池最小标的数，不足不判定冰点 |
| `exclude_st` | `true` | 排除 ST |
| `prefer_ditianban` | `true` | 地天板次新优先 |
| `stop_loss_pct` | 0.08 | 持有期止损；`null` 关闭 |
| `take_profit_pct` | 0.25 | 持有期止盈；`null` 关闭 |

## 7. 运行示例

示例请求（全 A 抽样，只依赖价格数据）：

```bash
cd backend
python -m app.script backtest --request examples/backtest_shared_new_stock_ice_reversal.json
```

股票池：`universe="all_a"` + `max_universe` 抽样。次新股占比小，建议增大 `max_universe`
（默认示例 1000）或用 `symbols` 显式提供次新列表，以提高信号覆盖率。

## 8. 注意事项与风险

- **数据近似**：地天板用"前收跌停 + 当日收盘涨停"的收盘价近似，未用盘中价，与真实打板成交有差异；封死跌停标的已排除（买不进）。
- **样本覆盖**：随机抽样可能漏掉实际跌停的次新股，板块冰点用样本内次新的跌停占比近似，样本过小时信号稀少或噪音大（调大 `max_universe` 或调低 `min_limit_down_ratio` / `min_pool_size`）。
- **信号稀少**：连续跌停潮是极端事件，区间内可能无信号（净值保持现金），这符合策略"极致冰点才出手"的定位。
- **短线高波动**：次新股本身波动极大，止损 `stop_loss_pct` 与止盈 `take_profit_pct` 建议配合持有期使用，单票失败不影响整篮。
- **涨跌停成交限制**：涨停封板的标的实际可能买不到，回测按收盘价成交属于简化处理。
