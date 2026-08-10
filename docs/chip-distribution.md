# 筹码分布分析（chip-dist）

本文档描述项目新增的**筹码分布分析**数据功能：基于历史换手衰减算法，估算个股当前流通筹码在价格轴上的成本分布，并派生获利盘、平均成本、成本区间与集中度等指标。

实现代码：`backend/app/indicators/chip_distribution.py`  
数据源：`backend/app/data_sources/market_data.py::fetch_a_share_daily_turnover`  
CLI 入口：`python -m app.script chip-dist`

> 筹码分布是统计估算，不是真实持仓数据。算法思路与通达信/同花顺的筹码成本分布一致，但不同软件对换手率口径、衰减系数与区间分档的处理存在差异，指标仅供研究参考。

## 1. 核心思想

把"流通筹码"看作一张分布在价格轴上的直方图（默认 200 个价格格点），用**历史换手率**逐日更新：

- 每个交易日按当日 `换手率` 作为"当日新筹码"占总筹码的比例；
- 旧筹码随换手**逐日衰减**：`分布[p] *= (1 - 换手率)`；
- 当日新筹码在 `[low, high]` 区间按**三角分布**分配，峰顶位于当日均价 `(high + low + close) / 3`；
- 逐日叠加后，最终把分布归一化为总和 1，即各价位筹码占全部流通筹码的比例。

初始分布为 0（不假设历史成本），数据长度决定了筹码最早回溯到哪一天；换手率越高的个股，旧筹码衰减越快，分布越"新"。

### 1.1 单日更新公式

对第 `t` 天（换手率 `turn_t`），在价格格点 `p` 上：

```
新分布[p] = 旧分布[p] × (1 - turn_t) + turn_t × 三角权重_p
```

其中三角权重在 `[low_t, high_t]` 区间内以 `avg_t = clip((high+low+close)/3, low, high)` 为峰顶线性递减到两端 0，区间内归一化；区间内没有格点的窄幅行情（如一字板）把当日全部筹码计入最近格点。

## 2. 变量定义

设共有 `N` 个价格格点，格点中心 `p_i`（`i = 1..N`），末日的筹码分布质量为 `d_i`，最新收盘价为 `C`。

| 指标 | 定义 |
| --- | --- |
| 获利盘比例 `profit_ratio` | `sum(d_i for p_i <= C)`，即成本不高于现价的筹码占比 |
| 平均成本 `average_cost` | `Σ d_i · p_i`，筹码分布的加权平均价 |
| 筹码峰值 `peak_price` | `argmax(d_i)` 对应的价格，即分布最密集的成本位 |
| 90% 成本区间 | `[p(5%), p(95%)]`，覆盖 90% 筹码的价位下/上沿（分位数截取） |
| 70% 成本区间 | `[p(15%), p(85%)]` |
| 集中度 `concentration` | `(高 - 低) / (高 + 低)`，区间越窄、数值越小，筹码越集中 |

价格网格上下各留 5% 余量：`p_min = min(low) × 0.95`，`p_max = max(high) × 1.05`。

## 3. CLI 用法

```bash
cd backend
# 基础用法（默认回溯 500 交易日）
python -m app.script chip-dist 000001

# 更多回溯天数
python -m app.script chip-dist 600519 --days 1000

# 完整 JSON 输出（含所有字段）
python -m app.script chip-dist 000001 --json

# 含完整分布数组（price_grid + distribution），可做可视化
python -m app.script chip-dist 000001 --detailed --output out/chip_000001.json
```

### 3.1 参数一览

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `symbol`（位置参数） | — | — | A 股代码，如 `000001` 或 `600000.SH` |
| `--days` | `500` | 50~5000 | 回溯交易日数量 |
| `--bins` | `200` | 50~1000 | 价格区间网格数，越大分布越精细 |
| `--json` | 关 | — | 以 JSON 打印完整结果 |
| `--output FILE.json` | 关 | — | 结果写入 JSON 文件 |
| `--detailed` | 关 | — | 额外返回 `price_grid` + `distribution` 数组 |

不带 `--json` 且不写 `--output` 时打印人类可读摘要：

```
筹码分布分析: 000001 平安银行
  数据区间: 2024-01-02 ~ 2025-12-31  (500 条)
  最新收盘价: 12.50
  获利盘比例: 67.85%
  平均成本:   11.20
  筹码峰值:   11.80
  90% 成本区间: [9.50, 13.20]
  90% 集中度:   0.1632
  70% 成本区间: [10.30, 12.10]
  70% 集中度:   0.0804
```

## 4. 输出字段（JSON）

对应 `app/schemas.py::ChipDistResponse`。除下表外，返回体含 `symbol`、`name`（股票名，来自 A 股股票池，未匹配到为 `null`）、`days_used`（实际使用的交易日数）、`disclaimer`。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `current_price` | float | 最新收盘价 |
| `profit_ratio` | float | 获利盘比例（0~1，如 `0.6785` 表示 67.85%） |
| `average_cost` | float | 平均成本（元） |
| `peak_price` | float | 筹码峰值（元） |
| `interval_90_low` / `interval_90_high` | float | 90% 成本区间下/上沿 |
| `concentration_90` | float | 90% 集中度（越小越集中） |
| `interval_70_low` / `interval_70_high` | float | 70% 成本区间下/上沿 |
| `concentration_70` | float | 70% 集中度 |
| `price_grid` | float[] \| null | 价格网格（`--detailed` 时返回） |
| `distribution` | float[] \| null | 各格点筹码占比，总和为 1（`--detailed` 时返回） |
| `warning` | string \| null | 所有交易日换手率为 0 时的提示 |

## 5. 数据来源

日线含换手率数据经**腾讯财经 `newfqkline`** 接口（`app/data_sources/tencent_finance_sdk.py::get_kline_with_turnover`）拉取，不依赖 akshare / mootdx：

- **真实历史换手率**：腾讯返回的换手率为交易所口径的当日真实换手率（已换算为小数，`0.01` = 1%），非流通股本推算的估计值。
- **前复权价格**：价格按 `qfq` 前复权返回，除权除息缺口已消除，成本分布不因送转跳变而失真。
- **翻页拉长历史**：单次请求最多约 640 条，`fetch_a_share_daily_turnover`（`app/data_sources/market_data.py`）以 `end` 参数逐页向前翻取，按日期去重后按时间升序拼接，覆盖 50~5000 交易日的回溯区间。
- **无跨源回退**：腾讯接口失败直接抛 `MarketDataError`（CLI 退出码 3），符合项目「行情失败硬失败」约定。

## 6. 适用场景与局限

- **适用**：个股筹码换手结构研究、成本分布估算、结合获利盘/集中度判断多空力量与套牢盘压力。
- **局限**：
  - **换手率口径**：腾讯换手率按实际流通股本计算，送转/增发/解禁导致的历史股本变化由腾讯自身口径反映；对上市早期、长期停牌等特殊阶段仍可能有偏差。
  - 算法假设换手筹码仅在当日 `[low, high]` 区间换手，忽略日内更细的成交价格结构。
  - 数据拉取失败（网络异常、代码无效等）时报 `MarketDataError`（CLI 退出码 3）。
  - 所有交易日换手率为 0 时（数据异常）返回 `warning` 字段，分布不反映真实成本结构。
