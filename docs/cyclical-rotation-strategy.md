# 顺周期行业轮动策略说明

本文档描述横截面共享资金策略 `cyclical_rotation`：**以黄金/白银、原油、豆粕等商品类 ETF 为先行信号，在有色金属、能源、农业三个周期行业间轮动**；信号走强买入对应行业 ETF 龙头，信号见顶或中期调整离场。

实现代码：`backend/app/strategies/cross_section/cyclical_rotation.py`
横截面执行：`backend/app/backtest/cross_section_runner.py`，共享资金引擎：`backend/app/backtest/shared_engine.py`

> 核心思路来自经典的顺周期宏观逻辑——"金银启动 → 有色，布伦特跌穿 60 → 关注能源，石油启动后农产品必然启动"——系统化为信号资产代理 + 周期状态机 + 动量选龙头。

## 1. 周期架构

```
┌────────────────────────────────────────────────────┐
│              信号资产（仅判定，不持仓）              │
│  黄金 ETF (518880) + 白银基金 (161226)               │
│  南方原油 (501018)                                   │
│  华夏豆粕 ETF (159985)                               │
└────────────┬───────────────────────────────────────┘
             │ 动量 + 均线 + 回撤 判定
             ▼
┌────────────────────────────────────────────────────┐
│              周期状态机（四个独立周期）              │
│                                                     │
│  ├─ 有色金属 (nonferrous) ── 前置：无               │
│  │   信号：金银 任一上行                             │
│  │   离场：金银 中期调整 / 深度回撤见顶              │
│  │                                                 │
│  ├─ 能源 (energy) ── 前置：无                       │
│  │   信号：原油上行（附加：必须经历深度回撤后才允许）│
│  │   离场：原油中期调整 / 深度回撤见顶               │
│  │                                                 │
│  └─ 农业 (agriculture) ── 前置：能源激活            │
│      信号：豆粕上行                                  │
│      离场：豆粕中期调整 / 深度回撤见顶               │
└────────────┬───────────────────────────────────────┘
             │ 激活周期 → 持仓池
             ▼
┌────────────────────────────────────────────────────┐
│              持仓池（持有 ETF）                      │
│  有色金属：512400（南方中证有色金属ETF）             │
│  能源：    515220（国泰中证煤炭ETF）                 │
│  农业：    159825（富国中证农业主题ETF）              │
│                                                     │
│  每个激活周期内按动量排名，取 top_n_per_cycle 只    │
└────────────────────────────────────────────────────┘
```

### 默认参数示例

```json
{
  "decision_frequency": "monthly",
  "decision_every_n": 1,
  "top_n_per_cycle": 1,
  "allocation": "all",
  "signal_momentum_days": 20,
  "signal_trend_days": 120,
  "exit_ma_days": 60,
  "exit_drawdown_pct": 20.0,
  "energy_entry_drawdown_pct": 30.0,
  "min_cycle_days": 20
}
```

> 决策频率与 `decision_every_n` 的含义与所有横截面策略一致：决策日锚定自然周期（日 / ISO 周末 / 自然月末），回测结果不随起始日漂移。

## 2. 决策与换仓

本策略默认**每月末检查一次**（`decision_frequency="monthly"`，可改 `weekly` / `daily`），每次重算三个周期的信号状态：

1. **重算每个周期的信号**：对每个信号资产计算动量、长期均线、中期均线与峰值回撤。
2. **周期状态机**：按入场 / 离场规则更新各周期激活状态（状态跨决策日保存在 `ctx.cache`）。
3. **持仓构建**：从激活周期的持仓池中，按 ETF 动量排名取 `top_n_per_cycle` 只，构成目标持仓。
4. 换仓执行方式由 `rebalance_mode` 决定——默认 `full`（全清重建），推荐 `incremental`（只交易差异，保留共同持仓以减少摩擦）。

> 周期最小持有天数 `min_cycle_days`（默认 20 个交易日）防止信号噪音引起的刚入场就离场；保护期内即使信号暂时转弱也不卖出。

## 3. 信号判定规则

对每只信号资产，在当前决策日 `T` 取其 `asof ≤ T` 的日线收盘价序列，计算：

### 3.1 信号评分

