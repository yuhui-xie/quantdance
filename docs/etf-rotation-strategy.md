# ETF 动量轮动策略说明（斜率版）

本文档描述横截面共享资金策略 `etf_rotation`：**在 ETF 池中选择归一化收盘价回归斜率
（×R²）最强的标的，按周期（默认每自然月首个交易日）调仓，等权持有 top-N**。策略在
`config/etf_core_pool.json` 及 `config/etf_core_sub_pool.json` 等精选 ETF 池上轮动，
用原始收盘价斜率做低延迟趋势评分，并可叠加原始趋势 / 波动率 / 近期涨幅过滤与"得分>0
才持有"的现金规则。

实现代码：`backend/app/strategies/cross_section/etf_rotation.py`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：
`backend/app/backtest/shared_engine.py`

> 该策略已移除早期基于 LLT（Low-Lag Trendline）的打分分支，现为**斜率单口径**：
> 排序得分统一取"归一化收盘价线性回归斜率×R² 的横截面 z 值"。

## 1. 策略概述

```
┌───────────────────────────────────────────────┐
│        候选 ETF 池（etf_core / etf_core_sub…） │
└──────────────┬────────────────────────────────┘
               │ 每只 ETF（调仓日 asof 时点）
               ▼
┌───────────────────────────────────────────────┐
│  1. 可交易过滤  非停牌 / 非涨跌停（asof_tradeable_row）│
│  2. 绝对过滤(可选) 原始趋势>0 / 年化波动率上限 / 近期涨幅上限│
│  3. 打分 slope_momentum(close, slope_days)     │
│     → 归一化收盘价回归斜率×R² 的横截面 z 值      │
│  4. 可选 require_positive_score：只留得分>0，全负→现金 │
└──────────────┬────────────────────────────────┘
               │ 按得分(z) 降序排名
               ▼
┌───────────────────────────────────────────────┐
│        取 top_n 只，等权持有                  │
│        rotate_threshold=1.0 → 关闭惰性、纯按得分调仓 │
│        无候选通过过滤 → 持有现金               │
│  5. RSRS 门控(可选) rsrs ≤ 阈值 → 本轮不买入（留现金）│
└───────────────────────────────────────────────┘
```

决策日默认由 `DecisionFrequencyParams` 调度：`decision_frequency=monthly`（默认，每自然
月首个交易日）、`weekly`、`biweekly`、`monthly_2x`、`monthly_nth`、`daily` 可选。`top_n`
默认 1，即每月只持有得分最高的 1 只 ETF。

**成交时点**由请求级字段 `execution_timing` 控制（作用于共享引擎，通用），三种取值：
`same_day_close`=决策日当日收盘 / `next_day_open`=决策日**次日开盘**（默认）/ `next_day_close`
=决策日次日收盘。决策始终只用当日收盘信息计算；`next_day_*` 模式把目标实际成交顺延一个
交易日（开盘成交取开盘价、收盘成交取次日收盘价），无未来信息泄露，更贴近现实。

## 2. 参数说明

