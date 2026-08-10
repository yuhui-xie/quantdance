# 三因子ETF轮动策略说明

本文档描述横截面共享资金策略 `etf_rotation_3factor`：**用乖离动量、斜率动量、效率动量三个动量因子对 ETF 做加权评分，在跨市场 ETF 池（红利低波 / 创业板50 / 纳指 / 黄金）间轮动**，并通过 Z-Score 标准化消除量纲差异，用 1.5× 调仓阈值减少震荡期的频繁换仓。

实现代码：`backend/app/strategies/cross_section/etf_rotation_3factor.py`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

> 单一动量指标容易出现偶然性。这里对三种不同的动量因子做加权融合，类似集成学习的投票机制，可提高预测稳定性、降低过拟合风险。这与"多个量化策略组合的稳定性大于单策略"同理。

## 1. 策略概述

```
┌────────────────────────────────────────────────────┐
│              候选 ETF 池（默认 4 只跨市场/风格）     │
│  512890  红利低波ETF   （A股防御：红利 + 低波动）    │
│  159949  创业板50ETF   （A股成长：创业板50）         │
│  513100  纳斯达克100ETF（海外科技）                  │
│  518880  黄金ETF        （贵金属避险）               │
└────────────┬───────────────────────────────────────┘
             │ 每只 ETF 计算三个动量因子
             ▼
┌────────────────────────────────────────────────────┐
│          三因子模型（原始分，量纲各异）              │
│  乖离动量 bias_score                                │
│  斜率动量 slope_score                               │
│  效率动量 efficiency_score                          │
└────────────┬───────────────────────────────────────┘
             │ 横截面 Z-Score 标准化（均0 标准差1）
             ▼
┌────────────────────────────────────────────────────┐
│          加权融合 final_score = Σ wᵢ × zᵢ           │
│  默认等权 w=(1/3, 1/3, 1/3)，内部自动归一化          │
└────────────┬───────────────────────────────────────┘
             │ 按 final_score 降序排名
             ▼
┌────────────────────────────────────────────────────┐
│        1.5× 调仓阈值滞后带（防抖）                   │
│  持有当前冠军，挑战者须超过其得分 × 1.5 才切换       │
│  所有得分 ≤ min_score_to_hold 时空仓                │
└────────────────────────────────────────────────────┘
```

> 默认 ETF 池覆盖 A 股防御、A 股成长、海外科技、贵金属四个互不相关的市场与风格，可有效分散风险、捕捉轮动机会。可根据自身偏好替换标的，但需先充分回测。

### 默认参数示例

```json
{
  "decision_frequency": "monthly",
  "decision_every_n": 1,
  "bias_ma_days": 20,
  "bias_momentum_days": 25,
  "slope_days": 60,
  "efficiency_days": 60,
  "weight_bias": 0.333,
  "weight_slope": 0.333,
  "weight_efficiency": 0.333,
  "rebalance_threshold": 1.5,
  "min_score_to_hold": 0.0
}
```

> 决策频率与 `decision_every_n` 的含义与所有横截面策略一致：决策日锚定自然周期（日 / ISO 周末 / 自然月末），回测结果不随起始日漂移。

## 2. 三因子模型详解

对每只候选 ETF，在当前决策日 `T` 取其 `asof ≤ T` 的日线 OHLCV 历史，计算三个因子。

### 2.1 乖离动量因子（bias）

衡量价格相对于长期均线的偏离程度和趋势方向。

```
bias      = close / MA(close, bias_ma_days)          # 乖离度
recent    = bias 最近 bias_momentum_days 天
y         = recent / recent[0]                        # 归一化
x         = arange(bias_momentum_days)
slope     = 线性回归 slope(x, y)
bias_score = slope × 10000
```

- 当价格向上偏离均线且偏离趋势加强 → 处于强势上涨阶段；
- 当价格向下偏离且趋势加强 → 处于弱势下跌阶段；
- 捕捉价格的相对强弱变化。

