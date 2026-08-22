# limit_up_pullback_lowbuy：涨停回调低吸

## 策略思想

短线回调低吸：**只做涨停后的回调企稳再启动**——先确认近 15 日有过涨停（有炒作
热度与资金关注），随后股价回调回踩重要均线（默认 MA30）获得支撑，出现**极度缩量
十字星**这类关键趋势逆转 K 线，次日若**放量收阳**即为买点，按当日收盘价全仓买入，
博 3~5 日反弹（目标 5%~25%）。

筛选链（层层叠加，逐票独立判定）：

1. **近 N 日有涨停**：`limit_lookback`（默认 15）交易日内至少出现一次涨跌幅 ≥
   `limit_pct` 的涨停（排除炒作热度不足的票）。
2. **回调回踩均线获支撑**：设置日 T 的日内最低价 `low[T]` 触及 MA`ma_period`
   （默认 MA30）上方 `support_tolerance` 容差内，且收盘 `close[T]` 仍站上均线
   （支撑有效）；并确认 `close[T]` 低于近期 `limit_lookback` 日盘中峰值（是回调
   而非仍在高位）。
3. **极度缩量十字星**：T 日 K 线实体极小（`|close-open| ≤ doji_body_ratio ×
   (high-low)`），且量能显著萎缩（`volume[T] ≤ low_vol_ratio × MAvol`）。多空在该
   位置达成平衡，是潜在的止跌转折信号。
4. **放量收阳买点**：T+1 日若量能放大（`volume ≥ confirm_vol_ratio × MAvol`）、
   收阳（`close > open`）且高于 T 日收盘，即为买点，按 **T+1 收盘价**全仓买入。

**离场**（持仓后每日，从进场次日起，优先级从高到低）：
- **止损**：当日最低价 `low[i] ≤ entry_price × (1 − stop_loss_pct)` → 收盘离场；
- **下次放量落袋**：当日量能再次放大（`volume ≥ confirm_vol_ratio × MAvol`）→
  收盘离场（量能释放常伴随冲高，顺势落袋）；
- **时间兜底**：持仓满 `max_hold_days` 个交易日 → 收盘强制平仓。

单票全仓、单持仓；平仓后若后续再出现符合条件的买点可再次进场。

## 参数

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `limit_pct` | 9.5 | 涨停近似阈值（%），主板 10% 用 9.5 |
| `limit_lookback` | 15 | 近 N 交易日有涨停的观察窗口 |
| `ma_period` | 30 | 回踩的重要均线周期（默认 MA30） |
| `support_tolerance` | 0.02 | `low` 触及 MA 上方容差比例（回踩判定） |
| `doji_body_ratio` | 0.30 | 十字星：`|close-open| ≤ ratio×(high-low)` |
| `low_vol_ratio` | 0.65 | 极度缩量：`volume ≤ ratio×MAvol` |
| `confirm_vol_ratio` | 1.20 | 放量：`volume ≥ ratio×MAvol`（买点与离场均用） |
| `vol_ma_period` | 5 | 量能基准 MA 周期 |
| `stop_loss_pct` | 0.05 | 止损：跌破成本该比例即离场（默认 -5%） |
| `max_hold_days` | 5 | 持仓时间上限（交易日），到点强制平仓兜底 |

## 回测语义

这是一个**时序策略 `StrategySpec`**，在 `mode=universe` 下逐票独立用完整
`initial_cash` 运行，再按归一化净值等权汇总（复用 `run_backtest_universe_request`）。
**不满足条件的股票自然无交易、净值走平，即"跳过"**；行情为空或 K 线不足的股票由
runner 标记为 `skipped`。买点按确认日收盘价成交，离场按收盘价成交，未建模涨跌停
成交受限。

### 批量验证示例（ZZ1000）

```bash
cd backend
python -m app.script backtest --request \
  examples/batch_test/backtest_limit_up_pullback_lowbuy.json
```

### 示例请求参数

以 `backend/examples/batch_test/backtest_limit_up_pullback_lowbuy.json` 为例：

请求级字段（`strategy_params` 之外）：

| 字段 | 含义 |
| --- | --- |
| `mode` | `universe`：逐票独立回测并按归一化净值等权汇总 |
| `strategy_id` | `limit_up_pullback_lowbuy` |
| `universe` | `zz1000`（中证1000）；可改 `all_a`/`hs300`/`zz500` 或 `symbols` 显式列 |
| `max_universe` | 股票池上限；超出时按 `seed` 可复现抽样 |
| `seed` | 抽样随机种子，便于复现 |
| `start_date` / `end_date` | 回测区间 `YYYY-MM-DD`（批量模式必填） |
| `initial_cash` | 每只股票独立使用的初始资金（元） |
| `commission` | 买卖手续费率 |
| `max_workers` | 并发线程数 |
| `include_equity` / `include_trades` / `include_price` | 逐票明细体积开关 |
| `output_options.report` | 交互 HTML 报告路径（`.html`） |

`strategy_params`：上表全部策略参数，用于调参（如放宽 `limit_lookback`、
调整 `confirm_vol_ratio` 或 `stop_loss_pct`）。

命令行覆盖示例（小样本冒烟）：

```bash
cd backend
python -m app.script backtest --mode universe --universe zz1000 \
  --strategy limit_up_pullback_lowbuy --start-date 2024-01-01 \
  --end-date 2026-08-01 --max-universe 30 --report out/smoke_lowbuy.html
```

## 已知局限

- **涨停用日涨跌幅阈值近似**（`limit_pct`），非交易所正式涨停状态；涨跌停当日的
  成交受限未按完整规则建模。
- **买/卖按收盘价近似**，未建模盘口冲击、整手与最低佣金（时序批量口径）。
- **放量/缩量用成交量相对短期均线的倍数近似**，非交易所量比等官方口径。
- **"跳过"口径**：无触发事件的股票被计入 `succeeded` 且以走平净值参与等权汇总
  （不严格剔除）。
- `mode=universe` 等权汇总不模拟共享资金、不设持仓上限，仅统计性汇总各票独立
  净值曲线。