全部参数通过 `strategy_params` 传入（Pydantic 校验）。除下列字段外，还继承
`DecisionFrequencyParams` 的 `decision_frequency` / `decision_every_n` / `decision_anchor`
/ `decision_warmup` / `decision_month_nth`。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `top_n` | `1` | 持有得分最高的 ETF 数量（1–10） |
| `slope_days` | `60` | 归一化收盘价线性回归窗口（斜率趋势的交易日数，SLOPE_N；recipe 用 40） |
| `short_term_slope_days` | `20` | 短窗斜率回看（检测短期过热）。仅 `short_term_damp_coef>0` 时生效 |
| `short_term_damp_coef` | `0.0` | 短期过热压制系数；`0`=关闭（得分退化为纯长窗斜率 z） |
| `amount_recent_days` | `5` | 量价综合（近期异常放量压制）近期窗口：取截至调仓日最近 `5` 个有效交易日的日均成交额作分子 |
| `amount_baseline_days` | `10` | 量价综合的分母基线窗口：紧邻近期窗口之前的 `10` 个交易日的日均成交额 |
| `amount_surge_damp_coef` | `0.0` | 量价综合压制系数；`>0` 时扣减 `coef × z(LOG(近5日均成交额/紧邻其前10日均成交额))`，压制近期异常放量；`0`=关闭（向后兼容）。典型 `0.1` |
| `require_raw_trend` | `False` | 要求标的自身原始（非横截面）N 日归一化收盘价回归斜率>0 才纳入候选（绝对上升趋势门槛，见 §2.1） |
| `raw_trend_days` | `40` | 原始趋势门槛的线性回归回看交易日数 |
| `max_annualized_vol` | `None` | 近 `vol_lookback` 日（默认 20）对数收益年化波动率上限（小数，`0.40`=40%）；超出剔除；`None` 关闭 |
| `vol_lookback` | `20` | 年化波动率回看交易日数 |
| `max_recent_gain_pct` | `None` | 近 `recent_gain_days` 日（默认 10）简单涨幅上限（%）；超出剔除，避免追高；`None` 关闭 |
| `recent_gain_days` | `10` | 近期涨幅回看交易日数 |
| `require_positive_score` | `False` | 开启后只保留得分>0 的候选；某调仓日全部候选都不满足 → 持现金（见 §2.1） |
| `min_score` | `0.0` | 配合 `require_positive_score=True` 时的分数下限（score≥min_score）；默认 `0` 即只要求得分>0 |
| `rotate_threshold` | `1.0` | 轮动惰性阈值（见 §3.1）；`1.0` 关闭惰性、纯按得分轮动；越接近 `0` 惰性越强 |
| `rsrs_gate` | `False` | 开启 **RSRS 门控**：对已选出的 top-N 再算 RSRS，`rsrs ≤ rsrs_threshold` 者本轮不买入（留现金、不顺位递补，见 §2.3） |
| `rsrs_regression_days` | `18` | RSRS 的 high~low 回归窗口（交易日数，经典口径 18） |
| `rsrs_zscore_days` | `120` | RSRS 标准分窗口：取最近这么多个滚动 beta 做时序标准分（经典口径 120） |
| `rsrs_threshold` | `-0.5` | RSRS 门控阈值：`rsrs > 阈值` 才允许持有 |
| `exclude_limit` / `exclude_suspended` | `True` | 调仓日排除涨跌停 / 停牌标的 |
| `limit_pct_threshold` | `9.5` | 判定涨跌停的涨跌幅阈值（%） |

### 综合得分公式（斜率单口径 + 可选压制项）

```
默认：  得分 = z( 归一化收盘价线性回归斜率 × R² )                    # 长窗 slope_days
过热：  得分 = z(长窗斜率) − short_term_damp_coef × z(短窗斜率)       # 短窗 short_term_slope_days
量价：  得分 = 过热后得分 − amount_surge_damp_coef × z( LOG( 近 amount_recent_days 日均成交额
                                                        / 紧邻其前 amount_baseline_days 日均成交额 ) )
```

- 对最近 `slope_days` 根 K 线的**原始收盘价**按首值归一化后做最小二乘回归
  （`slope_momentum`），取 `斜率 × R²`：斜率>0 表示区间整体上行，R² 刻画趋势是否贴近
  一条直线（拟合质量）。
- 再对当日池内所有候选做**横截面 z 标准化**（均值 0、标准差 1，z 值可 >1），含义为
  "高于当日池内平均多少个标准差"，天然把跨标的量纲统一。
- **短期过热压制**（`short_term_damp_coef>0`）：在长窗斜率 z 上再扣减 `系数 × 短窗`
  （`short_term_slope_days`）斜率的横截面 z。短窗斜率远强于长期者 = 近端急拉，得分被
  下调，压制追高——是 §2.1 近期涨幅上限在斜率空间的连续版。`coef=0` 关闭，完全等价
  纯长窗斜率 z（向后兼容）。
- **量价综合（近期异常放量压制）**（`amount_surge_damp_coef>0`）：用**成交额(amount)**
  构造放量生值 `LOG( 近 5 日均成交额 / 紧邻其前 10 日均成交额 )`（非重叠窗口：分子为截至
  调仓日最近 `amount_recent_days` 个交易日日均成交额，分母为其前紧邻 `amount_baseline_days`
  个交易日日均成交额），做当日池内横截面 z 后从得分中扣减。放量生值 z 高者 = 近端量能扩张
  高于池内平均，得分被下调，从而**避开刚异常放量的标的**；`coef=0` 关闭（向后兼容）。
  系数同样不宜取大，近端经验约 `0.1`（未锁进主配方）。

