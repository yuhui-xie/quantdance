# 数据复权说明（前复权）

记录本项目行情数据的前复权口径、曾出现的数据缺陷、修复方案与维护约定。

> ETF 份额折算断层的专项排查与检查脚本见 [etf-data-issues.md](etf-data-issues.md)。

## 结论

本项目日线行情（`app/data_sources/market_data.py` 的 `fetch_a_share_daily`）统一返回**前复权（qfq）**价格，
跨分红除权日**连续无断层**，可直接用于历史回测与选股，无需额外处理复权。

## 曾出现的缺陷（已修复）

### 现象

宝信软件（600845）在 2024-06-11 → 2024-06-12 出现约 **-18.7%** 的隔夜跳水；且历史上每次分红除权日
都有类似断层，最深达 **-48%**（2016-05-09 高送转）。这些远超 A 股 ±10% 涨跌停限制，显然不是真实行情。

检查口径：逐日 `close.pct_change()`，凡 |隔夜涨跌| > 12% 即判定为除权断层未消除。

### 根因

主数据链路 `fetch_a_share_daily → AStockDataSDK.get_klines → MootdxMarketSDK.get_klines → mootdx bars`。

问题出在 mootdx 自带的 `bars(adjust='qfq')`：

1. 它依赖新浪预计算的复权因子表（`mootdx.utils.factor.fq_factor`），该表按**乘**因子处理
   （`reversion.factor_reversion`：`price = raw * factor`），而该因子是**后复权风格**（越早越大），
   应用后序列在除权日并不连续；
2. 结果在每次除权日都残留一个虚假断层，污染信号、收益曲线与绩效指标。

### 修复方案

不再依赖 mootdx 自带复权，改为**拉原始价 + 本地按除权记录折算前复权**：

- mootdx 拉**不复权**（`adjust=''`）原始 K 线；
- 从 TDX 本地服务器取除权除息记录（`get_xdxr`，`category==1` 的行含
  `fenhong`(每10股派现)、`peigu`(配股)、`peigujia`(配股价)、`songzhuangu`(送转股)）；
- 用教科书前复权公式（与 mootdx `reversion._reversion` 的前复权分支一致）折算：

```text
preclose = (前收 * 10 - fenhong + peigu * peigujia) / (10 + peigu + songzhuangu)
adj      = (preclose.next / close).fillna(1)[::-1].cumprod()   # 最新一根因子 = 1
复权价   = 原始价 * adj
```

实现位于 `MootdxMarketSDK._forward_adjust_bars` / `_get_xdxr_info`（`app/data_sources/mootdx_market_sdk.py`）。

> 注意：`get_xdxr` 只接受**裸六位代码**（如 `600845`），带市场前缀（`sh600845`）会取不到数据。

### 修复验证

- 宝信软件 600845：2024-06-11 收 **32.90** → 06-12 收 **32.88**，断层消除，序列连续；
- 全历史 4999 根中 **0 处** >12% 隔夜跳变，最大单日波动 +10.05%（符合 ±10% 限制）。

### ETF 份额折算断层（已修复）

- **现象**：ETF 512930 在 2026-05-22 出现约 **-74%** 隔夜跳水（2.748 → 0.705）；ETF 512200 在 2024-08-12 出现约 **+170%** 隔夜跳涨（0.441 → 1.191）。均远超涨跌停限制，明显非真实行情，根因是这两个 ETF 当日做了**份额折算**（拆细/合并）。
- **根因**：TDX 除权记录里，普通股票分红送转是 `category==1`，而 ETF 份额折算/拆分是 `category==11`（字段 `suogu`=份额比例，1 份变 suogu 份、价格 ×1/suogu）。`_get_xdxr_info` 原先只取 `category==1`，漏掉了 `category==11`，导致前复权未折算、在折算日留下断层。
- **修复**：`_get_xdxr_info` 改为同时纳入 `category==11`，并把 `suogu` 折算为等价的送转股 `songzhuangu=10*(suogu-1)`（1 拆 4 等价于每 10 份送 30 份，数学上与送转共用同一条前复权公式）。注意 `suogu` 双向有效：>1 为拆细（价格下调，如 512930 的 4.0）、<1 为合并（价格上调，如 512200 的 0.358，songzhuangu 为负）；仅跳过 `suogu<=0` 的无有效比例记录。由于该改动改变前复权口径，`_KLINE_CACHE_VERSION` 已递增（v3→v4→v5，v5 补齐合并方向）使旧缓存整体重建。

### 除权日非交易日导致记录被丢弃（已修复，v5→v6）

