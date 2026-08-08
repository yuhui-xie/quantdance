# 景气共振策略说明

本文档描述横截面共享资金策略 `prosperity_resonance`：**盈利增长性价比（PEG）+ 趋势确认 + 回调入场**，三重共振后综合评分选股，按决策日等权换仓。

实现代码：`backend/app/strategies/cross_section/prosperity_resonance.py`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

> 核心思路：寻找"好公司（低 PEG）+ 好趋势（MA/LLT 确认）+ 好价格（回调到均线支撑）"的共振点。三重过滤大幅降低噪音交易，提高信号纯度。

## 1. 核心思想

A 股有两个被反复验证的 alpha 来源：

- **小市值 + 低 PEG**：以合理价格买入成长型中小盘股，长期跑赢市场。
- **回调入场**：A 股散户追涨杀跌创造波动，趋势中的回调是更好的入场点。

本策略将这三者系统化为三层过滤 + 一层评分：

```
第 1 层：盈利质量 ── PE>0, PEG 合理, PB/PS 不极端
    ↓
第 2 层：趋势确认 ── 价在 MA50 上, 窗口正收益, LLT 向上
    ↓
第 3 层：入场时机 ── 价在 MA20 的窄偏离带内（趋势回调而非破位）
    ↓
综合评分 ── PEG 排名 + 动量排名 + 均线接近度 → 取 top N
```

默认参数示例：

```json
{
  "decision_interval": 20,
  "top_n": 8,
  "min_peg": 0.1,
  "max_peg": 1.5,
  "trend_ma": 50,
  "entry_ma": 20,
  "max_ma_deviation": 0.08,
  "min_ma_deviation": -0.05
}
```

## 2. 选股规则（每个调仓日）

在股票池内，对调仓日 `T`：

1. **可交易**：剔除 ST/退、疑似停牌、当日疑似涨跌停。
2. **板块（可选）**：`main_board_only=true` 时仅保留沪深主板。
3. **价格 / 市值**：收盘价 ∈ `[min_price, max_price]`；总市值 ∈ `[min_market_cap, max_market_cap]`。
4. **盈利质量**：
   - `require_profit`：PE(TTM) > 0
   - PEG ∈ `[min_peg, max_peg]`（核心因子）
   - PB ≤ `max_pb`（若有）
   - PS(TTM) ≤ `max_ps`（若有）
5. **趋势确认**：
   - 近 `lookback_days`（默认 60）个交易日累计收益 ∈ `[momentum_min, momentum_max]`
   - 收盘价 ≥ `trend_ma`（默认 50）日均线
   - （可选）LLT 趋势向上：当前 LLT 值 ≥ 回看前的 LLT 值
6. **入场时机**：
   - 收盘价 / `entry_ma`（默认 20）日均线 − 1 ∈ `[min_ma_deviation, max_ma_deviation]`
   - 即价格在短期均线的窄带内——允许略微在均线上方（刚突破）或略微下方（回调），但不允许大幅偏离
7. **综合评分**：
   - PEG 因子分 = `w_peg × (1 − PEG 在候选中的百分位排名)`——PEG 越低分越高
   - 动量因子分 = `w_momentum × 窗口收益率百分位排名`——动量越强分越高
   - 入场因子分 = `w_entry × (1 − |均线偏离| / 最大带宽)`——越接近均线分越高
   - 总分 = PEG 分 + 动量分 + 入场分
8. **排序持仓**：按总分降序，取前 `top_n` 只等权持有。

## 3. 参数总表

### 3.1 公共字段

与所有横截面策略一致，见 [策略总览](./strategy-guide.md)。示例：

| 字段 | 示例值 | 说明 |
|------|--------|------|
| `strategy_id` | `prosperity_resonance` | 策略 ID |
| `mode` | `universe` | `universe`=共享资金回测；`screen`=仅截面选股 |
| `universe` | `zz1000` | 默认使用中证 1000 成分股 |
| `initial_cash` | `1000000` | 初始资金（元） |
| `position_management.max_positions` | `8` | 最大持仓数 |
| `position_management.stop_loss_pct` | `0.10` | 个股止损线（−10%） |
| `position_management.take_profit_mode` | `trailing` | 追踪止盈模式 |
| `position_management.take_profit_pct` | `0.30` | 止盈触发线（+30%） |
| `position_management.trailing_drawdown_pct` | `0.10` | 从峰值回撤 10% 止盈 |

### 3.2 `strategy_params`