经验上长窗 `slope_days` 取中长（约 30–60）较好；过短易追噪音、过长被端点主导。回测
（recipe 参数、2020–2026 子池）表明轻微压制普遍提升 Sharpe：`短窗 20 · coef 0.1` 在保持
回撤与基线持平的同时收益约 +8%；字面的 `z20·0.2`（coef 0.2 且短窗 20）反而略低于无压制
基线，故系数不宜取大。

### 2.1 绝对趋势 / 风险 / 过热过滤（可选）

排序得分是**相对**指标（z 值），它只能区分"比别的强"，无法表达"自身是否够稳、是否已
过热"。以下三条是**绝对**过滤，逐条可选（默认关闭，不影响其它回测），在调仓日对每只
标的单独判定，不满足即剔除：

```
原始趋势为正  require_raw_trend=True → 近 raw_trend_days(40) 日归一化收盘价线性回归斜率 > 0
波动率上限    max_annualized_vol=0.40 → 近 vol_lookback(20) 日对数收益年化波动率 std×√252 ≤ 40%
近期涨幅上限  max_recent_gain_pct=10  → 近 recent_gain_days(10) 日简单涨幅 < 10%（避免追高）
```

- **原始趋势为正**（`require_raw_trend`）：对每只 ETF 自身的归一化收盘价做回归，斜率>0
  才保留。这是跨标的口径独立门槛，不受当日池内其它标的强弱影响；常与 `slope_days=40`
  搭配——先保证 40 日绝对上行，再在满足条件的标的里按相对强弱取 top-N。
- **波动率上限**（`max_annualized_vol`）：过滤高波动标的（回测实证放宽上限反而降收益、
  升回撤）。
- **近期涨幅上限**（`max_recent_gain_pct`）：短期已急拉>阈值的不追，往往在真正爆发前
  进场更优。

**得分门槛**（`require_positive_score=True`，配合 `min_score` 数字下限）：只保留得分>0
的标的（= 高于当日池内平均斜率强度）。当某调仓日**全部**候选都不满足时，组合持现金，
而非买入当前最弱但得分仍为负的标的。该开关默认关闭以保持历史行为（始终在可用候选取
top-N）。

### 2.2 动态行业池发现（`pool_discovery`）

`pool_discovery=True`（配合 `universe=etf_market`）时，每个调仓日不再用静态 config 池，
而是从**全市场 ETF 面板**按 as-of 重新发现候选池：按 9 大类 / 细分方向分桶 → 每桶按历史
日均成交额排序、桶内做日收益相关性去冗余（`discover_corr_threshold`，默认 0.7）→ 每方向
至多 `discover_per_direction` 只 → 再按方向分层填充（先各方向第 1 名、再第 2 名…）封顶
`discover_max_pool`。随后才交给动量斜率 z 排序选 `top_n`。剔除货币、除 5 年国债外的债券、
无法细分、以及恒生国企/纳斯达克之外的跨境品种。**所有窗口只读 `date <= asof` 的行，无前视**。

发现的中间结果按决策日落盘（见下方「过程可观测性」），`source=pool_discovery`。

**性能**：`ctx.panel` 在一次回测内不变，故发现所需的面板索引只在首个决策日构建一次
（`build_industry_pool_index`，约 1s），之后每个决策日只做二分切片 + 窗口统计，挂在
`ctx.cache["industry_pool_index"]` 上跨决策日复用。实测 81 个决策日：**220.9s → 13.2s
（约 16×）**，池成员与顺序不变。绕过缓存直接调 `discover_industry_pool`（不传 `index`）
会在每次调用时重建索引，行为一致但慢——批量回测务必走 `select()` 这条路。

### 2.3 RSRS 门控（`rsrs_gate`）

打分（斜率 z）与 §2.1 的绝对过滤都是"**买入前**按自身强度/风险决定谁能进候选"，RSRS
门控则是**选出之后的最后一道闸门**：对本轮已选出的 top-N 持仓逐票再算一次 RSRS，不达标
者本轮**不买入**——该仓位留现金，**不顺位递补给下一名**（top_n=1 时该调仓日即空仓）。

```
beta, R²  = 近 rsrs_regression_days(18) 根 K 线：以 low 为自变量、high 为因变量做 OLS
z         = 最近 rsrs_zscore_days(120) 个 beta 的【时序】标准分 (beta_last − mean) / std
rsrs      = z × R² × beta
门控      = rsrs > rsrs_threshold(-0.5) ? 允许持有 : 本轮不买入（现金）
```

