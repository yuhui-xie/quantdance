# 我的策略（my_strategy）说明

本文档描述单票 `my_strategy` 策略：以 **LLT 斜率动量**刻画趋势方向与拐点，
用**正负阈值滞回**过滤拐点附近噪音，默认叠加 **ADX 趋势强度过滤**与
**Choppiness 震荡市过滤**，可选叠加 **VPT 量价过滤**。

实现代码：`backend/app/strategies/my_strategy.py`  
量价指标：`backend/app/factors/volume_flow.py`

## 1. 核心逻辑：LLT 斜率动量（滞回）

LLT 序列 `L_t` 及其斜率 `D_t = L_t - L_{t-K}` 的计算见
[llt-trend-strategy.md](./llt-trend-strategy.md) 第 1、2 节。

> **斜率拟合窗口（平滑）**：默认 `slope_fit_window=1`，斜率即单点差分 `L_t - L_{t-K}`，
> 变动快、对噪音敏感。若希望趋势更平滑，可设 `slope_fit_window >= 2`，此时改为对最近
> 该根 K 线的 LLT 趋势线做**滚动最小二乘线性回归**、取拟合斜率——用整段窗口刻画趋势，
> 对噪音更稳健，`slope_fit_window` 越大越平缓（但对拐点响应也越慢）。
>
> 回归同时输出拟合质量 **R²**（叠加曲线 `llt_r2`）：越接近 1 说明趋势越贴近一条直线、
> 斜率越可信；接近 0 说明窗口内涨跌混乱、拟合方差大、斜率不可信。可设
> `min_fit_r2`（如 0.5）要求 R² 达标才允许进场，拟合差时**放弃入手**。

- **进场**：`D_t > +threshold` 时由空仓转持仓（动量向上）。
- **离场**：`D_t < -threshold` 时由持仓转空仓（动量转弱）。
- 介于 `[-threshold, +threshold]` 之间为**观望死区**，保持原状态，避免拐点附近反复进出。
  设 `threshold=0` 即退化为过零逻辑（上穿 0 买、下穿 0 卖）。

## 2. 可选过滤

### Choppiness 震荡市过滤

`use_chop_filter=True`（默认）时，当 Choppiness Index `CHOP > chop_threshold`
（默认 62）判定为震荡市，该区间**强制空仓、不开趋势单**，避免趋势策略在无趋势
行情中反复进出。指标见 `backend/app/factors/range_motion.py`。

### ADX 趋势强度过滤

`use_adx_filter=True`（默认）时，用 **ADX（平均趋向指数）** 判断趋势强度：
`ADX < adx_threshold`（默认 20）视为无趋势/震荡市，该区间**强制空仓、不开趋势单**。
ADX 衡量趋势强度（非方向），与 Choppiness 互补：两者任一判定为无趋势/震荡即触发
空仓（`ranging = chop_ranging | adx_weak`）。指标见 `backend/app/factors/range_motion.py`。

### VPT 量价过滤

`use_vpt_filter=False`（默认，关闭以保持原行为）。启用后引入 **VPT（量价趋势）**：

$$
VPT_t = VPT_{t-1} + volume_t \cdot \frac{close_t - close_{t-1}}{close_{t-1}}, \quad VPT_0 = 0
$$

- `vpt_ok`：VPT 斜率（`VPT_t - VPT_{t-K}`）非负视为量价共振向上。
- **进场**：除斜率上穿阈值外，还需 `vpt_ok` 为 True。
- **离场**：除斜率下穿阈值外，持仓期间 VPT 斜率转负（量价背离）也触发离场。

未启用时 **不输出** VPT 相关叠加曲线（`vpt` / `vpt_slope` / `vpt_ok`），报告不画量价图。

> **报告按开关出图**：`use_chop_filter` / `use_adx_filter` / `use_vpt_filter` 关闭时，对应的
> 叠加曲线（`chop`/`chop_ranging`、`adx`/`adx_weak`、`vpt`/`vpt_slope`/`vpt_ok`）不会输出，
> HTML 报告即不绘制这些指标图，避免无效或恒值的曲线占用版面。`slope_fit_window<2` 时同样
> 不输出 `llt_r2` / `llt_fit_ok`。

## 3. 策略参数

| 参数 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `llt_period` | 20 | 2~400 | LLT 平滑周期；越大越平滑，信号越慢 |
| `slope_lookback` | 1 | 1~60 | 计算 LLT 斜率时回看的交易日数（仅 `slope_fit_window=1` 时的单点差分用） |
| `slope_fit_window` | 1 | 1~120 | LLT 斜率拟合窗口；≥2 时用滚动最小二乘拟合斜率（更平滑），1 时退化为单点差分 |
| `min_fit_r2` | 0.0 | 0~1 | 回归拟合质量（R²）阈值；仅 `slope_fit_window≥2` 生效，R² 低于此值放弃进场；0 表示不做该过滤 |
| `slope_threshold` | 0.0 | ≥0 | 斜率阈值（价格单位）；0 即过零逻辑 |
| `use_chop_filter` | True | bool | 是否启用 Choppiness 震荡市过滤 |
| `chop_period` | 14 | 2~400 | Choppiness Index 周期 |
| `chop_threshold` | 62.0 | 0~100 | CHOP 高于此值判定为震荡市 |
| `use_adx_filter` | True | bool | 是否启用 ADX 趋势强度过滤 |
| `adx_period` | 14 | 2~400 | ADX 周期 |
| `adx_threshold` | 20.0 | 0~100 | ADX 低于此值判定为无趋势/震荡市 |
| `use_vpt_filter` | False | bool | 是否启用 VPT 量价过滤（量价共振才持仓） |
| `vpt_slope_lookback` | 1 | 1~60 | 判断 VPT 斜率时回看的交易日数 |

最少 K 线数为 `max(llt_period, [chop_period], [adx_period], [vpt_slope_lookback]) + max(slope_lookback, slope_fit_window - 1) + 1`。

## 4. 运行示例

```bash
cd backend
# 默认逻辑（含 Choppiness 过滤，VPT 仅作叠加）
python -m app.script backtest --strategy my_strategy --mode screen --max-universe 20 --seed 42 --json
```

启用 VPT 量价过滤需通过请求 JSON 的 `strategy_params` 传入：

```json
{
  "mode": "universe",
  "strategy_id": "my_strategy",
  "universe": "zz500",
  "strategy_params": {
    "use_chop_filter": true,
    "use_vpt_filter": true,
    "vpt_slope_lookback": 3
  }
}
```

```bash
cd backend
python -m app.script backtest --request examples/batch_test/backtest_my_strategy.json
```

公共字段总表见 [策略总览](./strategy-guide.md)。
