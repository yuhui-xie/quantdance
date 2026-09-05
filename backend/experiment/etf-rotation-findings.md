# ETF 轮动实验结论汇总（2025-09，etf_core_sub / 2020-2026 / next_day_open）

> 本文把散落在本地记忆与本仓库各次迭代里的**实验结论**收敛成文，避免后续重跑重推。
> 基准背景：`etf_rotation`（斜率版）主配方 `examples/cross_section/backtest_shared_etf_rotation.json`，
> 口径 2020-01~2026-09 的 `etf_core_sub` 精选池，`execution_timing=next_day_open`，
> `slippage=0.001`。指标默认以"最终净值倍数(×)"与 Sharpe 表述。

## 1. 成交时点 EXECUTION_TIMING（请求级，通用共享引擎）

共享横截面引擎 `run_shared_backtest` 支持三种成交时点（决策内容在决策日以 asof 收盘算好，
无未来信息）：`same_day_close`=决策日收盘 / `next_day_open`=决策日次日开盘（默认）/
`next_day_close`=决策日次日收盘。next_day 会把目标成交顺延一个交易日、换对应成交面板；
**换仓买卖用成交面板，风险/止损离场始终用收盘价**。字段挂 `BacktestRequest`，非策略参数。

同参数（斜率 40 + 三档绝对过滤 + require_positive_score + 短窗20/coef0.1）实测：

| execution_timing | 最终净值(×) | 年化 | 回撤 | Sharpe |
|---|---|---|---|---|
| `same_day_close` | ~17.7 | 53.8% | 25.3% | 1.63 |
| **`next_day_open`（默认）** | ~17.9* | 54.1% | 24.8% | 1.62 |
| `next_day_close` | ~13.1 | 47.1% | 30.3% | 1.46 |

\* 上表为无短期压制的基线对照；叠加短窗压制后 next_day_open 到 ~19.4×（见 §3）。
成交时点影响显著，**比较配方必须锁定时点**。

## 2. 是否去掉旧版分层峰值止损（stop_loss_tier_*）

历史上层叠式"峰值回落 X% 卖一档"止损会**截断动量主升**：移除后 1503%→1770%，且回撤更低、
Sharpe 更高。→ 动量轮动不建议开这类峰位减仓。

## 3. 短期过热压制（可选）：score = z(长窗) − coef·z(短窗)

参数 `short_term_slope_days` + `short_term_damp_coef`。`coef>0` 时对近端斜率远强于长期者
（短期过热）扣分，是"近 10 日涨幅上限"在斜率空间的连续版。`coef=0` 完全等价纯长窗 z
（已复跑精确复现，向后兼容）。

- 用户字面 `z(20)·0.2`（短窗 20、coef 0.2）= ~17.7×，**反而低于无压制基线** → 不划算。
- 短窗 20 需配**小系数**。**锁定：短窗 20 · coef 0.1** → **~19.4×**（年化 56%、回撤 24.8%
  与基线持平、Sharpe 1.67、143 换手），比无压制基线 ~17.9× 收益 +8% 且 Sharpe 更高。
- 短窗 10 在 coef 0.10~0.20 区间平稳 ~20×（但回撤升到 ~28%）；coef 0.25 尖峰 ~21.7× 疑似
  局部过拟合，勿追。coef 过大（0.5）明显变差（~15×）。

## 4. 近期涨幅上限：10 日 / 10% 是甜点

扫描近 10 日涨幅上限（`recent_gain_days=10`）呈单峰倒 U，峰值恰在 **10%**（~19.4×）：

- 更严（8%、5%）砍掉主升 → 收益下滑，5% 时回撤还升至 ~30%。
- 更松（12/15/20%、关闭）放进追高 → 一路跌到 ~11~15×、Sharpe 与胜率同步下滑。

→ 该参数已调在甜点，保持 10% / 10 日。

## 5. 涨幅窗口拉长到 20/30 日：严格更差

把近期涨幅窗口拉长（`recent_gain_days=20/30`）并各自配最优上限，**均远不如 10 日窗口**
（20日最佳 ~12×、30日最佳 ~13×）。原因：窗口拉长后统计变成"整段上涨已积累多少"，与
40 日斜率想抓的主升**正相关**，设限自我拆台；而 10 日才贴近"调仓前新鲜尖峰"的本意。

## 6. 更长区间累计涨幅门槛（max_gain_pct / gain_days）：反噬

在近 10 日门槛之上叠加长窗累计涨幅上限，越严越伤（动量策略本就要吃已涨很多的强势主升）：

| 叠加门槛 | 最终净值(×) | 备注 |
|---|---|---|
| 无（基线） | ~19.4 | |
| 40日 ≤30% | 14.65 | 最松也仍低于基线 |
| 40日 ≤25% | 9.95 | |
| 40日 ≤20% | 3.66 | |
| 60日 ≤25% | 12.81 | |

→ `max_gain_pct` 保持关闭（`None`）。代码保留可选开关，勿默认启用。

## 7. 绝对过滤的必要性（勿放宽）

回撤/收益双重约束：放宽波动率上限（>40%）或近期涨幅上限会显著降收益、升回撤。三条缺一
不可：40 日原始斜率>0、20 日年化波动率 std·√252 ≤40%、近 10 日涨幅 ≤10%。

## 8. 结论性配方（2025-09 锁定值）

```
decision_frequency=monthly, decision_anchor=start, top_n=1
slope_days=40
short_term_slope_days=20, short_term_damp_coef=0.1      # 轻微压制短期过热
require_raw_trend=true, raw_trend_days=40
max_annualized_vol=0.40, vol_lookback=20
max_recent_gain_pct=10.0, recent_gain_days=10
require_positive_score=true, min_score=0.0
rotate_threshold=1.0（关惰性）
exclude_limit/suspended=true, limit_pct_threshold=9.5
execution_timing=next_day_open
```
→ etf_core_sub 2020-2026 约 **19.4×**（年化 56%、回撤 24.8%、Sharpe 1.67）。