```
momentum  = close[-1] / close[-1 - signal_momentum_days] - 1
trend_ma  = mean(close[-signal_trend_days:])      # 长期均线（趋势确认）
exit_ma   = mean(close[-exit_ma_days:])           # 中期均线（调整离场）
peak      = max(close[-signal_trend_days:])        # 长期窗口峰值
drawdown  = close[-1] / peak - 1                  # 负值 = 从峰顶回撤

bullish   = (momentum > 0)                        # 动量正向
            AND (close[-1] > trend_ma)            # 价格站上长期均线
            AND (close[-1] > exit_ma)             # 价格站上中期均线
            AND (drawdown > -exit_drawdown_pct / 100)  # 未深度回撤
```

一个周期的 `cycle_holding` = 周期内 `signal_mode`（`any` / `all`）的函数：
- `any`：任一信号资产 bullish 即认为周期处于可持有态。
- `all`：全部信号资产 bullish 才算。

### 3.2 入场规则

```
IF 上一决策日状态为 OFF
   AND cycle_holding == True
   AND 所有前置周期均已激活
   AND (周期 != "energy" OR 信号资产在看窗口内最低价/峰值 ≤ 1 - energy_entry_drawdown_pct/100)
THEN 周期状态 → ON，记录入场日期
```

能源周期的附加门槛 `energy_entry_drawdown_pct`（默认 30%）近似还原"布伦特跌穿 60"的逻辑：必须在 `signal_trend_days` 天窗口内经历过 ≥30% 的从峰到谷的下跌，才能入场——防止在油价高位顶部追入。

### 3.3 离场规则

```
IF 上一决策日状态为 ON
   AND cycle_holding == False
   AND (当前日期 - 入场日期) >= min_cycle_days    # 超出最小保护期
THEN 周期状态 → OFF
```

保护期内即使信号转弱，状态也保持不变——避免刚入场就被短期噪音踢出。

## 4. 持仓构建

激活周期集合确定后：

- `allocation="all"`（默认）：所有激活周期的持仓合并，周期间等权（引擎按目标数量均分资金）。
- `allocation="strongest"`：只取信号动量强度最高的一个周期，集中持仓。

每个激活周期内部的持仓池按 ETF 自身的动量（`close / close[-signal_momentum_days] - 1`）排名，取 `top_n_per_cycle` 只。

无任何周期激活时，目标持仓为空，组合持有现金。

## 5. 参数表

| 参数 | 类型 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `decision_frequency` | str | `"monthly"` | daily / weekly / monthly | 决策频率，同所有横截面策略的锚定逻辑 |
| `decision_every_n` | int | 1 | ≥1 | 决策步长：monthly+3=季末 |
| `decision_warmup` | int | 20 | 0-250 | 冷启动期（仅 daily 生效时跳过前 N 日） |
| `top_n_per_cycle` | int | 1 | 1-10 | 每个激活周期内持仓的 ETF 数量 |
| `allocation` | str | `"all"` | all / strongest | all=跨周期分散 | strongest=仅最强周期 |
| `signal_momentum_days` | int | 20 | 2-200 | 信号资产的动量回看交易日数 |
| `signal_trend_days` | int | 120 | 20-504 | 信号资产的长期均线交易日数 |
| `exit_ma_days` | int | 60 | 5-504 | 信号资产的中期均线交易日数（跌破即调整离场） |
| `exit_drawdown_pct` | float | 20.0 | 1.0-90.0 | 信号资产距长期峰值回撤（%）达此阈值视为见顶 |
| `energy_entry_drawdown_pct` | float | 30.0 | 1.0-90.0 | 能源周期入场门槛（%）：信号资产须在此深度深跌后才允许入场 |
| `min_cycle_days` | int | 20 | 1-120 | 周期最小持有交易日数（防抖） |
| `exclude_limit` | bool | true | — | 是否排除疑似涨跌停的持仓 ETF |
| `exclude_suspended` | bool | true | — | 是否排除疑似停牌的持仓 ETF |
| `limit_pct_threshold` | float | 9.5 | 1.0-30.0 | 涨跌幅阈值（%），高于此值视为涨跌停 |
| `cycles` | list[CycleGroup] | 见默认 | — | 自定义周期定义（详见下节），可覆盖或扩展 |

## 6. 周期自定义

