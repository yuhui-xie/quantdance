# 涨停回落埋伏策略说明

本文档描述横截面共享资金策略 `limit_up_pullback`：近 N 个交易日出现涨停且随后回落、股价未大幅上涨；再过滤高价、大市值、亏损，要求相对低位与均线、低延迟趋势线（LLT）略多/平台整理，按决策日等权换仓。

实现代码：`backend/app/strategies/cross_section/limit_up_pullback.py`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

> 思路来源：知乎「简然」涨停未大涨选股法 + 40 日涨停回落、月线低位、均线整理等人工规则的可量化近似。概念题材叠加请用 `concept_symbols` 白名单人工筛入。

## 1. 核心思想

涨停说明有资金关注，但若之后没有连板、也没有走出大幅主升，往往仍处在相对低位的整理区。策略在「有过脉冲、尚未充分定价」的小盘低价盈利股中埋伏，长线等权持有。

默认参数示例：

```json
{
  "decision_frequency": "monthly",
  "top_n": 10,
  "lookback_days": 40,
  "min_limit_ups": 1,
  "max_price": 20.0,
  "max_market_cap": 10000000000,
  "max_period_return": 0.20,
  "require_pullback_after_limit": true,
  "forbid_consecutive_limit_ups": true,
  "main_board_only": true
}
```

## 2. 选股规则（每个调仓日）

在股票池内，对调仓日 `T`：

1. **可交易**：剔除 ST/退、疑似停牌、当日疑似涨跌停。
2. **板块（可选）**：`main_board_only` 时仅保留沪深主板（含原中小板），排除创业板 / 科创板 / 北交所。
3. **涨停事件**：近 `lookback_days`（默认 40）个交易日内，日涨跌幅 ≥ `limit_up_pct`（默认 9.5%）的次数 ≥ `min_limit_ups`。
4. **涨停质量**
   - `forbid_consecutive_limit_ups`：窗口内不得出现连续两日涨停（排除连板）。
   - `require_pullback_after_limit`：至少一次涨停后次日涨跌幅 < 0（涨停后下跌，不要连涨）。
5. **涨幅克制**：观察窗口累计涨幅 ≥ `min_period_return` 且 **严格小于** `max_period_return`（默认 0% ≤ 涨幅 < 20%）。
6. **价格 / 市值 / 盈利**：收盘价 ∈ `[min_price, max_price]`；总市值 ≤ `max_market_cap`；`require_profit` 时要求 `PE(TTM) > 0`。
7. **相对低位**：近 `position_lookback`（默认 120）日高低点分位 ≤ `max_price_position`（默认 0.5）。
8. **均线形态**（满足其一即可）
   - 略微多头：MA5/10/20 近似多排且 LLT 趋势向上；
   - 平台整理：近 `platform_days` 日振幅 ≤ `platform_max_range`。
9. **概念叠加（可选）**：`concept_symbols` 非空时，仅保留白名单内标的。
10. **排序持仓**：按总市值升序，其次涨停次数多、分位更低、窗口涨幅更小；取前 `top_n` 只等权。

当前策略默认 `decision_frequency="monthly"`，决策日**锚定在自然月月末**；可改
`weekly`（ISO 周周末）或 `daily`（每个交易日，配合 `decision_warmup` 冷启动）。
决策频率统一锚定自然周期而非回测起始日，因此回测结果不随起始日相位漂移。
这是策略规则，不是共享资金引擎强制的调仓周期。

## 3. 示例请求参数

对照 `backend/examples/backtest_shared_limit_up_pullback.json`。公共字段总表见 [策略总览](./strategy-guide.md)。

### 3.1 请求级字段

| 字段                          | 示例值                                   | 含义                                               |
| ----------------------------- | ---------------------------------------- | -------------------------------------------------- |
| `strategy_id`               | `limit_up_pullback`                    | 本横截面策略 id                                    |
| `mode`                      | `universe`                             | `universe`=共享资金回测；`screen`=仅截面选股   |
| `data_source`               | `a_stock_data`                         | 行情数据源                                         |
| `universe`                  | `all_a`                                | `symbols` 为空时用全 A 股票池                    |
| `symbols`                   | `[]`                                   | 显式代码列表；非空时覆盖`universe`               |
| `max_universe`              | `80`                                   | 股票池上限（1~10000；全 A 约设 `6000` 即可）     |
| `seed`                      | `42`                                   | 抽样种子，保证可复现                               |
| `start_date` / `end_date` | `2023-01-01` / `2024-12-31`          | 回测区间                                           |
| `initial_cash`              | `100000`                               | 初始资金（元）                                     |
| `commission`                | `0.0003`                               | 佣金费率                                           |
| `min_commission`            | `5.0`                                  | 单笔最低佣金（元）                                 |
| `slippage`                  | `0.01`                                 | 单边滑点 1%                                        |
| `lot_size`                  | `100`                                  | 买入整手数（股）                                   |
| `position_management`      | 见下文                                  | 系统化渐进建仓、加仓、止损与追踪止盈                 |
| `use_cache`                 | `true`                                 | 使用本地估值缓存                                   |
| `force_refresh`             | `false`                                | 不强制重新拉取                                     |
| `max_workers`               | `8`                                    | 并行拉取线程数                                     |
| `output_options.output`     | `out/backtest_shared_limit_up_pullback.json` | 结果 JSON 路径                               |
| `output_options.report`       | `out/backtest_shared_limit_up_pullback.html`  | 交互报告路径                               |
| `output_options.json`       | `false`                                | 是否向 stdout 打印完整 JSON                        |

