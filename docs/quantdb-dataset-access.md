# quantdb 数据集访问指南（全部表）

[`quantdb 数据源`](quantdb-data-source.md) 只接了回测用的两张 K 线表。数据集里其实还有
基础资料、财务、估值、技术指标、盘口情绪、ML 因子宽表等二十多张表，本文说明**怎么访问
它们**：命令行 `dataset` 子命令，以及 `app.data_sources.quantdb` 的编程接口。

> 这些都走同一套本地镜像与增量更新：先查本地有没有，缺的才按需下载。

## 表清单

| 表名 | 布局 | 内容 | 远端全量 | 关键列 |
|------|------|------|---------|--------|
| `kline_stock` | daily | 股票日线·**等比前复权** | 403 MB | symbol, time, ohlcv, amount |
| `kline_stock_raw` | daily | 股票日线·**不复权** | 235 MB | 同上 |
| `kline_stock_hfq` | daily | 股票日线·**等比后复权** | 415 MB | 同上 |
| `kline_etf` | daily | ETF 日线（不复权） | 44 MB | 同上 |
| `index_daily` | daily | 指数日线（宽基/风格/行业/主题） | 8.5 MB | symbol, time, close, **Category** |
| `margin_trading` | daily | 融资融券（余额/买入/偿还/净额） | 144 MB | symbol, time, finance_*, slo_* |
| `valuation` | daily | 估值（PE/PB/PS/市值/股息率） | 857 MB | symbol, time, close, pe_ttm, pb, total_mv… |
| `technical_indicators` | daily | 技术指标（MA/RSI/KDJ/MACD/ATR…） | 2.6 GB | symbol, time, close, ma*, rsi*, future_return_* |
| `market_sentiment` | daily | 盘口情绪与流动性 | 1.0 GB | symbol, time, close, buy_pressure, liquidity_score… |
| `features_daily` | daily | 日线特征宽表 78 列 | 3.6 GB | symbol, time, close, … |
| `l1_factors` | daily | L1 日频因子 119 列 | 7.5 GB | symbol, **date**, close, turn_*, mom_*, … |
| `l1_l2_factors` | daily | L1+L2 因子宽表 329 列（2018 起） | 25 GB | symbol, **date**, …（含 micro_trade_*） |
| `factor_dictionary` | daily | 动态因子宽表 571 列（2018 起） | 34 GB | symbol, **date**, …（gtja_*/risk_fac_*） |
| `fin_balance` | per_symbol | 资产负债表 75 列 | 333 MB | m_timetag, m_anntime, … |
| `fin_income` | per_symbol | 利润表 37 列 | 188 MB | m_timetag, m_anntime, revenue… |
| `fin_cashflow` | per_symbol | 现金流量表 68 列 | 324 MB | m_timetag, … |
| `fin_capital` | per_symbol | 股本结构 | 45 MB | m_timetag, total_capital… |
| `fin_holder_num` | per_symbol | 股东户数与机构持仓 | 147 MB | m_timetag, holder_num… |
| `fin_per_share` | per_symbol | 每股指标 TTM | 49 MB | m_timetag, net_profit_ttm… |
| `fin_pershare_index` | per_symbol | 每股财务比率 86 列 | 437 MB | m_timetag, equity_roe… |
| `dividend_factors` | per_symbol | 分红送配（可自算复权因子） | 25 MB | time, type, interest, stockBonus, allotment |
| `instrument_detail` | single | 合约基础资料 152 列 | 3.2 MB | Symbol, Name, Unit, MinPrice, BelongHS300… |
| `sector_concept` | single | 行业/概念板块成分 | 0.2 MB | SectorCode, SectorName, SectorType, Symbol |
| `trading_calendar` | single | 交易日历 | 20 KB | TradingDate, IsTradingDay |
| `index_weights` | single | 指数成分权重（每指数一文件） | 30 KB | Symbol, Weight, IndexCode |
| `convertible_bond` | single | 可转债基础信息 | 50 KB | BondCode, bondConvPrice… |
| `etf_pcf` | single | ETF 申赎清单（3 个文件） | 1.1 MB | EtfCode / ComponentCode / TrackIndex… |

三种布局的含义：

