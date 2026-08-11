"""动量族因子与公共价格历史：跨横截面策略复用。

集中 3factor / etf_rotation / cyclical_rotation / prosperity_resonance 等
策略反复出现的动量计算与价格历史提取，避免每策略私有实现一份。

- ``linreg``：最小二乘线性回归（斜率, R²）；
- ``price_history``：取 asof 及之前的日线历史（保留存在的 OHLC 列），排序去重；
- ``bias_momentum`` / ``slope_momentum`` / ``efficiency_momentum``：三因子动量；
- ``simple_momentum``：简单区间动量 close/base - 1。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


def linreg(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """最小二乘线性回归，返回 (斜率, R²)。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x_mean = float(x.mean())
    y_mean = float(y.mean())
    sxx = float(((x - x_mean) ** 2).sum())
    if sxx == 0:
        return 0.0, 0.0
    slope = float(((x - x_mean) * (y - y_mean)).sum() / sxx)
    ss_tot = float(((y - y_mean) ** 2).sum())
    if ss_tot == 0:
        return slope, 0.0
    ss_res = float(((y - (slope * (x - x_mean) + y_mean)) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot
    return slope, r_squared


def price_history(value_df: pd.DataFrame, asof: str) -> pd.DataFrame:
    """取 asof 及之前的日线历史（保留存在的 OHLC 列供效率因子用），按日期排序去重。

    仅依赖 ``value_df`` 中存在 date/open/high/low/close 列的子集；只读 close 的策略照用。
    """
    cols = [c for c in ("date", "open", "high", "low", "close") if c in value_df.columns]
    if "date" not in cols:
        return pd.DataFrame()
    history = value_df.loc[
        value_df["date"].astype(str).str[:10] <= str(asof)[:10],
        cols,
    ].copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    for c in ("open", "high", "low", "close"):
        if c in history.columns:
            history[c] = pd.to_numeric(history[c], errors="coerce")
    history = history.dropna(subset=["date", "close"])
    history = history[history["close"] > 0]
    return history.sort_values("date").drop_duplicates("date", keep="last")


def simple_momentum(closes: np.ndarray, lookback: int) -> float | None:
    """简单区间动量：closes[-1] / closes[-lookback-1] - 1。历史不足返回 None。"""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < lookback + 1:
        return None
    base = closes[-lookback - 1]
    if not np.isfinite(base) or base <= 0:
        return None
    last = closes[-1]
    if not np.isfinite(last):
        return None
    return float(last / base - 1.0)


def bias_momentum(
    closes: np.ndarray,
    ma_days: int,
    momentum_days: int,
) -> float | None:
    """乖离动量因子：价格相对长期均线的偏离程度与趋势方向。"""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < ma_days + momentum_days:
        return None
    series = pd.Series(closes)
    bias = (series / series.rolling(ma_days, min_periods=1).mean()).values
    recent = bias[-momentum_days:]
    y = recent / recent[0]
    x = np.arange(momentum_days)
    slope, _ = linreg(x, y)
    return slope * 10000.0


def slope_momentum(closes: np.ndarray, slope_days: int) -> float | None:
    """斜率动量因子：归一化价格的回归斜率 × R²，衡量趋势强度与质量。"""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < slope_days:
        return None
    window = closes[-slope_days:]
    normalized = window / window[0]
    x = np.arange(1, slope_days + 1)
    slope, r_squared = linreg(x, normalized)
    return 10000.0 * slope * r_squared


def efficiency_momentum(
    history: pd.DataFrame,
    efficiency_days: int,
) -> float | None:
    """效率动量因子：价格中枢对数动量 × 效率系数（净移动距离/总移动距离）。"""
    if len(history) < efficiency_days:
        return None
    if not {"open", "high", "low"}.issubset(history.columns):
        return None
    pivot = (
        history["open"] + history["high"] + history["low"] + history["close"]
    ) / 4.0
    pivot = pivot.astype(float).tail(efficiency_days)
    pivot = pivot[pivot > 0]
    if len(pivot) < 2:
        return None
    log_p = np.log(pivot.values)
    momentum = 100.0 * (log_p[-1] - log_p[0])
    direction = abs(log_p[-1] - log_p[0])
    volatility = float(np.abs(np.diff(log_p)).sum())
    efficiency_ratio = direction / volatility if volatility > 0 else 0.0
    return momentum * efficiency_ratio
