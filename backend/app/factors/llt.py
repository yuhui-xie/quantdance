"""低延迟趋势线（Low-Lag Trendline, LLT）。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from app.factors.base import FactorSpec


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


def llt_slope_reg(trend: np.ndarray, window: int = 5) -> np.ndarray:
    """基于 LLT 的滚动最小二乘线性回归斜率（便捷版，仅返回斜率）。

    对最近 ``window`` 根 K 线的 LLT 趋势线做最小二乘线性拟合，取拟合斜率
    作为该时点的趋势斜率。相比 ``llt_slope`` 的单点差分，回归斜率用整段
    窗口刻画趋势、对噪音更稳健，变化更平滑，适合刻画较慢的中期趋势；
    ``window`` 越大斜率越平滑（但对拐点的响应也越慢）。

    如需同时获得拟合质量（R²），用 :func:`llt_slope_fit`。
    """
    slope, _r2 = llt_slope_fit(trend, window)
    return slope


def llt_slope_fit(trend: np.ndarray, window: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """滚动最小二乘线性回归，返回 ``(斜率, R²)``。

    对最近 ``window`` 根 K 线的 LLT 趋势线做最小二乘线性拟合。``R²`` 刻画
    拟合质量（趋势是否贴近一条直线）：越接近 1 拟合越好、斜率越可信；接近 0
    表示窗口内涨跌混乱、拟合方差大，此时斜率不可信，可用于过滤（放弃进场）。

    两个返回值都与 ``trend`` 等长，前 ``window-1`` 项为 NaN。R² 对完全平坦
    的窗口（无方差）取 1.0。``window`` 必须大于等于 2。
    """
    if isinstance(window, bool) or not isinstance(window, (int, np.integer)):
        raise TypeError("window 必须是整数")
    if window < 2:
        raise ValueError("window 必须大于等于 2")

    source = np.asarray(trend, dtype=float)
    if source.ndim != 1:
        raise ValueError("trend 必须是一维序列")

    n = source.size
    slope = np.full(n, np.nan, dtype=float)
    r2 = np.full(n, np.nan, dtype=float)
    if n < window:
        return slope, r2

    # 最小二乘斜率 = sum((x-mean)*y) / sum((x-mean)^2)，x 取窗口内序号 0..window-1
    x_centered = np.arange(window, dtype=float) - (window - 1) / 2.0
    sxx = float((x_centered**2).sum())  # = window*(window-1)*(window+1)/12
    if sxx == 0:
        return slope, r2

    # num[t] = sum_k x_centered[k] * trend[t-window+1+k]（k=0..window-1）
    kernel = x_centered[::-1]
    num = np.convolve(source, kernel, mode="valid")
    sl = num / sxx

    # 滚动总离差平方和 ss_tot = sum(y-mean)^2 = sum(y^2) - window*mean^2
    ones = np.ones(window, dtype=float)
    sum_y2 = np.convolve(source**2, ones, mode="valid")
    sum_y = np.convolve(source, ones, mode="valid")
    ss_tot = sum_y2 - sum_y**2 / float(window)

    # R² = num^2 / (sxx * ss_tot)（即相关系数平方）；无方差时定义 R²=1
    r2_slice = np.divide(
        num**2,
        sxx * ss_tot,
        out=np.ones_like(num),
        where=ss_tot > 0,
    )
    r2_slice = np.clip(r2_slice, 0.0, 1.0)

    start = window - 1
    slope[start:] = sl
    r2[start:] = r2_slice
    return slope, r2


# ---------------------------------------------------------------------------
# DataFrame → Series 因子 handler（供 IC 分析复用）
# ---------------------------------------------------------------------------

def llt_factor(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """LLT 趋势线原始值，返回与 df 索引对齐的 Series。

    注意：趋势线是平滑价格，横截面量纲随价格水平而异，跨股票比较意义有限；
    要评价趋势方向建议用 :func:`llt_slope_factor`。
    """
    close = df["close"].astype(float).to_numpy()
    return pd.Series(llt(close, period=period), index=df.index)


def llt_slope_factor(df: pd.DataFrame, period: int = 20, lookback: int = 1) -> pd.Series:
    """LLT 趋势线斜率（period=20，单点差分），返回与 df 索引对齐的 Series。"""
    close = df["close"].astype(float).to_numpy()
    return pd.Series(
        llt_slope(llt(close, period=period), lookback=lookback), index=df.index
    )


# 本模块导出的因子注册（供 app/factors/registry.py 自动汇总）
FACTORS: list[FactorSpec] = [
    FactorSpec("llt", llt_factor, min_bars=21, source="strategy"),
    FactorSpec("llt_slope", llt_slope_factor, min_bars=21, source="strategy"),
]
