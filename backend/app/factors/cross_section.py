"""横截面归一化因子：跨标的标准化与百分位排名。"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def zscore(values: Sequence[float]) -> list[float]:
    """横截面 Z-Score 标准化：均值为 0、标准差为 1。样本不足或方差为 0 时返回全 0。"""
    arr = np.asarray(list(values), dtype=float)
    if len(arr) < 2:
        return [0.0] * len(arr)
    std = float(arr.std())
    if std == 0 or not np.isfinite(std):
        return [0.0] * len(arr)
    mean = float(arr.mean())
    return [float((v - mean) / std) for v in arr]


def percentile_ranks(values: Sequence[float]) -> list[float]:
    """返回每个值在列表中的 [0, 1] 百分位排名（值越小排名越低）。"""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    arr = np.asarray(list(values), dtype=float)
    order = np.argsort(arr)
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(n, dtype=float) / (n - 1)
    return ranks.tolist()
