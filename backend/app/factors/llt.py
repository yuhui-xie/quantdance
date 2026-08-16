"""低延迟趋势线（Low-Lag Trendline, LLT）。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def llt(values: Sequence[float] | np.ndarray, period: int = 20) -> np.ndarray:
    """按经典二阶递推公式计算 LLT。

    返回值与输入等长。每段连续有效数据的前两项以原值初始化；遇到
    NaN/Inf 时输出 NaN，并在下一段有效数据重新初始化。
    """
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)):
        raise TypeError("period 必须是整数")
    if period < 2:
        raise ValueError("period 必须大于等于 2")

    source = np.asarray(values, dtype=float)
    if source.ndim != 1:
        raise ValueError("values 必须是一维序列")

    result = np.full(source.shape, np.nan, dtype=float)
    if source.size == 0:
        return result

    alpha = 2.0 / (period + 1.0)
    alpha_sq = alpha * alpha
    price_0_weight = alpha - alpha_sq / 4.0
    price_1_weight = alpha_sq / 2.0
    price_2_weight = alpha - 3.0 * alpha_sq / 4.0
    trend_1_weight = 2.0 * (1.0 - alpha)
    trend_2_weight = (1.0 - alpha) ** 2

    valid_run = 0
    for i, price in enumerate(source):
        if not np.isfinite(price):
            valid_run = 0
            continue
        if valid_run < 2:
            result[i] = price
        else:
            result[i] = (
                price_0_weight * source[i]
                + price_1_weight * source[i - 1]
                - price_2_weight * source[i - 2]
                + trend_1_weight * result[i - 1]
                - trend_2_weight * result[i - 2]
            )
        valid_run += 1

    return result


def llt_slope(trend: np.ndarray, lookback: int = 1) -> np.ndarray:
    """LLT 斜率（差分）D_t = L_t - L_{t-K}。

    基于 LLT 趋势线的 K 期差分，用于刻画趋势方向与拐点；前 K 项为 NaN。
    返回值与 ``trend`` 等长。``lookback`` 必须为正整数。
    """
    if isinstance(lookback, bool) or not isinstance(lookback, (int, np.integer)):
        raise TypeError("lookback 必须是整数")
    if lookback < 1:
        raise ValueError("lookback 必须大于等于 1")

    source = np.asarray(trend, dtype=float)
    if source.ndim != 1:
        raise ValueError("trend 必须是一维序列")

    K = int(lookback)
    dt = np.full(source.shape, np.nan, dtype=float)
    if source.size > K:
        dt[K:] = source[K:] - source[:-K]
    return dt
