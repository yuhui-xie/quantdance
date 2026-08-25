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

## 缓存失效约定

K 线缓存（`backend/data/a_stock_data/klines/`）带格式版本 `version` 字段与复权标记 `adjust`：

- `app/data_sources/a_stock_data.py` 中 `_KLINE_CACHE_VERSION`；
- 读取时若 `version` 不符或 `adjust != 'qfq'`，整份缓存作废重拉，避免新旧口径混合；
- **前复权实现或字段语义有破坏性变更时，务必递增 `_KLINE_CACHE_VERSION`**，
  使所有存量缓存重建。

## 其他注意事项

- **pandas 3.0 兼容垫片**：mootdx 0.11.x 的前复权与除权数据处理仍用 `fillna(method=...)`，
  该写法在 pandas 3.0 已移除。`mootdx_market_sdk.py` 在导入时安装了一个兼容垫片
  （把 `method='ffill'/'bfill'` 转译为 `ffill()/bfill()`），仅恢复被移除的关键字语义，
  不改变其他行为。若将来升级 mootdx 到修复版本，可评估移除该垫片。
- **换手率路径**（`fetch_a_share_daily_turnover`）走腾讯 `newfqkline`，本身即为前复权且口径正确，不受本缺陷影响。
- 前复权价格随最新除权动态重算：同一段历史在不同时间拉取可能略有差异，行情变化后需让缓存刷新。

## 相关代码

- `app/data_sources/mootdx_market_sdk.py`：`_forward_adjust_bars` / `_get_xdxr_info` / `get_klines`
- `app/data_sources/a_stock_data.py`：`_KLINE_CACHE_VERSION` / 缓存读写
- `tests/test_a_stock_data_sdk.py`：`test_mootdx_adapter_klines_fetches_raw_then_forward_adjusts`、
  `test_forward_adjust_smoothes_ex_right_gap`、`test_get_xdxr_info_converts_category11_suogu`、
  `test_forward_adjust_smoothes_etf_split_category11`、`test_forward_adjust_smoothes_etf_consolidation`、
  `test_a_stock_data_rebuilds_cache_without_adjust_marker`
