# ETF 行情数据问题排查记录

记录 ETF 行情数据（`fetch_a_share_daily` 返回的前复权日线）曾出现的数据缺陷、修复方案、
检查方法与当前结论。相关复权公式见 [data-adjustment.md](data-adjustment.md)。

## 背景：ETF 与普通 A 股的差异

普通 A 股日涨跌停限制为 ±10%；**创业板 / 科创板跟踪的 ETF 为 ±20%**。因此排查断层时
不能把 20% 以内的隔夜波动当作异常，而把 30% 以上（远超任何涨跌停限制）的隔夜跳变视为
**未消除的复权断层 / 数据损坏**。

## 曾出现的缺陷（已修复）：ETF 份额折算断层

### 现象

| ETF | 日期 | 隔夜跳变 | 类型 |
|-----|------|---------|------|
| 512930 | 2026-05-22 | 2.748 → 0.705（**-74%**） | 份额拆细（1 拆 4） |
| 512200 | 2024-08-12 | 0.441 → 1.191（**+170%**） | 份额合并（缩股） |

两者均远超 ±20% 限制，明显非真实行情。

### 根因

TDX 除权记录里，普通股票分红送转是 `category==1`，而 ETF 份额折算/拆分是 `category==11`
（字段 `suogu` = 份额比例，1 份变 `suogu` 份、价格 ×`1/suogu`）。`_get_xdxr_info`
原先只取 `category==1`，漏掉了 `category==11`，导致前复权未折算、在折算日留下断层。

### 修复

`_get_xdxr_info`（`app/data_sources/mootdx_market_sdk.py`）改为同时纳入 `category==11`，
并把 `suogu` 折算为等价的送转股 `songzhuangu=10*(suogu-1)`，与分红送转共用同一条前复权公式。
注意 `suogu` **双向有效**：

- `suogu > 1`：拆细（价格下调，如 512930 的 4.0 → `songzhuangu=30`）
- `suogu < 1`：合并（价格上调，如 512200 的 0.358 → `songzhuangu≈-6.42`，公式分母仍为正）

仅跳过 `suogu<=0` 的无有效比例记录（避免除零）。

由于前复权口径变更，`_KLINE_CACHE_VERSION` 已递增（v3→v4→v5，v5 补齐合并方向），
存量缓存整体作废重建。

## 检查方法

用隔夜跳变扫描发现断层。阈值取 **30%**（ETF 最高 20% 涨停限制之上留余量），
同时自动忽略 akshare 列表里但 mootdx 无 K 线的标的（那是覆盖缺口，非断层）。

复用脚本（临时，见 `backend/` 根目录）：

```bash
cd backend
# 先拉全市场 ETF 列表（写 _etf_scan_list.json）
.venv/Scripts/python.exe -c "from app.data_sources.market_data import fetch_etf_universe as f; import json; json.dump([r['symbol'] for r in f(max_universe=2000)[0]], open('_etf_scan_list.json','w'))"
# 抽样 400 只扫描，>30% 隔夜跳变写入 _etf_scan_result.jsonl
.venv/Scripts/python.exe _etf_scan.py
```

`_etf_scan.py` 内可调 `SAMPLE`（抽样数）与 `THRESH`（断层阈值）。

## 当前结论（2026-08 复查）

- **核心池**（`config/etf_core_pool.json`，48 只，实际用于策略的标的）：0 处 >30% 断层。
- **全市场抽样 400 只**（自 1681 只随机抽样）：0 处 >30% 断层。
- 上述 12% 阈值扫出的少量隔夜波动（如 159915/588000/159949 在 2024-09-30、2024-10-08、
  2024-10-09、2025-04-07 的 ±20% 内波动）均为**真实行情**（2024-10 政策行情、2025-04 关税
  冲击），且都在 20% 涨停限制内，**非数据断层**。

**结论：前复权断层问题已修复，当前未发现残留断层。**

## 已知限制（非本次缺陷，单独记录）

akshare ETF 列表中存在、但 **mootdx/TDX 无任何 K 线数据** 的标的（2020–2026 范围复查抽样
400 只中出现 8 只，如 158000、561270、159096、516040、159799 等）。这些大概率是
新上市/小众 ETF 未被 TDX 行情服务器收录，调用会抛 `MarketDataError`，且不做数据源回退
（见 `market_data.py` 约定）。属于**数据覆盖缺口**，与本次复权断层无关，后续可考虑
接入腾讯/东财作为 ETF 备用行情源。

## 相关代码

- `app/data_sources/mootdx_market_sdk.py`：`_forward_adjust_bars` / `_get_xdxr_info` / `get_klines`
- `app/data_sources/a_stock_data.py`：`_KLINE_CACHE_VERSION` / 缓存读写
- `tests/test_a_stock_data_sdk.py`：`test_get_xdxr_info_converts_category11_suogu`、
  `test_forward_adjust_smoothes_etf_split_category11`、`test_forward_adjust_smoothes_etf_consolidation`
