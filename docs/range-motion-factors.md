# 区间震荡判断因子：ADX 与 Choppiness Index

本文档描述项目新增的两个用于**判断行情处于趋势还是震荡**的技术因子：`adx`（平均趋向指数）与 `choppiness_index`（碎形指数）。两者均基于单标的 OHLC 数组，数组进、数组出（`np.ndarray`），适合做行情环境过滤。

实现代码：`backend/app/factors/range_motion.py`  
导入方式：`from app.factors import adx, choppiness_index`

> ADX 衡量趋势**强度**（不区分方向），数值越高越像单边趋势、越不像震荡；Choppiness Index 衡量行情"碎"/震荡程度，数值越高（越接近 100）越震荡、越接近 0 越单边。两者思路相反但可互补：ADX 低时常常对应 CHOP 高。

## 1. 核心思想

两者都以 **True Range（真振幅 TR）** 与 **Wilder 平滑** 为基础，但侧重点不同。

### 1.1 公共基础：True Range 与 Wilder 平滑

```
TR_t = max( high_t - low_t,  |high_t - prev_close|,  |low_t - prev_close| )
```

- 首根 K 线无前一收盘，`TR` 为 NaN；
- 后续以 `_rma`（Wilder 递归平滑）累积：首个有效值为前 `period` 项均值，之后 `rma_t = (rma_{t-1} × (period-1) + v_t) / period`；
- 遇到 NaN/Inf 时输出 NaN，并在下一段有效数据重新初始化。

### 1.2 ADX：趋势强度

通过 `+DM` / `-DM`（方向动量）与 `ATR`（平滑 TR）构造 `+DI` / `-DI`，再对二者之差的归一化 `DX` 做第二次 Wilder 平滑：

```
+DM_t = high_t - high_{t-1}     （当且仅当向上移动大于向下移动且 > 0）
-DM_t = low_{t-1} - low_t       （当且仅当向下移动大于向上移动且 > 0）
+DI = 100 × rma(+DM) / ATR
-DI = 100 × rma(-DM) / ATR
DX  = 100 × |+DI - -DI| / (+DI + -DI)
ADX = rma(DX)
```

- `ADX` 只给**强度**，不给方向（方向看 `+DI`/`-DI` 谁大）；
- 首个有效值约在第 `period` 根之后；
- 常规判读：`ADX > 25` 有趋势，`ADX < 20` 多属震荡。

### 1.3 Choppiness Index：震荡程度

标准 Dreiss 公式，分子用 **n 期 TR 之和**，分母用 **n 期最高 − 最低**（同量纲，保证随机行情落在 0~100 中间地带）：

```
ΣTR   = sum(TR_t, 窗口 n 根)
Range = max(high, 窗口 n 根) - min(low, 窗口 n 根)
CHOP  = 100 × log10( ΣTR / Range ) / log10(n)
```

- 越接近 100 越震荡（趋势策略应回避）；越接近 0 越单边；
- `Range` 或 `ΣTR` 为 0 时输出 NaN；
- 分子若用单根 ATR 而非 n 期之和，随机行情会算出明显负值，故本实现采用求和口径。

## 2. 函数签名与参数

| 函数 | 签名 | 默认 `period` | 返回 |
| --- | --- | --- | --- |
| `adx` | `adx(high, low, close, period=14)` | `14` | 与输入等长的 `np.ndarray`（float），暖机期为 NaN |
| `choppiness_index` | `choppiness_index(high, low, close, period=14)` | `14` | 与输入等长的 `np.ndarray`（float），暖机期为 NaN |

三者均要求：

- `high` / `low` / `close` 为**一维**序列且**等长**，否则抛 `ValueError`；
- `period` 必须为**正整数**（非整数抛 `TypeError`，`< 1` 抛 `ValueError`）。

## 3. 用法示例

```python
from app.factors import adx, choppiness_index

high  = df["high"].astype(float).to_numpy()
low   = df["low"].astype(float).to_numpy()
close = df["close"].astype(float).to_numpy()

adx_v  = adx(high, low, close, period=14)      # 趋势强度，高=有趋势
chop_v = choppiness_index(high, low, close)    # 震荡程度，高=震荡市
```

配合 `numpy` 做环境过滤：

```python
import numpy as np

is_choppy = np.where(np.isfinite(chop_v), chop_v > 62, False)   # 震荡市掩码
```

## 4. 判读口径

| 信号 | CHOP | ADX | 含义 |
| --- | --- | --- | --- |
| 强单边趋势 | CHOP 低（< 40） | ADX 高（> 25） | 顺势交易为主 |
| 温和趋势 | CHOP 中低 | ADX 中（20~25） | 谨慎顺势 |
| 震荡/无趋势 | CHOP 高（> 62） | ADX 低（< 20） | 回避趋势策略，或高抛低吸 |

## 5. 在 my_strategy 中的应用

`my_strategy`（`backend/app/strategies/my_strategy.py`）用 `choppiness_index` 做**大环境过滤**：

- `use_chop_filter=True`（默认）时，用个股自身 OHLC 计算 `CHOP`；
- 当 `CHOP > chop_threshold`（默认 `62`）判定为**震荡市**，此时**强制空仓、不开趋势单**（已持仓也立即平仓）；
- 相关参数：`chop_period`（默认 `14`）、`chop_threshold`（默认 `62`）、`use_chop_filter`；
- `CHOP` 为 NaN 的暖机期不误判为震荡。

报告叠加曲线 `chop`（CHOP 值）与 `chop_ranging`（0/1 震荡市掩码）可用于可视化验证。

## 6. 适用场景与局限

- **适用**：行情环境判断、趋势/震荡切换过滤、与趋势策略（如多头排列 + LLT）组合。
- **局限**：
  - `period` 越长指标越平滑但越滞后；阈值（如 CHOP 62、ADX 20/25）是经验值，需按标的与周期调参。
  - ADX 只看强度不分方向，单独使用无法区分涨跌趋势，需配合 `+DI`/`-DI` 或方向类指标。
  - 两者都对 `period` 敏感，暖机期（前 `period` 根）为 NaN，短序列或无历史数据时无效。
  - 当前 `my_strategy` 用**个股自身** CHOP 做环境过滤；如需用**大盘指数**做全局过滤，需要额外接入指数数据管道。