| 参数 | 默认 | 范围 | 说明 |
|------|------|------|------|
| `decision_interval` | 20 | ≥1 | 决策间隔（交易日），≈月频 |
| `top_n` | 10 | 1–50 | 持仓只数 |
| `min_price` | 5.0 | ≥0 | 最低股价（元） |
| `max_price` | 100.0 | ≥min | 最高股价（元） |
| `min_market_cap` | 3e9 | ≥0 | 总市值下限（30 亿） |
| `max_market_cap` | 5e10 | ≥min | 总市值上限（500 亿） |
| `require_profit` | true | — | 要求 PE(TTM)>0 |
| `min_peg` | 0.1 | 0–5 | PEG 下限（过低可能是数据异常） |
| `max_peg` | 2.0 | ≥min | PEG 上限（GARP 阈值） |
| `max_pb` | 8.0 | 0.1–50 | 市净率上限 |
| `max_ps` | 10.0 | 0.1–50 | 市销率上限 |
| `lookback_days` | 60 | 10–250 | 趋势动量观察窗口 |
| `trend_ma` | 25 | 5–250 | 趋势过滤均线周期 |
| `momentum_min` | 0.0 | −1–5 | 窗口最低收益率 |
| `momentum_max` | 0.60 | ≥min | 窗口最高收益率 |
| `llt_period` | 20 | 2–250 | LLT 平滑周期 |
| `llt_slope_lookback` | 5 | 1–20 | LLT 斜率回看天数 |
| `require_llt_up` | true | — | 要求 LLT 向上 |
| `entry_ma` | 20 | 2–120 | 入场均线周期 |
| `max_ma_deviation` | 0.12 | 0–1 | 高于入场均线的最大偏离 |
| `min_ma_deviation` | −0.08 | −1–0 | 低于入场均线的最大偏离 |
| `w_peg` | 1.0 | 0–10 | PEG 评分权重 |
| `w_momentum` | 0.8 | 0–10 | 动量评分权重 |
| `w_entry` | 0.8 | 0–10 | 入场质量评分权重 |
| `main_board_only` | false | — | 仅沪深主板（默认 false 覆盖全市场） |
| `exclude_st` | true | — | 剔除 ST/退 |
| `exclude_limit` | true | — | 剔除当日疑似涨跌停 |
| `exclude_suspended` | true | — | 剔除疑似停牌 |
| `limit_pct_threshold` | 9.5 | 1–30 | 涨跌停近似阈值（%） |

## 4. 数据说明与局限

- **PEG**：来自东财 `stock_value_em`，计算口径为 PE(TTM) / 盈利增速。部分成分股 PEG 缺失或不稳定，候选池可能偏小。
- **均线**：用 K 线日数据计算简单移动平均（SMA），非交易所官方均线。
- **LLT**：低延迟趋势线（二阶递推），对近期价格变化更敏感，比传统 MA 滞后更小。
- **PB/PS**：为可选过滤字段，值为 None 时自动跳过该过滤。
- **小市值偏差**：默认市值范围 50 亿–500 亿（中小盘），在大小盘风格切换时可能阶段性跑输沪深 300 等大市值指数。
- **候选不足**：若通过全部过滤的标的少于 `top_n`，只持有能选出的标的；放宽 `max_peg`、`max_pb`、均线偏离带可扩大候选池。

## 5. 运行示例

```bash
cd backend

# 查看策略是否注册
python -m app.script backtest --list-strategies | grep prosperity

# 截面选股（查看某日选了什么）
python -m app.script backtest --strategy prosperity_resonance --mode screen \
  --max-universe 200 --seed 42 --json

# 完整回测
python -m app.script backtest --request examples/backtest_shared_prosperity_resonance.json

# 快速测试（小股票池 + CLI 参数覆盖）
python -m app.script backtest --strategy prosperity_resonance --mode universe \
  --universe zz500 --max-universe 100 --seed 42 \
  --start-date 2023-01-01 --end-date 2024-12-31 \
  --initial-cash 100000 --plot out/test_prosperity.svg
```

## 6. 调参指南

### 候选太少 → 扩大候选池
- 提高 `max_peg`（如 1.5 → 2.0）
- 放宽均线偏离带（`max_ma_deviation` 0.08 → 0.12，`min_ma_deviation` −0.05 → −0.10）
- 降低 `momentum_min`（允许略微负收益的横盘股）
- 关闭 `require_llt_up`
- 降低 `min_market_cap` 或提高 `max_market_cap`

### 回撤太大 → 提高风控
- 降低 `stop_loss_pct`（0.10 → 0.07）
- 降低 `trailing_drawdown_pct`（0.10 → 0.07）
- 收窄 `max_momentum` 上限（排除高位股）
- 收窄均线偏离带（更严格的入场价格）

### 跑不赢基准 → 调整因子暴露
- 提高 `w_momentum`（给趋势更多权重）
- 降低 `max_peg`（更严格的价值要求）
- 缩小市值范围（更纯粹的小市值暴露）
- 设为 `main_board_only: true`（排除创业板等高波动板块）

## 7. 与其他策略的关系

| 策略 | 与本策略的关系 |
|------|---------------|
| `limit_up_pullback` | 都做回调入场，但本策略用 PEG 替代涨停事件，覆盖面广得多 |
| `market_auntie` | 都有 PEG 过滤，但本策略增加了趋势确认层和精确入场时机 |
| `etf_rotation` | 都是趋势跟随，但本策略用 PEG 估值锚避免追高 |
| `order_inflection` | 都关注基本面改善，但本策略用 PEG 而非财报科目 |
| `small_cap_zz399101` | 都偏小市值，但本策略多了盈利质量和趋势过滤 |

## 8. 默认参数回测结果

以下为默认参数 + zz500 成分股 500 只在 2020-01-01 ~ 2026-07-31 的回测表现：

| 指标 | 数值 |
|------|------|
| 累计收益 | **+56.74%** |
| 年化收益 | +7.12% |
| 最大回撤 | −23.16% |
| 夏普比率 | **0.58** |
| 卡玛比率 (收益/回撤) | 2.45 |
| 超额收益 (vs 沪深300) | +47.66% |

分年收益：
- 2020: **+11.6%**（建仓期）
- 2021: **+31.6%**（牛市共振）
- 2022: −15.5%（熊市回撤，但跑赢市场）
- 2023: +2.8%（震荡筑底）
- 2024: +4.7%（缓慢恢复）
- 2025: **+27.4%**（牛市共振）
- 2026 (至7月): −8.7%

重现命令：
```bash
cd backend
python -m app.script backtest --request examples/backtest_shared_prosperity_resonance.json
```