- **daily**：`<表目录>/dt=YYYYMMDD/data.parquet`，一天一个分区，可按日期区间只下载需要的天；
- **per_symbol**：`<表目录>/600519.SH.parquet`，一标的一文件，按标的下载；
- **single**：`<表目录>/*.parquet`，整张表就几个小文件，直接整目录拉。

## 命令行

```bash
# 列出全部表、布局与本地镜像情况
python -m app.script dataset --list-tables

# 看某张表的列结构与前几行（只下载一个样本文件；逐股表可加 --sample-symbol 600519）
python -m app.script dataset --describe valuation
python -m app.script dataset --describe fin_balance --sample-symbol 600519

# 读数据：日期区间 + 代码 + 列子集
python -m app.script dataset --read valuation --symbols 600519 000001 \
    --start-date 2026-09-01 --end-date 2026-10-30 --columns symbol time pe_ttm pb
python -m app.script dataset --read fin_balance --symbols 600519 --columns m_timetag tot_assets --limit 8
python -m app.script dataset --read index_daily --symbols 000300.SH --start-date 2026-09-01
python -m app.script dataset --read trading_calendar --limit 20
python -m app.script dataset --read l1_factors --symbols 600519 --start-date 2026-09-01 \
    --columns symbol date close turn_20 mom_rsi_14 --output out/l1_600519.csv

# 落盘：--output 按后缀写 .csv / .parquet / .json；--json 直接打印全部行
```

参数语义：

| 参数 | 说明 |
|------|------|
| `--start-date/--end-date` | 按 `date_col` 过滤行；daily 表同时决定下载哪些分区 |
| `--symbols` | 逐股表**必填**；daily/single 表可选，按代码列过滤 |
| `--columns` | 只返回这些列（先按它读 parquet，再按同样顺序输出） |
| `--limit` | daily 表 = 最近 N 个**交易日分区**；逐股表 = 每只最近 N 行；single 表 = 末尾 N 行 |
| `--head N` | 控制台预览行数（默认 8），全部数据请用 `--output` |
| `--output` | `.csv`（utf-8-sig）/`.parquet`/`.json` |

注意：daily 表给了 `--start-date/--end-date` 时 `--limit` 不再截断（区间优先）。

同步（下载到本地镜像）也用同一套参数：

```bash
# 只补某个区间（daily）
python -m app.script dataset --sync --tables valuation --start-date 2026-01-01 --end-date 2026-10-30
# 逐股表按标的补
python -m app.script dataset --sync --tables fin_balance --symbols 600519 000001
# 逐股表全量（会下几千个文件，注意体积）
python -m app.script dataset --sync --tables fin_balance --all
# 单文件表整目录拉取
python -m app.script dataset --sync --tables instrument_detail
```

`dataset --update` 只复查**已镜像**的内容：daily 表复查已镜像的每一天，再补「最新已镜像日
之后」的新分区（跨度超过 30 天视为稀疏镜像，不自动补，只提示该用的 `--sync --start-date`）；
逐股表只复查已镜像的标的；single 表整目录复查。没镜像过的表不会被动拉全量。

## 编程接口

```python
from app.data_sources.quantdb import get_store

store = get_store()

# 列表 / 结构
store.list_tables()
store.describe_table("valuation")            # dict：columns / preview / sample_file

# 读取（返回扁平 DataFrame，列名与数据集一致）
df = store.read_table(
    "valuation",
    start="2026-09-01", end="2026-10-30",
    symbols=["600519", "000001.SZ"],          # 6 位代码会自动补 .SH/.SZ/.BJ
    columns=["symbol", "time", "pe_ttm", "pb"],
)

# 逐股表必须给 symbols；limit 是每只标的的行数
fin = store.read_table("fin_income", symbols=["600519"], columns=["m_timetag", "revenue"], limit=8)

# 只要确定镜像存在，也可以直接按路径读 parquet
store.partition_path("valuation", "20261030")
store.file_path("fin_balance", "600519.SH.parquet")
```

`read_table` 内部会先 `sync` 缺的部分，所以首次读某张表/某段日期会自动下载；下载失败或
远端不可达时抛 `QuantDBError`（CLI 里转成 `MarketDataError`，退出码 3），不会换源。

