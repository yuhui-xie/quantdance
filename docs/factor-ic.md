# 因子 IC 分析（factor-ic）

本文档描述项目新增的**因子 IC（信息系数）分析**功能：对一组横截面因子，在每个时点
计算「因子值 × 未来 h 期收益」的秩相关系数（Spearman，即 RankIC），得到逐期序列，
再汇总为均值 RankIC、RankIC 标准差、ICIR、t 值与 IC>0 占比，用于评价因子对未来收益
的预测能力。

实现代码：`backend/app/ic_analysis.py`  
数据源：`backend/app/data_sources/market_data.py::fetch_a_share_daily`（前复权日线）  
CLI 入口：`python -m app.script factor-ic`

> IC 是**预测力评价**统计量，只回答"因子排序与未来收益排序是否一致"，不做任何
> 交易模拟，也不构成投资建议。

## 1. 核心思想

因子研究里，我们关心某因子（如动量、波动率、量比）的取值能否预测未来收益。做法是
在**每个时点 t**，对股票池横截面地比较两件事：

- 各股票在 t 的**因子值**；
- 各股票从 t 持有到 t+h 的**未来收益** `close[t+h]/close[t] - 1`。

两者（秩）相关系数越高，说明该因子越能"排序"出未来涨得好的股票，预测力越强。
把 t 沿时间轴滑动，得到逐期 RankIC 序列，再汇总结论。

## 2. 统计口径

### 2.1 RankIC（Spearman 秩相关）

环境无 scipy，用 numpy 手动实现：先对因子值与未来收益各自求**平均秩**（并列取平均），
再对两组秩算 Pearson 相关系数。

```
rank(x)_i = x 在横截面中升序排列的名次，并列取平均
RankIC_t = corr(rank(因子_t), rank(未来收益_t))
```

秩相关对极值与单调非线性更稳健，是因子评价最常用的口径。若某期横截面有效样本不足
或秩方差为 0，该期跳过。

### 2.2 汇总统计（每因子 × 每持有期）

对某因子在某持有期的逐期 RankIC 序列 `{ic_1, ..., ic_N}`：

| 指标 | 定义 |
| --- | --- |
| 均值 RankIC `mean_ic` | `mean(ic)`，整体预测力方向与强度 |
| 标准差 `std_ic` | `std(ic, ddof=1)`，预测力稳定性 |
| ICIR | `mean_ic / std_ic`，经风险调整后的预测力（类似信息比率） |
| t 值 `t_stat` | `mean_ic / (std_ic / sqrt(N))`，均值显著性的近似检验 |
| IC>0 占比 | `count(ic > 0) / N`，方向一致性 |
| 期数 `n_dates` | 有效（≥ min_cs）期数 |

## 3. 因子集合

因子定义与选股打分（`stock_screening.py`）**完全一致，公式统一归置在 `app/factors/` 中复用**。
每个因子在其所属因子模块里提供一个 **DataFrame→Series 的 handler**（`*_factor` 函数，
形式见 §3.3），`ic_analysis` 只负责把它们注册进 `FACTORS` 并评价，**不重新实现公式**：
- 选股技术因子（`ret_*`/`vol_*`/`vol_ratio`/`price_vs_ma5`/`ma_spread`/`sma5_slope`/`rel_vol`/`amihud_illiq_20`）：handler 在 `app/factors/screen_factors.py`，`stock_screening` 的 `compute_*_breakdown` 也取自此，末值即对应标量；
- 动量/趋势因子（`simple/bias/slope/efficiency_momentum`）：handler（`*_momentum_factor`）在 `app/factors/momentum.py`，用滚动包装器 `_rolling_scalar_series` 逐个时间窗复用其标量函数取整条序列；
- 策略信号因子（`llt`/`vpt`/`llt_slope`/`vpt_slope`/`adx`/`chop`）：handler 分别在 `app/factors/llt.py` / `volume_flow.py` / `range_motion.py`。

即「IC 因子 == screen 因子」。用 `--list-factors` 查看全部可用因子。

