"""区间震荡判断因子：ADX 与 Choppiness Index。

两者均基于 OHLC 数组，用于区分趋势与震荡行情：

- ``adx``（Average Directional Index）：衡量趋势强度，数值越高趋势越强
  （更不像震荡），低于阈值（如 20~25）通常视为无趋势/震荡；
- ``choppiness_index``（Choppiness Index）：衡量行情"碎"/震荡程度，
  数值越高（越接近 100）越震荡，越低（接近 0）越单边。

输入三数组（high / low / close）须等长，均为数组进数组出（np.ndarray）。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


def _validate_period(period: int) -> int:
    if isinstance(period, (bool, np.bool_)) or not isinstance(
        period, (int, np.integer)
    ):
        raise TypeError("period 必须是整数")
    if period < 1:
        raise ValueError("period 必须大于等于 1")
    return int(period)


def _as_ohlc(high, low, close) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把三个输入统一为一维 float ndarray，并校验等长。"""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if high.ndim != 1 or low.ndim != 1 or close.ndim != 1:
        raise ValueError("high/low/close 必须是一维序列")
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high/low/close 必须等长")
    return high, low, close


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """真振幅 TR = max(high-low, |high-prev_close|, |low-prev_close|)。"""
    hl = high - low
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    hc = np.abs(high - prev_close)
    lc = np.abs(low - prev_close)
    return np.maximum(np.maximum(hl, hc), lc)


def _rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder 平滑（Recursive Moving Average）：首个有效值为前 period 项均值，
    之后 rma_t = (rma_{t-1} * (period-1) + v_t) / period。
    输入含 NaN/Inf 时输出 NaN，并在下一段有效数据重新初始化。
    """
    source = np.asarray(values, dtype=float)
    out = np.full(source.shape, np.nan, dtype=float)
    if source.size < period:
        return out

    first_window = source[:period]
    if np.all(np.isfinite(first_window)):
        out[period - 1] = float(np.nanmean(first_window))
    running = out[period - 1]

    for i in range(period, source.size):
        v = source[i]
        if not np.isfinite(v):
            running = np.nan
            out[i] = np.nan
            continue
        if np.isfinite(running):
            running = (running * (period - 1) + v) / period
        else:
            running = v
        out[i] = running

    return out


def adx(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """计算平均趋向指数（Average Directional Index）。

    通过 +DI 与 -DI 的差值归一化得到 DX，再对 DX 做 Wilder 平滑得到 ADX。
    ADX 衡量趋势强度（非方向）：值越高趋势越强，越低越接近震荡。
    首个有效值约在第 ``2 * period - 2`` 个位置，其后为完整 ADX。
    """
    n = _validate_period(period)
    high, low, close = _as_ohlc(high, low, close)

    tr = _true_range(high, low, close)
    atr = _rma(tr, n)

    up = np.diff(high)
    down = -np.diff(low)  # down = low[i-1] - low[i]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    # 首项无前一根 K 线，置 NaN；后续经 _rma 首窗口均值为准
    plus_dm = np.concatenate(([np.nan], plus_dm))
    minus_dm = np.concatenate(([np.nan], minus_dm))

    plus_di = 100.0 * _rma(plus_dm, n) / atr
    minus_di = 100.0 * _rma(minus_dm, n) / atr

    di_sum = plus_di + minus_di
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, np.nan)

    return _rma(dx, n)


def choppiness_index(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """计算碎形指数（Choppiness Index）。

    公式 ``CHOP = 100 * log10(ΣTR_n / (HH_n - LL_n)) / log10(n)``，
    其中 ΣTR 为 n 期真振幅之和，HH/LL 为 n 期最高/最低。结果大致落在
    0~100：越接近 100 越震荡，越接近 0 越单边；HH 与 LL 重合或 ΣTR 为 0
    时输出 NaN。
    """
    n = _validate_period(period)
    high, low, close = _as_ohlc(high, low, close)

    tr_sum = pd.Series(_true_range(high, low, close)).rolling(n, min_periods=n).sum().to_numpy()
    hh = pd.Series(high).rolling(n, min_periods=n).max().to_numpy()
    ll = pd.Series(low).rolling(n, min_periods=n).min().to_numpy()
    rng = hh - ll

    with np.errstate(divide="ignore", invalid="ignore"):
        chop = np.where(
            (tr_sum > 0) & (rng > 0),
            100.0 * np.log10(tr_sum / rng) / np.log10(n),
            np.nan,
        )
    return chop
