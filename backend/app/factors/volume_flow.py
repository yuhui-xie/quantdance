"""量价指标：OBV（能量潮）与 VPT（量价趋势）。

两个指标均基于收盘价相对前一日的变化方向/幅度与成交量累积：
- ``obv``：收盘上涨 +volume、下跌 -volume、平盘不变，逐日累积；
- ``vpt``：按成交量加权的价格变化率逐日累积。

遇到 NaN/Inf 时输出 NaN，并在下一段有效数据重新初始化累积状态。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from app.factors.base import FactorSpec
from app.factors.llt import llt_slope


def _validate_arrays(
    close: Sequence[float] | np.ndarray,
    volume: Sequence[float] | np.ndarray,
) -> np.ndarray:
    c = np.asarray(close, dtype=float)
    v = np.asarray(volume, dtype=float)
    if c.ndim != 1 or v.ndim != 1:
        raise ValueError("close 与 volume 必须是一维序列")
    if c.size != v.size:
        raise ValueError("close 与 volume 长度必须一致")
    return v


def obv(close: Sequence[float] | np.ndarray, volume: Sequence[float] | np.ndarray) -> np.ndarray:
    """On-Balance Volume 能量潮。

    收盘价相对前一日上涨则加上当日成交量，下跌则减去，平盘不变；
    逐日累积，首日 OBV 为 0。返回值与输入等长。
    """
    c = np.asarray(close, dtype=float)
    v = _validate_arrays(close, volume)

    result = np.full(c.shape, np.nan, dtype=float)
    acc = 0.0
    last_close = np.nan
    for i in range(c.size):
        if not np.isfinite(c[i]) or not np.isfinite(v[i]):
            result[i] = np.nan
            acc = 0.0
            last_close = np.nan
            continue
        if np.isfinite(last_close):
            if c[i] > last_close:
                acc += v[i]
            elif c[i] < last_close:
                acc -= v[i]
        result[i] = acc
        last_close = c[i]
    return result


def vpt(close: Sequence[float] | np.ndarray, volume: Sequence[float] | np.ndarray) -> np.ndarray:
    """Volume Price Trend 量价趋势。

    ``VPT_t = VPT_{t-1} + volume_t * (close_t - close_{t-1}) / close_{t-1}``，
    首日 VPT 为 0。返回值与输入等长。
    """
    c = np.asarray(close, dtype=float)
    v = _validate_arrays(close, volume)

    result = np.full(c.shape, np.nan, dtype=float)
    acc = 0.0
    last_close = np.nan
    for i in range(c.size):
        if not np.isfinite(c[i]) or not np.isfinite(v[i]):
            result[i] = np.nan
            acc = 0.0
            last_close = np.nan
            continue
        if np.isfinite(last_close) and last_close != 0:
            acc += v[i] * (c[i] - last_close) / last_close
        result[i] = acc
        last_close = c[i]
    return result


# ---------------------------------------------------------------------------
# DataFrame → Series 因子 handler（供 IC 分析复用）
# ---------------------------------------------------------------------------

def _volume_arr(df: pd.DataFrame) -> np.ndarray:
    """取数值化的成交量序列（含 NaN 暖机容错，缺失补 0）。"""
    return pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(float).to_numpy()


def vpt_factor(df: pd.DataFrame) -> pd.Series:
    """VPT 量价趋势原始值，返回与 df 索引对齐的 Series。

    注意：VPT 是逐日累积的量纲量，横截面量纲因成交量规模而异，跨股票比较
    意义有限；要评价量价方向建议用 :func:`vpt_slope_factor`。
    """
    close = df["close"].astype(float).to_numpy()
    return pd.Series(vpt(close, _volume_arr(df)), index=df.index)


def vpt_slope_factor(df: pd.DataFrame) -> pd.Series:
    """VPT 量价趋势斜率（单点差分），返回与 df 索引对齐的 Series。"""
    close = df["close"].astype(float).to_numpy()
    return pd.Series(llt_slope(vpt(close, _volume_arr(df)), lookback=1), index=df.index)


# 本模块导出的因子注册（供 app/factors/registry.py 自动汇总，来源默认 strategy）
FACTORS: list[FactorSpec] = [
    FactorSpec("vpt", vpt_factor, min_bars=2),
    FactorSpec("vpt_slope", vpt_slope_factor, min_bars=2),
]
