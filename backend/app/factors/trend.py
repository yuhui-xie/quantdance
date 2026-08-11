"""趋势/均线辅助：末尾 N 根均线等。"""

from __future__ import annotations

import numpy as np


def sma_last(closes: np.ndarray, window: int) -> float | None:
    """末尾 window 根 K 线的均值；不足或含 NaN 返回 None。"""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < window:
        return None
    chunk = closes[-window:]
    if not np.all(np.isfinite(chunk)):
        return None
    return float(chunk.mean())


def trend_ma(closes: np.ndarray, window: int) -> float | None:
    """趋势均线：末尾 window 根收盘均值，语义化别名（= sma_last）。"""
    return sma_last(closes, window)
