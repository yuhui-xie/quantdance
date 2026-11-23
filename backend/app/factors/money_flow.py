"""资金流指标：MFI（Money Flow Index，资金流量指数）。

MFI 把成交量加权后的典型价格方向累积，得到 0~100 的"带量的 RSI"：

- 典型价 ``TP = (high + low + close) / 3``，资金流 ``raw = TP × volume``；
- ``TP`` 较前一日上升计入正资金流、下降计入负资金流、持平不计数；
- ``MFI = 100 − 100 / (1 + Σ正资金流_n / Σ负资金流_n)``。

数值越高越接近超买（经典 80），越低越接近超卖（经典 20）。负资金流窗口为 0
时输出 100，正资金流窗口为 0 时输出 0；两者同时为 0（窗口内价格完全走平）时
输出 50。窗口不足或数据含 NaN 时输出 NaN。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from app.factors.base import FactorSpec


def money_flow_index(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    volume: Sequence[float] | np.ndarray,
    period: int = 7,
) -> np.ndarray:
    """计算资金流量指数（MFI），返回与输入等长的 float 数组。

    输入四个数组须等长。首个有效值出现在第 ``period`` 个位置（下标从 0 计），
    其前为 NaN 暖机区。``period`` 须为正整数。
    """
    if isinstance(period, (bool, np.bool_)) or not isinstance(period, (int, np.integer)):
        raise TypeError("period 必须是整数")
    n = int(period)
    if n < 1:
        raise ValueError("period 必须大于等于 1")

    high_a = np.asarray(high, dtype=float)
    low_a = np.asarray(low, dtype=float)
    close_a = np.asarray(close, dtype=float)
    volume_a = np.asarray(volume, dtype=float)
    sizes = {len(high_a), len(low_a), len(close_a), len(volume_a)}
    if len(sizes) != 1:
        raise ValueError("high/low/close/volume 必须等长")

    result = np.full(close_a.shape, np.nan, dtype=float)
    if high_a.size <= n:
        return result

    tp = (high_a + low_a + close_a) / 3.0
    raw = tp * volume_a
    prev_tp = np.concatenate(([np.nan], tp[:-1]))
    valid = (
        np.isfinite(tp)
        & np.isfinite(prev_tp)
        & np.isfinite(raw)
    )
    # 有效日内：上升计正资金流、下降计负资金流、持平日两者都计 0（不能留 NaN，
    # 否则任意一个下跌日都会让整个滚动窗口作废）。仅前值缺失日保持 NaN 暖机。
    pos = np.where(valid & (tp > prev_tp), raw, 0.0)
    neg = np.where(valid & (tp < prev_tp), raw, 0.0)
    pos = np.where(valid, pos, np.nan)
    neg = np.where(valid, neg, np.nan)

    pos_sum = (
        pd.Series(pos).rolling(n, min_periods=n).sum().to_numpy()
    )
    neg_sum = (
        pd.Series(neg).rolling(n, min_periods=n).sum().to_numpy()
    )
    ratio = np.divide(
        pos_sum,
        neg_sum,
        out=np.full(close_a.shape, np.nan, dtype=float),
        where=neg_sum > 0,
    )
    mfi = 100.0 - 100.0 / (1.0 + ratio)
    # 边界口径：窗口内只有正资金流 → 100；只有负资金流 → 0；完全走平 → 50。
    mfi = np.where((neg_sum == 0) & (pos_sum > 0), 100.0, mfi)
    mfi = np.where((pos_sum == 0) & (neg_sum > 0), 0.0, mfi)
    mfi = np.where((pos_sum == 0) & (neg_sum == 0), 50.0, mfi)
    valid_window = np.isfinite(pos_sum) & np.isfinite(neg_sum)
    result[valid_window] = mfi[valid_window]
    return result


def mfi_factor(df: pd.DataFrame, period: int = 7) -> pd.Series:
    """MFI 因子，返回与 df 索引对齐的 Series。"""
    return pd.Series(
        money_flow_index(
            df["high"].astype(float).to_numpy(),
            df["low"].astype(float).to_numpy(),
            df["close"].astype(float).to_numpy(),
            df["volume"].astype(float).to_numpy(),
            period=period,
        ),
        index=df.index,
    )


# 本模块导出的因子注册（供 app/factors/registry.py 自动汇总，来源默认 strategy）
FACTORS: list[FactorSpec] = [
    FactorSpec("mfi", mfi_factor, min_bars=8),
]
