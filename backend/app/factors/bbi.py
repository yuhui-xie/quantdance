"""多空指标（Bull and Bear Index, BBI）。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


def bbi(
    values: Sequence[float] | np.ndarray,
    periods: Sequence[int] = (3, 6, 12, 24),
) -> np.ndarray:
    """计算多条简单移动平均线的等权平均值。

    默认采用经典公式 ``(MA3 + MA6 + MA12 + MA24) / 4``。在最长周期
    尚未形成，或任一均线窗口包含 NaN/Inf 时，结果为 NaN。
    """
    source = np.asarray(values, dtype=float)
    if source.ndim != 1:
        raise ValueError("values 必须是一维序列")

    normalized = tuple(periods)
    if not normalized:
        raise ValueError("periods 不能为空")
    if any(
        isinstance(period, (bool, np.bool_))
        or not isinstance(period, (int, np.integer))
        for period in normalized
    ):
        raise TypeError("periods 必须全部为整数")
    if any(period < 1 for period in normalized):
        raise ValueError("periods 必须全部大于等于 1")

    clean = source.copy()
    clean[~np.isfinite(clean)] = np.nan
    series = pd.Series(clean)
    moving_averages = [
        series.rolling(window=int(period), min_periods=int(period)).mean().to_numpy()
        for period in normalized
    ]
    return np.mean(np.vstack(moving_averages), axis=0)