## 口径（务必先看）

同一只股票在不同表里的 `close` **不是同一个价格基准**（实测 `600519` 2024-06-20）：

| 基准 | 值 | 出现在 |
|------|----|--------|
| 前复权 | 1391.36 | `kline_stock` |
| 不复权 | 1500.10 | `kline_stock_raw`、`valuation.close`、`market_sentiment.close` |
| 后复权 | 1754.65 | `kline_stock_hfq`、`technical_indicators.close`、`features_daily` / `l1_factors` / `l1_l2_factors` / `factor_dictionary` 的 `close` |

- 同一张表内部收益率是一致的（等比复权只差一个比例），**跨表混用绝对价格会错**；
- 前复权锚定最新交易日，因此**历史价格会随新分红变化**；后复权锚定上市首日，历史价格稳定；
- K 线类表 `amount` 单位是**万元**，`volume` 单位股，价格元；`margin_trading` 的
  `finance_*`/`slo_*` 同为万元；`valuation.total_mv/float_mv` 单位**元**，
  `total_capital` 单位股，`dividend_rate` 是小数（0.037 = 3.7%）；
- 日期列名不统一：K 线/估值/技术指标/情绪/features 用 `time`，`l1_factors` 之后的三张
  宽表用 `date`，财务表用 `m_timetag`（报告期，int）与 `m_anntime`（公告日，int），
  交易日历用 `TradingDate`（int）；
- ETF 只有不复权日线，没有复权因子，折算日会出现成倍跳空（见 quantdb 数据源文档）。

## 各表能用在哪

- **`index_daily`**：本地就有沪深300/中证500 等指数日线，可替代 akshare 的基准曲线
  （`Category` 列区分宽基/行业/主题）；`index_weights` 提供指数成分权重快照（7 个指数）；
- **`valuation`**：横截面策略的 `needs_fundamentals` 分支、`screen` 的基本面因子所需的
  PE/PB/市值/股息率都在这里，且是逐日的（含历史），比现用的腾讯快照口径更完整；
- **`fin_*`**：三大报表 + 股本 + 股东户数 + 每股指标，逐股文件、按 `m_timetag` 报告期；
  注意是**报告期**数据，做时点回测要用 `m_anntime`（公告日）避免未来函数；
- **`dividend_factors`**：分红送配明细（派息/送股/转增/配股价），可据此自己算复权因子；
- **`sector_concept`**：行业与概念板块成分，可用于板块轮动/拥挤度；`SectorType` 区分
  行业/概念；
- **`technical_indicators` / `market_sentiment` / `features_daily` / `l1_*`**：现成的技术
  指标、盘口情绪、ML 因子宽表，`technical_indicators` 还带 `future_return_*`；
- **`instrument_detail`**：合约基础资料（含 `BelongHS300`、`BelongRZRQ`、上市/退市日期等），
  可作为股票池的本地来源（见下方注意）；
- **`etf_pcf`**：ETF 申赎清单与成分，用于 ETF 折溢价/规模分析。

## 限制与注意

- **体积**：`l1_l2_factors` 25 GB、`factor_dictionary` 34 GB，别随手 `--sync --all`；
  日表请用日期区间读，逐股表请指定 `--symbols`；
- **`etf_pcf` 三个文件结构不同**（`etf_pcf` 是申赎清单、`etf_components` 是成分、`trackzs_etf_info`
  是跟踪指数信息），一次读取会把它们纵向合并成宽表，不重叠的列为 NaN；需要精确结构时
  直接 `pd.read_parquet(store.file_path("etf_pcf", "etf_pcf.parquet"))`；
- **`instrument_detail.Name` 是正常的 UTF-8 中文**（如 `贵州茅台`）。早期文档里写的「乱码」
  是 Windows 控制台编码问题，数据本身没问题；
- 本数据源目前**只有 K 线接入了回测/选股/IC 的主流程**（`--data-source quantdb`）；上面
  这些表需要通过 `dataset --read` 或 `read_table()` 显式取用，不会自动替换现有来源；
- 数据集 README 自述「历史数据不保证 100% 准确」，用于研究前建议抽样校验。
