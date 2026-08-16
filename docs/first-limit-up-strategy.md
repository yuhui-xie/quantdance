# first_limit_up：首板超短线（次日开盘进）

## 策略思想

源自知乎"首板战法"：**只做首板**——先选出当日收盘涨停（首板）且此前未连板的票，
再进一步筛选"平台回调后的首板"，次日不高开很多时按开盘价进场，博取 1~5 日内
一波反弹，动能量能衰竭或跌破起涨点时离场。

筛选链（层层叠加，逐票独立判定）：

1. **首板**：当日收盘涨跌幅 ≥ `limit_pct`，且此前 `board_lookback` 日无涨停
   （排除连板/连涨）。
2. **平台回调**：首板前 `platform_days` 日处于低振幅平台整理（`platform_max_range`）。
3. **一次上穿均线**：首板当根 K 线收盘同时站上 MA`ma_fast`/`ma_mid`/`ma_slow`，
   且至少一次性上穿 `min_crossed` 条均线。
4. **均线平行或上行**：短/中/长均线在过去 `ma_slope_lookback` 日保持平行或向上。
5. **相对低位**：首板前股价在 `position_lookback` 日高低点中的分位 ≤
   `max_price_position`（前期没有大幅炒作）。
6. **前期未大涨**：首板前 `spec_lookback` 日累计涨幅 ≤ `max_spec_ret`。

满足以上条件的首板日 T，若次日（T+1）开盘相对首板收盘的高开幅度 ≤ `max_gap_up`
（不高开很多），则按 **T+1 开盘价**全仓买入；否则跳过（不追高）。

**离场**（每日，优先级从高到低）：
- **止损**：当日最低价 ≤ 首板日低点（起涨点 `low[T]`）→ 离场；
- **止盈/上涨乏力**：当日收盘 ≤ 持仓期日内峰值 × (1 − `trailing_pct`) →
  离场（以日内峰值 trailing 近似"一波上涨没有力气 / 分时图调头向下"）；
- **时间窗口**：持仓满 `max_hold_days` 个交易日 → 强制平仓（5 天内不上涨即止损，
  持仓窗口 1~5 日封顶）。

单票全仓、单持仓；平仓后若后续再出现符合条件的首板可再次进场。

## 参数

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `limit_pct` | 9.5 | 涨停近似阈值（%），主板 10% 用 9.5 |
| `board_lookback` | 60 | 首板判定：此前 N 日无涨停（排除连板/连涨） |
| `platform_days` | 20 | 回调平台观察窗口（交易日） |
| `platform_max_range` | 0.15 | 平台振幅上限 (high-low)/mean |
| `ma_fast` / `ma_mid` / `ma_slow` | 5 / 10 / 20 | 均线周期（须递增） |
| `min_crossed` | 2 | 首板当根一次性上穿的均线数下限 |
| `ma_slope_lookback` | 3 | 均线平行/上行判断回看交易日 |
| `position_lookback` | 250 | 相对低位观察窗口（近似年线位置） |
| `max_price_position` | 0.5 | 首板前股价在窗口高低点中的最高分位 |
| `spec_lookback` | 60 | 前期炒作观察窗口（交易日） |
| `max_spec_ret` | 0.30 | 首板前窗口累计涨幅上限 |
| `max_gap_up` | 0.05 | 次日开盘相对首板收盘的最大高开幅度（超过不追） |
| `max_hold_days` | 5 | 持仓窗口上限（交易日） |
| `min_profit` | 0.0 | 时间止损：窗口内涨幅低于此值即止损 |
| `trailing_pct` | 0.04 | 上涨衰竭回撤阈值 |

## 回测语义

这是一个**时序策略 `StrategySpec`**，在 `mode=universe` 下逐票独立用完整
`initial_cash` 运行，再按归一化净值等权汇总（复用 `run_backtest_universe_request`）。
**不满足条件的股票自然无交易、净值走平，即"跳过"**；行情为空或 K 线不足的股票由
runner 标记为 `skipped`。

### 批量测试示例（ZZ500）

```bash
cd backend
python -m app.script backtest --request \
  examples/batch_test/backtest_universe_first_limit_up.json
```

请求示例（节选）：

```json
{
  "mode": "universe",
  "strategy_id": "first_limit_up",
  "universe": "zz500",
  "max_universe": 500,
  "seed": 42,
  "start_date": "2023-01-01",
  "end_date": "2026-08-01",
  "initial_cash": 100000,
  "commission": 0.0003,
  "max_workers": 8,
  "include_equity": true,
  "include_trades": true,
  "include_price": true,
  "strategy_params": {
    "limit_pct": 9.5, "board_lookback": 60, "platform_days": 20,
    "platform_max_range": 0.15, "min_crossed": 2, "ma_slope_lookback": 3,
    "position_lookback": 250, "max_price_position": 0.5, "spec_lookback": 60,
    "max_spec_ret": 0.30, "max_gap_up": 0.05, "max_hold_days": 5,
    "min_profit": 0.0, "trailing_pct": 0.04
  },
  "output_options": { "report": "out/backtest_universe_first_limit_up.html" }
}
```

命令示例（CLI 覆盖）：

```bash
python -m app.script backtest --mode universe --universe zz500 \
  --strategy first_limit_up --start-date 2023-01-01 --end-date 2026-08-01 \
  --max-universe 50 --report out/first_limit_up_smoke.html
```

## 已知局限

- **涨停用日涨跌幅阈值近似**（`limit_pct`），非交易所正式涨停状态；涨跌停当日
  的成交受限未按完整规则建模。
- **无分时数据**："分时图调头向下 / 一波上涨没有力气"用**日内峰值的 trailing
  止损**近似（`trailing_pct`），非精确分时卖点。
- **无板块/概念数据**：本时序批量版本**未做热点炒作叠加门控**；如需按热点板块
  过滤选股，应改为横截面 `CrossSectionStrategySpec`（`select` 按决策日截面选股，
  可用 `concept_symbols` 白名单，参考 `limit_up_pullback`）。
- **"跳过"口径**：无触发事件的股票被计入 `succeeded` 且以走平净值参与等权汇总
  （不严格剔除）。若需从汇总中剔除平仓票，需在聚合层另做处理。
- **相对低位用日线高低分位近似**，`position_lookback=250` 需要较长行情历史，
  K 线不足会被跳过。
