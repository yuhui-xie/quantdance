# quantdb 数据源（ModelScope QuantDB 数据集）

把 ModelScope 上的公开数据集 [`qusong0627/LightGBM_Alpha300`](https://www.modelscope.cn/datasets/qusong0627/LightGBM_Alpha300)
接成 `fetch_a_share_daily` 的可选数据源。选择它时，本地缺什么就按需下载什么；数据集
每周更新一次，用 `dataset --update` 增量补齐。

## 数据集是什么

- 中国 A 股全市场（约 5500 只，含北交所）2016 年至今的日线，另有约 1700 只 ETF 日线；
- 按**交易日**分区存放：`<表>/dt=YYYYMMDD/data.parquet`，每个分区是当日全市场；
- 股票提供不复权 / 等比前复权 / 等比后复权三套日线，ETF 只有不复权；
- 单位统一：价格=元、volume=股、amount=**万元**（注意与其它来源不同）。

本数据源目前只接了两张日线表：

| 表 | 仓库路径 | 口径 |
|----|----------|------|
| `kline_stock` | `1_kline_data/daily_forward` | 等比前复权（锚定最新交易日） |
| `kline_etf` | `4_bond_etf/etf_kline` | **不复权** |

## 用法

```bash
# 查看本地镜像状态（默认动作）
python -m app.script dataset --status

# 按日期区间补齐镜像（只下载这段时间）
python -m app.script dataset --sync --start-date 2024-01-01 --end-date 2026-10-30

# 全量镜像（约 500MB，首次较慢，之后所有区间都命中本地）
python -m app.script dataset --sync --all

# 强制检查远端并增量更新已镜像内容（见下方「增量更新」）
python -m app.script dataset --update
```

回测 / 选股 / 挑标的 / 相关性 / 因子 IC 都支持 `--data-source quantdb`：

```bash
python -m app.script backtest --strategy ma_crossover --symbol 600519 --bars 500 --data-source quantdb
python -m app.script backtest --strategy etf_rotation --mode universe --symbols 510300 510500 \
    --start-date 2025-01-01 --data-source quantdb
python -m app.script screen --preset momentum --data-source quantdb
python -m app.script pick --strategy etf_rotation --asof 2026-09-30 --data-source quantdb
python -m app.script correlation --symbols 510300 510500 --data-source quantdb
python -m app.script factor-ic --symbols 600519 000001 --data-source quantdb
```

请求 JSON 里写 `"data_source": "quantdb"` 等价（`screen` 的 `ScreenRequest` 同样支持）。

数据集里还有财务、估值、技术指标、ML 因子宽表等二十多张表，用 `dataset --list-tables /
--describe / --read` 访问，见 [quantdb-dataset-access.md](quantdb-dataset-access.md)。

## 本地镜像与更新机制

默认镜像目录 `backend/data/quantdb/`（已被 `.gitignore` 忽略）：

```
backend/data/quantdb/
├── _state.json                      # revision、分区清单(sha256)、下载记录
├── 1_kline_data/daily_forward/dt=20260930/data.parquet
├── 4_bond_etf/etf_kline/dt=20260930/data.parquet
└── derived/
    ├── kline_stock/data.parquet     # 按 symbol 排序的派生索引 + index.json
    └── kline_etf/data.parquet
```

**按需下载**：`fetch_a_share_daily(..., data_source="quantdb")` 先看本地镜像是否覆盖
请求区间；`limit=N` 取最近 N 个交易日分区，`start/end` 取该区间，缺的分区才下载。

**增量更新**：

1. `_state.json` 里记录 `checked_at`；距上次检查超过 `QUANTDANCE_QUANTDB_CHECK_HOURS`
   （默认 24 小时）才会向 ModelScope 问一次仓库 revision（一次 HTTP 调用）；
2. revision 没变就完全不动；变了才重新列分区清单，并按每个文件的 sha256 对比，
   只下载**新增**和**被改写**的分区；
3. `dataset --update` 强制走一遍上述检查，范围只有**已镜像的内容**：
   - daily 表：复查已镜像的每一天，再补「最新已镜像日之后」的新分区；
     若最新已镜像日离数据集最新日超过 30 天（说明镜像很稀疏，只读过个别日期），
     **不自动补齐**，只打一行提示并给出该用的 `dataset --sync --start-date ...` 命令，
     避免一下子拉几 GB；
   - 逐股表：只复查已镜像的标的；新增标的要显式 `--sync --symbols`；
   - single 表：文件少，整目录复查；
   - 从没镜像过的表不会被 `--update` 拉全量（要用 `--sync` 显式指定）。

   日常保持全量镜像时，`dataset --update` 就是「把最近一周新增的分区补上」。

**离线行为**：远端不可达但本地已覆盖请求区间时，打一行 stderr 告警后继续用本地数据；
覆盖不到才抛 `MarketDataError`（CLI 退出码 3）。不换源、不回退合成数据。

**派生索引**：数据集按日分区，逐只标的读取要去扫几千个文件。首次同步后会把镜像里
所有分区拼成一份按 `symbol` 排序、小 row group 的 parquet（`derived/<表>/data.parquet`），
按 `(symbol, 日期)` 过滤时只读命中的 row group。索引签名 = 镜像分区的 `(dt, sha256)`
集合，镜像扩展或 revision 变化时整体重建。

实测（2026-11，2628 个交易日、1085 万行）：

- 股票全量镜像：2 分 15 秒，分区 384MB + 索引 464MB；
- ETF 全量镜像：约 150MB；
- 索引命中后单只标的全历史读取约 30~40ms（1668 只 ETF 的 as-of 扫描约 1 分钟）。

## 数据校验（2026-11）

- **对旧缓存的抽样**：随机取 `backend/data/a_stock_data/klines/day` 200 只（193 只可比），
  对 `adjust=qfq` 的条目，相对差中位数 0、75 分位 4e-6；出现差异的都是**常数比例**，
  来自「缓存最后日期之后又发生了一次除权」（如前复权锚点不同，`000807` 3.4%、
  `601919` 2.6% 正好对应 2026-10 的除权日），不是口径问题；
- **旧版缓存**：`adjust` 字段缺失的条目（早期抓取版本写入）差异很大（中位数 31%），
  属旧缓存自身的问题，与 quantdb 无关；
- **独立反推**：用腾讯不复权日线算 `quantdb_close / raw_close` 得到数据集的前复权因子，
  该因子在每个除权日都按原料价格缺口跳变、末尾恒为 1，说明股票的等比前复权是正确的；
- **ETF**：同一方法下 ETF 因子在折算日为 2.0 / 3.0 这类整数倍，即上文所说的不复权跳空。

复核脚本思路：读缓存 JSON 的 `rows[].datetime/close`，`pd.to_datetime(...).dt.normalize()`
对齐后与 `get_store().read_daily(code, start=..., end=...)` 比较比值；比值恒定 = 锚点差异，
比值跳变 = 除权日。

## 口径与限制

- **股票**：前复权，与 `a_stock_data` 的口径一致（等比前复权），可直接替换；
- **ETF 只有不复权日线**。数据集不含 ETF 复权因子，分红日有跳空，**份额折算 / 拆分日
  会出现成倍的虚假涨跌**（实测 `159880` 在 2026-08-03 有一次 1 拆 2，价格直接腰斩；
  `515000` 在 2025-09-08 同样）。跨折算日的 ETF 回测收益、回撤不可用，对只看短周期
  动量的策略影响较小。凡是用到 ETF，stderr 会打一行告警：`[quantdb] ETF 日线为不复权价…`；
- **下列内容仍走原有数据源**，`--data-source quantdb` 只改变日线 K 线：
  - 股票池列表（`all_a` / 指数成分 / ETF 列表 / config 池）；
  - 估值与财务面板（横截面策略的 `needs_fundamentals` 分支、`screen` 的基本面因子）；
  - 换手率（`chip-dist`、`needs_turnover` 策略走腾讯日线）；
  - 沪深300基准（akshare）。
  这些路径与数据源选择不一致时，输出里的 `universe_note` 会说明股票池来源。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `QUANTDANCE_QUANTDB_DIR` | `backend/data/quantdb` | 本地镜像根目录 |
| `QUANTDANCE_QUANTDB_CHECK_HOURS` | `24` | 远端 revision 检查间隔；`0`=每次读取都查 |
| `QUANTDANCE_QUANTDB_WORKERS` | `8` | 分区下载并发数（上限 16） |
| `QUANTDANCE_QUANTDB_REPO` | `qusong0627/LightGBM_Alpha300` | 覆盖数据集仓库 |

依赖：`pyarrow>=15`（读 parquet + row group 过滤），已加入 `requirements.txt`。

## 已知问题

- `instrument_detail` 的 `Name` 列是正常 UTF-8 中文（早期判断为乱码，实为 Windows 控制台
  编码问题），但本数据源仍只用它做 K 线，股票池与名称继续走原有来源；
- 数据集 README 自述「历史数据不保证 100% 准确」；发现异常时先对比同分区的不复权 /
  后复权序列，再决定是否回退到 `a_stock_data`。
