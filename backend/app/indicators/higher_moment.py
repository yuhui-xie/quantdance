"""收益率滚动标准化中心矩。"""

from __future__ import annotations

import numpy as np


def rolling_standardized_moment(
    close: np.ndarray,
    *,
    order: int = 5,
    window: int = 20,
) -> np.ndarray:
    """计算收盘价对数收益率的滚动标准化中心矩。"""
    prices = np.asarray(close, dtype=float)
    out = np.full(len(prices), np.nan, dtype=float)
    if len(prices) < 2:
        return out

    returns = np.full(len(prices), np.nan, dtype=float)
    valid_prices = (prices[1:] > 0) & (prices[:-1] > 0)
    returns[1:][valid_prices] = np.log(
        prices[1:][valid_prices] / prices[:-1][valid_prices]
    )

    for end in range(window, len(prices)):
        values = returns[end - window + 1 : end + 1]
        if len(values) != window or not np.isfinite(values).all():
            continue
        centered = values - float(np.mean(values))
        scale = float(np.std(values, ddof=0))
        if scale <= 1e-12:
            continue
        out[end] = float(np.mean((centered / scale) ** order))
    return out


def ema_alpha(values: np.ndarray, alpha: float) -> np.ndarray:
    """以 alpha 形式计算 EMA；首个有限值作为初值。"""
    source = np.asarray(values, dtype=float)
    out = np.full(len(source), np.nan, dtype=float)
    state = np.nan
    for i, value in enumerate(source):
        if not np.isfinite(value):
            continue
        state = value if not np.isfinite(state) else alpha * value + (1.0 - alpha) * state
        out[i] = state
    return out