- **现象**：ETF 159922 在 2024-12-02 出现 **-60.6%** 隔夜跳水（5.951 → 2.343），前复权序列本应连续却留着断层；且 2024-12-02 **之前**的全部历史价格被高估 **2.5 倍**。
- **根因**：前复权是把除权记录按日期 join 到真实 K 线上（`df.join(info, how="left")`），而 TDX 的除权日**未必是交易日**——可能是周末/节假日（159922 的份额折算记录日期是周日 **2024-12-01**），也可能是标的当日**停牌**。这类记录会被 left join **静默丢弃**，该次除权完全不参与折算。抽样 300 只股票，窗口内除权记录有 **1.5%** 属于此类；幅度大的（10 送 10、10 转 15）会直接留下 -50% 级虚假断层。
- **修复**：新增 `_align_xdxr_to_bars`，把除权记录对齐到「该日或之后的第一个交易日」，多条记录顺延到同一天时合并（159922 即此例：周日 12-01 的折算记录与 12-02 的分红记录都落到 12-02）。**早于首根 K 线的记录仍丢弃**——前复权因子以最新一根为 1 向前累乘，窗口外的除权不影响窗口内的相对因子，若把它们也顺延到首根 K 线反而会污染整段窗口。
- **验证**：159922 的 2024-11-29 → 12-02 隔夜收益由 -59.8% 恢复为 **+0.61%**；400 只抽样中 34 条被丢弃的记录（占 1.5%）全部恢复，对齐后日期均落在真实交易日上。

### 除权记录取数失败被当成"无除权记录"（已修复）

- **根因**：`_get_xdxr_info` 原先 `except Exception: return None`，把**取数失败**与**确认无记录**混为一谈；调用方拿不到记录就按"无需复权"放行原始价，而 `_write_klines_cache` 照样盖上 `version`/`adjust='qfq'`。于是一次 TDX 抖动就会让这只票当天所有读取都拿到**原始未复权价**、被当作前复权长期复用，且不报任何错。
- **修复**：区分两者——空 `DataFrame` 表示确认无记录（放行）；mootdx 返回 `None`（其 `file_cache` 在"无本地缓存 + 取数失败"时的行为）或抛异常，则抛 `MootdxMarketError`。异常在写缓存前抛出，不会污染缓存；与"行情失败直接抛、不回退其他源"的项目约定一致。

### 缺失价导致整段历史被 inf 污染（已修复）

- **根因**：`data = df.join(info, how="left").fillna(0.0)` 会把缺失/为 0 的收盘价变成 `0.0`。一旦该日**紧邻除权日**，单日因子 `理论前收 / 收盘` 得到 ±`inf`；而因子是**反向 cumprod**，`inf` 会乘进它前面所有更早的 bar，把整段历史价格变成 ±`inf`。`.fillna(1.0)` 只捞 `NaN`、捞不住 `inf`，所以静默通过。
- **修复**：累乘前先把非有限的单日因子降级为 1（该步不复权），避免 `inf` 传播。

### 东财估值面板「不复权 close + 复权 pct_change」口径混用（已修复）

- **现象**：东财估值面板（`em_fundamentals`，`stock_value_em`）的 `close` 是**不复权**真实成交价，
  而**同一行**的 `pct_change` 是**复权**口径。实测 603027 在 2025-10-23：不复权收盘 11.53 → 8.92
  （-22.6%，除权），同行 `pct_change` 却是 **+1.48%**。
- **影响面**（两类，都属"入场价与收益不同口径"）：
  1. **回测成交/估值价**：`cross_section_runner` 用这份 `close` 建共享账户的成交面板
     （`build_close_panel(clipped)`），而不复权口径会在每个除权日凭空记一笔跳空亏损
     （分红没进账户、送转直接当暴跌），收益口径却又是复权的。16 只高股息标的、
     2024-01-01~2025-12-31 的 `market_auntie` 回测实测：不复权 **-29.19%** / 回撤 **32.94%**，
     前复权 **-25.83%** / 回撤 **29.76%**——即凭空多出 **3.4pp** 亏损与 **3.2pp** 回撤
     （215 笔交易相同）。偏差最大的正是**高股息标的**，也就是股息率策略的主战场。
  2. **策略内形态计算**：窗口收益、价格分位、均线、持有期收益等跨除权日的量，若用不复权
     close 计算，除权跳空会被当成真实下跌/动量。
- **修复**：东财面板新增 **`close_qfq`** 列——由复权口径的 `pct_change` 累乘出复权因子，
  锚定「最后一行原始收盘价 = 前复权收盘价」还原：

  ```text
  factor[t]    = cumprod(1 + pct_change[t]/100)
  close_qfq[t] = close[最后一行] * factor[t] / factor[最后一行]
  ```

  抽样 250 只标的逐日比对，该式还原出的日收益与 TDX 前复权 K 线收益偏差在 **1e-12** 量级
  （即完全一致），确认与 `fetch_a_share_daily` 同口径。`pct_change` 缺失（停牌/新股首日）
  按 0 处理；若整列不可用则抛 `MarketDataError`，**不退回不复权价**（静默退回正是本缺陷本身）。