- **为什么是 high~low**：RSRS（阻力支撑相对强度）用"最高价对最低价"的回归斜率刻画支撑
  与阻力的相对抬升速度，比收盘价斜率更早反映趋势的边际转弱。
- **为什么再乘 beta**：经典 RSRS 是 `z(beta) × R²`，只表达"相对自身历史有多强"，符号不
  表达方向。本策略额外乘 `beta` 生值，使 rsrs 的**符号跟随斜率方向**（beta<0 即区间内
  high 随 low 下行 → 值为负），于是可以直接用一条绝对阈值做"允许/不允许持有"的判定。
- **时序 vs 横截面**：这里的 z 是**同一只 ETF 自身 120 个 beta** 的标准分（不是当日池内
  横截面 z），因此它是"该标的相对自己近期状态"的指标，与排序得分的横截面口径互补。
- **数据不足按放行处理**：至少需要 `rsrs_regression_days + rsrs_zscore_days − 1` 根
  K 线（默认 **137**）且含 high/low 列；不足时 RSRS 无法判定，**按允许持有处理**（不会
  因为新上市 ETF 历史短而挡掉它）。报告中该类标的 `rsrs` 列为空。
- 被门控挡掉的标的不会写回轮动惰性缓存（`ctx.cache`），下个决策日的"当前持仓"即本轮
  真实买入的标的，避免门控标的被当作持仓"惯性保留"。
- 默认关闭（`rsrs_gate=False`），完全不影响历史配方。实现见 `backend/app/factors/rsrs.py`。

**回测示例**（`examples/cross_section/backtest_shared_etf_rotation_dynamic.json`，全市场
动态池 2020-01 ~ 2026-09，`next_day_open`）：

| 配置 | 总收益 | 最大回撤 | Sharpe | 换手 | 空仓决策日 |
| --- | --- | --- | --- | --- | --- |
| `rsrs_gate=False` | 12.76× | 34.8% | 1.47 | 147 | 1/81 |
| `rsrs_gate=True`（18/120，-0.5） | 13.85× | 19.1% | 1.69 | 111 | 21/81 |

81 个决策日中有 20 天因榜首 RSRS ≤ −0.5 而空仓；这些日子的 RSRS 集中在 −0.5 ~ −1.3
（beta 明显低于自身 120 个 beta 的均值），即"斜率动量仍最强、但相对自身历史已转弱"的
追顶时点——该配置下最大回撤近乎腰斩（34.8% → 19.1%）而收益未降。

## 3. 调仓

### 3.1 轮动惰性（`rotate_threshold`）

为避免在得分相近的标的间来回切换、降低调仓频率，策略引入惰性机制。策略在 `ctx.cache`
中记录上一调仓日持有的标的（跨决策日保留）。在每次调仓日，对仍可作为候选的当前持仓，
用它在**当前决策日**的得分对比新入选标的的**当前**得分：

```
当前持仓·当前得分 ≥ 新候选·当前得分 × rotate_threshold  → 保留持仓，不调仓
当前持仓·当前得分 < 新候选·当前得分 × rotate_threshold  → 轮动到明显更优的新候选
```

取值约定：

- `1.0`（默认）：关闭惰性，纯按得分轮动。
- `0.9`：新候选需明显更优才轮动。
- 越接近 `0` 惰性越强；`0` 时当前得分≥0 即保留，几乎永不调仓。

> 惰性比较针对仍通过可交易过滤、得分>0 的当前持仓；若当前持仓当前时刻已跌出候选，会
> 照常轮动，不会死守下跌标的。

## 4. 示例与回测配方

主配方见 `backend/examples/cross_section/backtest_shared_etf_rotation.json`，跑在
`etf_core_sub` 精选池、2020-01 ~ 2026-09，开启三档绝对过滤 + `require_positive_score` +
`rotate_threshold=1.0`（关闭惰性），长窗 `slope_days=40` + 短期过热压制（短窗 20 · coef 0.1）
+ **量价综合压制（近5/前10 · coef 0.1）**，`execution_timing=next_day_open`
（决策次日开盘成交）：

