"""多头排列因子（均线多头排列强度）。

经典技术面“多头排列”指短周期均线在上、中周期居中、长周期在下，
即短 > 中 > 长，价格整体处于上升趋势。该因子把这种排列量化为 [0, 1]
的强度分数：每条相邻均线对按周期长短升序是否严格满足“短均线 > 长均线”，
分数 = 满足的对数 / 相邻对数。分数为 1 表示完全多头排列，0 表示完全空头
排列（长 > 中 > 短）。

用于组合策略时通常取阈值（如 1.0 表示全部对齐，或 0.5 表示多数对齐）转
换为持仓状态。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


def bullish_alignment(
    closes: Sequence[float] | np.ndarray,
    periods: Sequence[int] = (5, 10, 20),
) -> np.ndarray:
    """计算均线多头排列强度，返回与 ``closes`` 等长的分数数组。

    对每个交易日，按周期长度升序排列均线（最短在上），分数为满足
    “短均线 > 长均线”的相邻对比例。最长周期尚未形成、或任一参与均线
    窗口含 NaN/Inf 时结果为 NaN。
    """
    source = np.asarray(closes, dtype=float)
    if source.ndim != 1:
        raise ValueError("closes 必须是一维序列")

    normalized = tuple(periods)
    if len(normalized) < 2:
        raise ValueError("periods 至少需要两个周期")
    if any(
        isinstance(period, (bool, np.bool_))
        or not isinstance(period, (int, np.integer))
        for period in normalized
    ):
        raise TypeError("periods 必须全部为整数")
    if any(period < 1 for period in normalized):
        raise ValueError("periods 必须全部大于等于 1")

    ordered = tuple(sorted(normalized))
    clean = source.copy()
    clean[~np.isfinite(clean)] = np.nan
    series = pd.Series(clean)
    moving_averages = [
        series.rolling(window=int(period), min_periods=int(period)).mean().to_numpy()
        for period in ordered
    ]
    stack = np.vstack(moving_averages)

    # 按周期升序，短均线应大于长均线；逐对比较后按列求比例。
    ascending = stack[:-1] > stack[1:]
    score = ascending.mean(axis=0)
    # 任一均线窗口含 NaN/Inf 时分数未定义。
    score[~np.all(np.isfinite(stack), axis=0)] = np.nan
    return score