| 因子 | 来源 | 公式（向量化，t 为交易日） | 暖机 |
| --- | --- | --- | --- |
| `ret_5`/`ret_10`/`ret_20`/`ret_60` | 选股技术 | `close[t]/close[t-h] - 1` | 12 / 12 / 61 / 61 |
| `vol_20`/`vol_60` | 选股技术 | `std(日收益, 近 N 日)` | 61 |
| `vol_ratio` | 选股技术 | `activity[t] / mean(activity[t-20..t-1])`（排除当日，成交额优先） | 25 |
| `price_vs_ma5` | 选股技术 | `close/MA5 - 1` | 25 |
| `ma_spread` | 选股技术 | `(MA5 - MA20)/MA20` | 25 |
| `sma5_slope` | 选股技术 | `(MA5[t] - MA5[t-3])/|MA5[t-3]|`（近零分母→1） | 25 |
| `rel_vol` | 选股技术 | `volume[t] / mean(volume[t-19..t])`（含当日） | 21 |
| `amihud_illiq_20` | 选股技术 | `mean(|日收益|/(volume+1e-6), 近 20 日)` | 21 |
| `simple_momentum` | 动量/趋势 | `close[t]/close[t-20] - 1` | 21 |
| `bias_momentum` | 动量/趋势 | bias 窗口内归一化回归斜率×1e4（ma=20, mom=5） | 25 |
| `slope_momentum` | 动量/趋势 | 归一化 close 回归斜率×R²×1e4（5 日） | 5 |
| `efficiency_momentum` | 动量/趋势 | pivot 对数动量 × 效率系数（5 日） | 5 |
| `llt` | 策略信号 | LLT 趋势线原始值（period=20）⚠️ 平滑价格量纲随价格水平而异，横截面可比性有限 | 21 |
| `vpt` | 策略信号 | VPT 量价趋势原始值 ⚠️ 逐日累积量纲随成交量规模而异，横截面可比性有限 | 2 |
| `llt_slope` | 策略信号 | LLT 趋势线斜率（period=20，单点差分），与 my_strategy 动量方向一致 | 21 |
| `vpt_slope` | 策略信号 | VPT 量价趋势斜率（单点差分），与 my_strategy VPT 量价共振一致 | 2 |
| `adx` | 策略信号 | ADX 趋势强度（period=14），与 my_strategy `use_adx_filter` 一致 | 27 |
| `chop` | 策略信号 | Choppiness 震荡指数（period=14），与 my_strategy `use_chop_filter` 一致 | 15 |

> ⚠️ `llt`/`vpt` 为**原始值**（非斜率），横截面上量纲随价格/成交量规模而异，跨股票直接
> 比较意义有限（如 `llt` 原始值会与股价水平/小市值效应混淆，上面示例里 `llt` 呈现强负 IC）。
> 评价趋势/量价**方向**请优先用 `llt_slope`/`vpt_slope`。

### 3.1 因子 handler 约定（如何新增因子）

每个因子的 IC 计算**必须**复用其所属因子模块里的 handler，形式固定为：

```python
# 输入：单只股票的日线 DataFrame（DatetimeIndex，含 open/high/low/close[/volume]）
# 输出：与 df 索引对齐的因子时间序列 Series（暖机期含 NaN）
def xxx_factor(df: pd.DataFrame) -> pd.Series:
    ...
```

Handler 与因子公式**同处一个模块**，保证「IC 因子 == 对应标量」：凡是被 `stock_screening`
或各策略当作末根 K 线标量用的计算，其 IC handler 末值 `.iloc[-1]` 必须等于该标量
（由 `tests/test_ic_analysis.py` 锁定）。这样公式权威只有一份，消费方（选股、策略、IC）
各自取所需。

动量类标量只算末窗口、不能直接产出整条序列，用 `momentum.py::_rolling_scalar_series`
按 t 逐一传入截至 t 的历史、逐个时间窗调用标量，得到整条序列（`needs_ohlc=True` 时传整段
DataFrame，供需要 open/high/low/close 的因子用）。

**注册采用插件式自动注册**（与 `app/strategies` 同思路）：各因子模块导出自己的
`FACTORS: list[FactorSpec]`，`app/factors/registry.py` 用 `pkgutil` 扫描 `app.factors`、
自动汇总（重复 name 会抛错）。`FactorSpec` 定义在 `app/factors/base.py`（公共类型，无子模块
依赖，避免循环导入），只含 `name / fn / min_bars`——**来源标签不写在条目里**，由 registry
按所属模块自动推断（`screen_factors` → screening、`momentum` → momentum、其余 → strategy）。

新增一个因子只需两步，其余查找结构自动派生：

1. 在所属因子模块（`app/factors/*.py`）写 `*_factor(df) -> pd.Series` handler；
2. 在该模块末尾的 `FACTORS` 列表加一行（`FactorSpec` 从 `app.factors.base` import）：
   ```python
   FactorSpec("my_factor", my_factor_fn, min_bars=暖机K线)
   ```