```bash
cd backend
.venv/Scripts/python.exe -m app.script backtest \
  --request examples/cross_section/backtest_shared_etf_rotation.json --json
```

配方在 2020-2026 子池上约 **20.9 倍**（100000 → ~209 万，累计 +1990%；年化约 58%、
最大回撤约 25%、Sharpe≈1.71、约 143 次换手）。相对无量价压制的锁定基线（short=0.1 ·
amount=0，约 19.4× / S1.67）在**回撤持平(24.8%)**下收益 +8%、Sharpe 升至 1.71。三条绝对
过滤缺一不可——放宽波动率或近期涨幅上限会把总收益压到 ~10 倍区间、回撤升高。轻微短期
过热/量价压制相比无压制基线（约 17.9 倍）在回撤持平下提升收益与 Sharpe。成交时点对结果
有明显影响：同参数下 `same_day_close` 约 17.7 倍、`next_day_close` 约 13.1 倍，故回测应
固定一个贴近真实下单方式的时点再比较。所有指标仅使用调仓日及之前的收盘价（next_day 成交
价不参与决策），不使用未来数据。

主配方现已开启量价综合压制 `amount_surge_damp_coef=0.1`（近5/前10），经 OOS 标定
（`experiment/calibrate_damp.py`，train 2020-24 / test 2025-26 切分）锁定**温和档**：全程
amount 0.05→0.2 单调升 Sharpe(1.67→1.83)且回撤恒 24.8%，但那段增益主要来自 2020-24，
OOS(2025-26) 几乎中性，故取 0.1（捕获大部分全程增益、回撤中性、不追 train 主导的 0.2），
详见 `experiment/etf-rotation-findings.md` §10。

### 过程可观测性（进度 + 决策日池落盘）

回测过程输出到 stderr：加载全市场行情时是单行原地刷新（`加载行情: 823/1681 ...`），
**每个决策日打印一行永久输出**，可滚动回看每个调仓日选了什么、当日候选有多少：

```
[  1/36] 2020-01-02 选中 159915 | 通过筛选 20 只
[  2/36] 2020-02-03 选中 512880,159915 | 通过筛选 21 只
```

**决策日池默认落盘**到 `out/pool_etf_rotation_universe_<start>_<end>.json`，内容是
`{asof: {source, count, members:[{symbol,name}]}}`，用于核查「那天到底圈进了哪些 ETF」：

- `source=pool_discovery`：开启 `pool_discovery=True` 时，该日由
  `discover_industry_pool` 从全市场发现出的池成员（§2.2；空池也会登记——空池正说明该日
  无合格标的，是最需要看到的诊断信息）；
- `source=eligibility`：未开动态发现时，该日通过全部过滤闸门、进入打分的候选集合，
  与报告中该日展示的候选表一致。

注意区分二者口径：进度行里的「通过筛选 N 只」是**打分后**的候选数，而
`pool_discovery` 的 `count` 是**发现阶段**的池大小（前者通常更小）。落盘用
`--no-pool-dump` 关闭，`--pool-dump FILE.json` 指定路径。

## 5. 报告中的指标对比

共享资金交互报告（`backtest --report` / `output_options.report`，`.html`）针对本策略提供
两类指标观察：

- **选股表**：每个调仓日展示**全部候选**（不止 top-N），含排名、收盘价与 `score` /
  `slope_raw` / `slope_score` 各指标列；开启 RSRS 门控时另有 `rsrs` / `rsrs_beta` /
  `rsrs_r2` / `rsrs_z` / `rsrs_pass` 列（`rsrs` 为空 = 历史不足、按放行处理）；`✓` 与
  高亮标出当次入选持仓（top-N，已被门控否决者不在其中）。
- **指标热力图（调仓日 × 标的）**：行 = 标的，列 = 调仓日；单元格按"该日候选内相对强弱"
  着色，蓝框 = 该日入选持仓，可一眼观察斜率/z 排名随时间的演化。

这是**通用基座**：报告模型会把策略 `select` 返回的每个候选 detail 中除元字段外的数值
字段自动透传进报告并被动态渲染。**新增指标只需在 `select_etf_rotation` 的候选 detail
字典里加一个字段**即可。

## 6. 局限

- 斜率趋势跟随在震荡/熊市里可能频繁在弱势标的间切换或持现金。
- 候选池过小（如只有 1–2 只）时，横截面 z 排名与过滤的意义有限。