### 2.2 斜率动量因子（slope）

通过线性回归分析价格趋势的强度与质量。

```
window      = close 最近 slope_days 天
normalized  = window / window[0]                      # 价格标准化
x           = arange(1, slope_days + 1)
slope, R²   = 线性回归 (x, normalized)
slope_score = 10000 × slope × R²
```

- `slope`（斜率）反映趋势陡峭程度，越大表示上涨趋势越强；
- `R²`（拟合优度）衡量趋势的线性度，越接近 1 越稳定；
- 综合得分同时考虑趋势强度与质量。

### 2.3 效率动量因子（efficiency）

衡量价格运行的有效性，考虑净移动距离与总波动的关系。

```
pivot       = (open + high + low + close) / 4         # 价格中枢
momentum    = 100 × ln(pivot[-1] / pivot[0])           # 对数动量
direction   = |ln(pivot[-1]) - ln(pivot[0])|           # 净移动距离
volatility  = Σ |diff(ln(pivot))|                      # 总移动距离
eff_ratio   = direction / volatility                   # 效率系数
eff_score   = momentum × eff_ratio
```

- 动量反映价格的整体变化幅度；
- 效率系数衡量价格运动的直接性，值越大走势越流畅；
- 挑选上涨流畅、震荡较少的标的。

## 3. Z-Score 标准化与加权融合

三个因子的数值范围与分布不同，直接在候选池内做 Z-Score 标准化：

```
zᵢ = (xᵢ - mean(x)) / std(x)
```

将原始动量数据转换为均值为 0、标准差为 1 的标准正态分布，消除量纲差异，使不同因子可比，防止某个因子因数值过大而主导最终得分。

样本不足 2 个或方差为 0 时，该因子标准化为全 0。随后加权融合：

```
final_score = w_bias × z_bias + w_slope × z_slope + w_eff × z_eff
```

权重在代码内部自动归一化（`wᵢ / Σw`），默认等权 `(1/3, 1/3, 1/3)`。

## 4. 调仓阈值机制

为避免频繁交易，策略引入调仓阈值滞后带（`rebalance_threshold`，默认 1.5）：

- 即使第二名超过第一名，也未必触发调仓：当前持仓（冠军）需在得分上**乘以 1.5**；
- 只有当挑战者得分 > 当前持仓得分 × 1.5 时，才切换持仓；
- 从而避免震荡期或两标的分差不大时的来回换仓，降低出手次数。

状态跨决策日保存在 `ctx.cache`。设为 `1.0` 可关闭滞后带，退化为始终持有最高分。当**最高得分 ≤ `min_score_to_hold`（默认 0）**时，组合持有现金。

## 5. 参数表

| 参数 | 类型 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `decision_frequency` | str | `"monthly"` | daily / weekly / monthly | 决策频率，同所有横截面策略的锚定逻辑 |
| `decision_every_n` | int | 1 | ≥1 | 决策步长：monthly+3=季末 |
| `decision_warmup` | int | 20 | 0-250 | 冷启动期（仅 daily 生效时跳过前 N 日） |
| `bias_ma_days` | int | 20 | 2-504 | 乖离因子：长周期均线（BIAS_N） |
| `bias_momentum_days` | int | 25 | 2-250 | 乖离因子：回归窗口（MOMENTUM_DAY） |
| `slope_days` | int | 60 | 2-504 | 斜率因子：归一化价格回归窗口 |
| `efficiency_days` | int | 60 | 2-504 | 效率因子：价格中枢动量与波动窗口 |
| `weight_bias` | float | 0.333 | 0-1 | 乖离动量权重（内部归一化） |
| `weight_slope` | float | 0.333 | 0-1 | 斜率动量权重 |
| `weight_efficiency` | float | 0.333 | 0-1 | 效率动量权重 |
| `rebalance_threshold` | float | 1.5 | 1.0-10.0 | 调仓阈值：挑战者须超过持仓得分 × 该倍数才切换；1.0 关闭 |
| `min_score_to_hold` | float | 0.0 | — | 最高加权得分 ≤ 该值时空仓（无粘性持仓时） |
| `exclude_limit` | bool | true | — | 是否排除疑似涨跌停的 ETF |
| `exclude_suspended` | bool | true | — | 是否排除疑似停牌的 ETF |
| `limit_pct_threshold` | float | 9.5 | 1.0-30.0 | 涨跌幅阈值（%），高于此值视为涨跌停 |