`FACTOR_REGISTRY` / `FACTOR_MIN_BARS` / 来源分组（`SCREENING/MOMENTUM/STRATEGY_FACTORS`）与
`factor_source()` 均由 `app/factors/registry.py` 从汇总结果派生，新增因子时无需另行维护。
`source` 只决定 `--list-factors` 的分组展示，不影响 IC 计算，三选一：`screening`（选股技术）
/ `momentum`（动量趋势）/ `strategy`（策略信号）。

## 4. CLI 用法

```bash
cd backend
# 查看全部可用因子
python -m app.script factor-ic --list-factors

# 对中证500抽样评价全部因子，多持有期 5/10/20，横截面下限 10
python -m app.script factor-ic --universe zz500 --max-universe 300 \
  --start-date 2024-01-01 --end-date 2024-12-31

# 指定部分因子
python -m app.script factor-ic --universe zz500 --max-universe 100 --seed 42 \
  --start-date 2024-01-01 --end-date 2024-12-31 \
  --factors ret_20 vol_20 vol_ratio

# 完整 JSON 输出（含逐期 ic_series），落盘
python -m app.script factor-ic --universe zz500 --max-universe 100 \
  --start-date 2024-01-01 --end-date 2024-12-31 --json --output out/ic.json

# 不指定起止日期 → 回溯最近 1200 个交易日（--limit 可调）
python -m app.script factor-ic --universe hs300 --max-universe 100

# 用请求 JSON 驱动（可放股票池与全部分析参数，CLI 显式值优先覆盖）
python -m app.script factor-ic --request examples/factor_ic.json
```

### 请求 JSON（`--request FILE.json`）

与 `backtest --request` 用法一致：JSON 里可放 `symbols`/`universe`/`max_universe`/
`seed` 及全部分析参数；未在 CLI 显式提供的参数以 JSON 为准，CLI 显式值优先。
参考 `backend/examples/factor_ic.json`：

```json
{
  "universe": "zz500",
  "max_universe": 100,
  "seed": 42,
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "horizons": [5, 10, 20],
  "min_cs": 5,
  "factors": ["ret_20", "vol_20", "vol_ratio"],
  "output_options": {"json": false, "output": "out/ic.json"}
}
```

### 参数说明

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--request` | — | 请求 JSON（可含股票池与全部分析参数，CLI 覆盖） |
| `--universe` | — | 股票池预设（all_a/hs300/zz500/zz1000/…） |
| `--symbols` | — | 直接指定代码列表（与 `--universe` 二选一） |
| `--max-universe` | 300 | 股票池抽样上限 |
| `--seed` | — | 抽样随机种子（可复现） |
| `--start-date`/`--end-date` | — | 回测区间；须同时提供，否则用尾部 `--limit` 窗口 |
| `--limit` | 1200 | 未给起止时回溯交易日数 |
| `--horizons` | 5 10 20 | 未来收益持有期（交易日），可多个 |
| `--min-cs` | 10 | 每期横截面有效样本下限，低于则跳过该期 |
| `--factors` | 全部 | 要评价的因子名，空格分隔 |
| `--max-workers` | 8 | 并行拉取行情的线程数 |

## 5. 输出结构（--json）

```json
{
  "universe_note": "...", "symbols": ["600000", "..."], "count": 300,
  "start_date": "2024-01-02", "end_date": "2024-12-30",
  "horizons": [5, 10, 20], "min_cs": 10,
  "results": [
    {
      "factor": "ret_20", "source": "screening",
      "horizons": [
        {
          "horizon": 5, "n_dates": 216,
          "mean_ic": 0.0105, "std_ic": 0.27, "icir": 0.038,
          "t_stat": 0.557, "ic_positive_ratio": 0.519,
          "ic_series": [{"date": "2024-01-02", "ic": 0.13}, "..."]
        }
      ]
    }
  ],
  "warnings": [], "disclaimer": "演示用途，IC 分析不构成投资建议。"
}
```

## 6. 注意事项

- **幸存者偏差**：预设指数池用"当前成分股"，历史区间存在幸存者偏差，结论仅供研究。
- **前视窗口**：因子在 t 取值、未来收益在 t..t+h，属标准 IC 评价口径；不含任何未来
  信息进入因子取值，但持有期收益自然覆盖未来——这是 IC 的意义所在，不是错误。
- **暖机与边缘 NaN**：因子有暖机期（样本前段缺失），未来收益在样本后段 `horizon` 根
  缺失；这些期因有效样本不足会被 `--min-cs` 过滤或跳过。
- **解读**：`|t|` 接近/超过 2 或 ICIR 显著为正，才表明因子有一定稳定预测力；
  单期 IC 绝对值普遍较小（0.0x 量级）属正常，看的是统计显著性与方向一致性，而非数值大小。