示例中最大持仓 5 只，因此每只股票的资金上限为初始资金的 20%；首次投入该上限的
50%，股价相对首次成交价每上涨 10% 加仓上限的 25%，直到达到单票上限。相对加权
平均成本亏损 10% 时止损；浮盈达到 30% 后启动追踪止盈，从启动后的最高价回撤
10% 时卖出。

### 3.2 `strategy_params`

| 参数                                   | 示例值                  | 默认    | 说明                                          |
| -------------------------------------- | ----------------------- | ------- | --------------------------------------------- |
| `decision_frequency`                | `monthly`             | `monthly`| 决策频率：`daily`=每日（冷启动期后）/ `weekly`=每周末 / `monthly`=每自然月末；均锚定自然周期，与回测起始日无关 |
| `decision_every_n`                  | `1`                   | 1       | 决策步长：`monthly`+3=季末、`weekly`+2=双周；`daily` 忽略 |
| `decision_warmup`                   | `20`                  | 20      | 冷启动期（交易日），仅 `daily` 生效 |
| `top_n`                              | `10`                  | 10      | 最终持仓只数                                  |
| `lookback_days`                      | `40`                  | 40      | 涨停观察窗口（交易日）                        |
| `min_limit_ups`                      | `1`                   | 1       | 窗口内最少涨停次数（可改为 2）                |
| `limit_up_pct`                       | `9.5`                 | 9.5     | 涨停近似阈值（%）；20cm 板需提高              |
| `require_pullback_after_limit`       | `true`                | true    | 至少一次涨停后次日下跌（不要连涨）            |
| `forbid_consecutive_limit_ups`       | `true`                | true    | 排除窗口内连续两日涨停（连板）                |
| `min_period_return`                  | `0.0`                 | 0.0     | 观察窗口累计涨幅下限（默认 0=窗口内股价未亏损） |
| `max_period_return`                  | `0.20`                | 0.20    | 观察窗口累计涨幅上限（严格小于，默认 <20%）   |
| `min_price` / `max_price`          | `2.0` / `20.0`      | 2 / 20  | 价格带（元）                                  |
| `max_market_cap`                     | `1e10`                | 1e10    | 总市值上限（元），约 100 亿                   |
| `require_profit`                     | `true`                | true    | 要求`PE(TTM) > 0`（近似非亏损）             |
| `position_lookback`                  | `120`                 | 120     | 相对低位观察窗口（交易日）                    |
| `max_price_position`                 | `0.5`                 | 0.5     | 现价在窗口高低点中的最高分位                  |
| `ma_fast` / `ma_mid` / `ma_slow` | `5` / `10` / `20` | 5/10/20 | 均线形态用的三档 SMA 周期                     |
| `allow_mild_ma_up`                   | `true`                | true    | 允许「略微多头」形态通过                      |
| `llt_period`                         | `20`                  | 20      | LLT 平滑周期                                  |
| `llt_slope_lookback`                 | `5`                   | 5       | LLT 趋势斜率回看交易日                        |
| `allow_platform`                     | `true`                | true    | 允许「平台整理」形态通过                      |
| `platform_days`                      | `20`                  | 20      | 平台振幅观察天数                              |
| `platform_max_range`                 | `0.15`                | 0.15    | 平台振幅上限`(high-low)/mean`               |
| `concept_symbols`                    | `[]`                  | []      | 概念白名单；空=不启用，非空则仅保留名单内标的 |
| `main_board_only`                    | `true`                | true    | 仅沪深主板（含原中小板），排除创业/科创/北交  |
| `exclude_st`                         | `true`                | true    | 剔除 ST/退                                    |
| `exclude_limit`                      | `true`                | true    | 剔除疑似涨跌停                                |
| `exclude_suspended`                  | `true`                | true    | 剔除疑似停牌                                  |
| `limit_pct_threshold`                | `9.5`                 | 9.5     | 涨跌停近似阈值（%，可交易过滤用）             |

## 4. 数据说明与局限

- 行情/估值：东财 `stock_value_em`（与其他组合策略相同），含 `close`、`pct_change`、市值、`pe_ttm`。
- 涨停为涨跌幅阈值近似，不是交易所正式涨停标记；默认 `main_board_only=true` 已排除 20cm 板；若关闭该开关，需自行提高 `limit_up_pct`。
- 主板判定按代码前缀：保留 `60xxxx`、`000/001/002/003`；排除 `300/301`（创业）、`688/689`（科创）、`4/8/92` 开头（北交）。
- 「月线低位」用日线高低分位近似，不是真正的月 K 结构。
- 概念题材无内置数据源，需自行维护 `concept_symbols`。
- 股票池 `max_universe` 较小时，通过全部过滤的标的可能很少；全市场复现需加大股票池。

## 5. 运行示例

```bash
cd backend

# 截面选股
python -m app.script backtest --strategy limit_up_pullback --mode screen \
  --max-universe 80 --seed 42 --json

# 共享资金回测
python -m app.script backtest --request examples/backtest_shared_limit_up_pullback.json

# 概念白名单：在 examples/backtest_shared_limit_up_pullback.json 的
# strategy_params.concept_symbols 中填入代码后，用 --request 运行
```

若候选过少：可放宽 `max_period_return`、`max_price_position`、`max_market_cap`，或将 `min_limit_ups` 保持为 1、暂时关闭 `forbid_consecutive_limit_ups`。

## 6. 改进方向

- 接入概念/题材数据做自动叠加打分
- 用月线/周线真正的结构低点替代日线分位
- 涨停后回撤深度、换手与封板质量等微观结构因子