## 6. 示例请求参数

示例文件位于 `backend/examples/backtest_shared_etf_rotation_3factor.json`，以下逐字段说明。

### 请求级字段

| 字段 | 示例值 | 说明 |
| --- | --- | --- |
| `strategy_id` | `"etf_rotation_3factor"` | 策略标识 |
| `mode` | `"universe"` | 共享资金回测；也可用 `"screen"` 只查看当前截面选股 |
| `data_source` | `"a_stock_data"` | 行情数据源 |
| `symbols` | （空） | 策略内置默认池（`default_symbols`），无需手动指定 |
| `start_date` | `"2020-01-01"` | 回测起始日 |
| `end_date` | `"2026-07-31"` | 回测截止日 |
| `initial_cash` | `100000` | 初始资金（元） |
| `commission` | `0.0003` | 万三佣金费率 |
| `min_commission` | `5.0` | 单笔最低佣金（元） |
| `slippage` | `0.001` | 单边滑点 0.1% |
| `lot_size` | `100` | 每手股数（ETF 同 A 股） |
| `rebalance_mode` | `"incremental"` | 推荐增量换仓：只交易差异，保留共同持仓 |

### `strategy_params` 字段

| 字段 | 示例值 | 说明 |
| --- | --- | --- |
| `decision_frequency` | `"monthly"` | 每月末决策 |
| `decision_every_n` | `1` | 每月一次 |
| `bias_ma_days` | `20` | 乖离度用 20 日均线 |
| `bias_momentum_days` | `25` | 乖离率回归看最近 25 天 |
| `slope_days` | `60` | 斜率回归看 60 个交易日 |
| `efficiency_days` | `60` | 效率因子看 60 个交易日 |
| `weight_bias` | `0.333` | 乖离动量权重 |
| `weight_slope` | `0.333` | 斜率动量权重 |
| `weight_efficiency` | `0.333` | 效率动量权重 |
| `rebalance_threshold` | `1.5` | 挑战者须超过持仓得分 1.5 倍才调仓 |
| `min_score_to_hold` | `0.0` | 全负分时空仓 |

### 运行命令

```bash
cd backend

# 查看当前截面选股（含三因子明细与 held 标记）
python -m app.script backtest --strategy etf_rotation_3factor --mode screen --json

# 运行完整历史回测
python -m app.script backtest --request examples/backtest_shared_etf_rotation_3factor.json
```

## 7. 注意事项

1. **三因子是相对动量指标**：Z-Score 在候选池内横向标准化，候选越少，标准化越粗糙。默认 4 只池只是量化投资中"多因子投票"思路的一个简单示例，可自行替换标的。

2. **ETF 代码会随市场变化而失效**：基金清盘、更名、代码变更均可能导致数据拉取失败。请通过 `symbols` 显式指定，或直接编辑 `default_symbols`。

3. **阈值本质是滞后带**：`rebalance_threshold`（1.5）降低换手，但也可能让"明显更优但未达倍数"的标的晚一个周期才被选中；这是稳定性与灵敏度之间的权衡。

4. **空仓逻辑**：当所有 ETF 的加权得分 ≤ `min_score_to_hold` 时组合持现金。熊市中这会跑输指数，但也会显著减损。

5. **历史回测 ≠ 未来表现**：动量因子在趋势市表现好、在长期横盘震荡市会反复磨损。本策略仅供研究参考，不构成投资建议。