每个 `CycleGroup` 对象的字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 周期唯一标识 |
| `name` | str | 展示名称 |
| `signal_symbols` | list[str] | 先行信号资产列表（纯判定，不持仓） |
| `signal_mode` | str | `"any"`=任一触发即激活 | `"all"`=全部触发才激活 |
| `holdings` | list[str] | 该周期可持仓的 ETF（按动量选龙头） |
| `precondition_cycle` | str\|null | 前置周期 id（该周期须先激活）；null=无前置 |

### 默认周期定义

```json
{
  "cycles": [
    {
      "id": "nonferrous",
      "name": "有色金属",
      "signal_symbols": ["518880", "161226"],
      "signal_mode": "any",
      "holdings": ["512400"]
    },
    {
      "id": "energy",
      "name": "能源",
      "signal_symbols": ["501018"],
      "signal_mode": "any",
      "holdings": ["515220"]
    },
    {
      "id": "agriculture",
      "name": "农业",
      "signal_symbols": ["159985"],
      "signal_mode": "any",
      "holdings": ["159825"],
      "precondition_cycle": "energy"
    }
  ]
}
```

> 用户可通过 `strategy_params.cycles` 扩展新的周期（如化工、黑色系、航运等），覆盖信号与持仓代码。信号资产仅需在面板中存在即可，不会进入持仓。

## 7. 示例请求参数

示例文件位于 `backend/examples/backtest_shared_cyclical_rotation.json`，以下逐字段说明。

### 请求级字段

| 字段 | 示例值 | 说明 |
| --- | --- | --- |
| `strategy_id` | `"cyclical_rotation"` | 策略标识 |
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
| `top_n_per_cycle` | `1` | 每周期持有动量最强的一只 |
| `allocation` | `"all"` | 多激活周期一起持有 |
| `signal_momentum_days` | `20` | 信号动量看 20 个交易日 |
| `signal_trend_days` | `120` | 趋势看半年均线 |
| `exit_ma_days` | `60` | 跌破季均线离场 |
| `exit_drawdown_pct` | `20.0` | 从峰值回撤 ≥20% 视为见顶 |
| `energy_entry_drawdown_pct` | `30.0` | 原油必须在看窗口内深跌 ≥30% 才允许入场 |
| `min_cycle_days` | `20` | 入场后至少持有 20 个交易日 |
| `exclude_limit` / `exclude_suspended` | `true` | 排除涨跌停与停牌 |

### 运行命令

```bash
cd backend

# 查看当前截面选股
python -m app.script backtest --strategy cyclical_rotation --mode screen --json

# 运行完整历史回测
python -m app.script backtest --request examples/backtest_shared_cyclical_rotation.json
```

## 8. 注意事项

1. **信号资产不持仓**：黄金 ETF、白银基金、南方原油、豆粕 ETF 仅用于周期判定，实际持仓来自各周期 `holdings` 池。这保证了信号与持仓的分离——例如你可以在有色周期中持有煤炭 ETF（如果 holdings 配置了的话），但信号仍来自黄金/白银。

2. **ETF 代码会随市场变化而失效**：基金清盘、更名、代码变更均可能导致数据拉取失败。请通过 `strategy_params.cycles` 覆盖信号与持仓代码，或直接编辑 `DEFAULT_CYCLES`。

3. **商品基金 ≠ 商品本身**：A 股商品类 ETF / LOF 的走势受 A 股市场整体情绪、基金折溢价、汇率、基金管理费等多重因素影响，与底层商品（伦敦金、布伦特原油、CBOT 豆粕）存在基差和跟踪误差。

4. **"布伦特跌穿 60"已近似为信号资产回撤门控**：南方原油（501018）作为原油价格代理，其回撤可能不等于布伦特的原值回撤，且 A 股原油 LOF 有折溢价波动。如有精确的布伦特价格数据，可改为直接接入。

5. **顺周期逻辑的滞后性**：月度决策的周期轮动天然有几个交易日至一个月不等的信号滞后——从商品价格变化到 A 股行业 ETF 的价格传导也存在时差。回测收益包含了这种滞后性，实盘信号也同理。

6. **无激活周期时持现金**：市场中性时期（三大周期均无信号），组合空仓。这在牛市中会跑输指数，但在熊市中会显著减损。

7. **历史回测 ≠ 未来表现**：本策略仅供研究参考，不构成投资建议。