- **口径约定**（此后新增代码请遵守）：

  | 用途 | 用哪一列 | 理由 |
  |------|---------|------|
  | 跨除权日的收益 / 均线 / 分位 / 持有期收益 / 回测成交与估值 | `close_qfq` | 序列连续，与 TDX 前复权同口径 |
  | 价格区间过滤（`min_price`/`max_price`）、股息率分母、报告展示 | `close` | 语义是"当时的**实际**成交价" |

  日线分支（`needs_fundamentals=False`，`chip_accumulation` / `cyclical_rotation` /
  `etf_rotation` / `etf_filter`）的面板来自 `fetch_a_share_daily`，其 `close` 本身就是
  前复权、`pct_change` 也由它算出，两者天然同口径，无需 `close_qfq`。唯一的对称性差异是
  这些策略的价格区间过滤用的是前复权价（拿不到不复权价），基本面分支用不复权价。

  已按此调整：`cross_section_runner.execution_clipped_panel`（成交面板统一为前复权）、
  `limit_up_pullback`（区间收益/价格分位/均线/平台）、`prosperity_resonance`
  （动量/趋势均线/偏离度）、`new_stock_ice_reversal`（`entry_prices` 与持有期收益同口径）。
  `ValueBars` 同时暴露 `close` 与 `close_qfq`。
- **已知限制**：东财估值面板只有收盘价、**没有开盘价**，因此基本面分支的 6 个策略
  （`needs_fundamentals=True`）无法用 `execution_timing=next_day_open`（默认值）成交——
  此前会抛出无从下手的「open价面板为空」，现在改成明确的 `ValueError` 提示改用
  `same_day_close` / `next_day_close`。**不做静默降级**（降级成收盘价成交等于偷偷改掉成交时点）。
  若要支持次日开盘成交，需为基本面分支额外接入 TDX 前复权日线的 `open`。

## 缓存失效约定

K 线缓存（`backend/data/a_stock_data/klines/`）带格式版本 `version` 字段与复权标记 `adjust`：

- `app/data_sources/a_stock_data.py` 中 `_KLINE_CACHE_VERSION`；
- 读取时若 `version` 不符或 `adjust != 'qfq'`，整份缓存作废重拉，避免新旧口径混合；
- **前复权实现或字段语义有破坏性变更时，务必递增 `_KLINE_CACHE_VERSION`**，
  使所有存量缓存重建。

**写入路径同样必须校验版本**（v5→v6 修复）：`_write_klines_cache` 原先裸读旧 payload 并
无条件 merge 旧行、盖上当前版本号。读路径已把旧版本缓存作废，写路径却把它们捞回来重新盖章，
等于版本升级机制被架空——实测 `sh603027.json` 里 v1 未复权行（2017 年 -48% 断层）被保留，
且最后一次只刷新了最新 200 根，接缝正好落在 `n-200`。现在旧行仅在
「版本 + 复权口径 + 拉取日期（`cached_at`）三者都与当前一致」时才沿用：
前复权因子以最新一根 K 线为锚点，跨日沿用旧行会在接缝处留下虚假跳变。
丢弃旧行时 `requested_count` 一并重置为本次拉取量，避免虚高的计数让
`_cached_klines` 的 `requested_count < count` 守卫误判缓存够用。

## 其他注意事项

- **pandas 3.0 兼容垫片**：mootdx 0.11.x 的前复权与除权数据处理仍用 `fillna(method=...)`，
  该写法在 pandas 3.0 已移除。`mootdx_market_sdk.py` 在导入时安装了一个兼容垫片
  （把 `method='ffill'/'bfill'` 转译为 `ffill()/bfill()`），仅恢复被移除的关键字语义，
  不改变其他行为。若将来升级 mootdx 到修复版本，可评估移除该垫片。
- **换手率路径**（`fetch_a_share_daily_turnover`）走腾讯 `newfqkline`，本身即为前复权且口径正确，不受本缺陷影响。
- 前复权价格随最新除权动态重算：同一段历史在不同时间拉取可能略有差异，行情变化后需让缓存刷新。

## 相关代码

- `app/data_sources/mootdx_market_sdk.py`：`_forward_adjust_bars` / `_align_xdxr_to_bars` / `_get_xdxr_info` / `get_klines`
- `app/data_sources/a_stock_data.py`：`_KLINE_CACHE_VERSION` / `_cached_klines` / `_write_klines_cache`
- `app/data_sources/em_fundamentals.py`：`_forward_adjusted_close`（`close_qfq` 的还原式）
- `app/strategies/cross_section/value_bars.py`：`ValueBars.close`（不复权）/ `ValueBars.close_qfq`（前复权）
- `app/backtest/cross_section_runner.py`：`execution_clipped_panel`（成交/估值口径统一）
- `app/backtest/shared_report_model.py`：`_read_stale_day_klines`（裸读缓存，须同样校验版本与复权口径）

> 本项目当前没有 `tests/` 目录（`pytest` 尚无测试用例）。历史上文档提到过的
> `tests/test_a_stock_data_sdk.py` 已不存在。
