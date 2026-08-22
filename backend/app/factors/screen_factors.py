"""选股技术因子的权威公式（整条序列向量化）：供 stock_screening 打分与 ic_analysis 复用。

统一归置选股技术面因子：每个函数接收单只股票的日线 DataFrame（DatetimeIndex，含
open/high/low/close[/amount]），返回与 df 索引对齐的因子时间序列 Series（含暖机期 NaN）。

两个消费方共用此公式，保证「IC 因子 == screen 因子」：

- ``app/stock_screening.py`` 的 ``compute_*_breakdown`` 取序列末值 ``.iloc[-1]`` 作为末根
  K 线标量（末值与历史实现逐位一致，由测试锁定）；
- ``app/ic_analysis.py`` 直接注册这些序列函数评价整条因子。

数据列约定：``close/volume`` 需为数值；``amount`` 存在且非空时优先用于量能，否则用
``volume``（与 ``compute_volume_pulse_breakdown`` 一致）。
"""

from __future__ import annotations

import pandas as pd

from app.factors.base import FactorSpec


def activity_series(df: pd.DataFrame) -> pd.Series:
    """量能：成交额优先，否则成交量（与 compute_volume_pulse_breakdown 一致）。"""
    if "amount" in df.columns:
        amount = pd.to_numeric(df["amount"], errors="coerce")
        if amount.notna().any() and float(amount.fillna(0).abs().sum()) > 0:
            return amount.astype(float)
    return df["volume"].astype(float)


def return_factor(df: pd.DataFrame, horizon: int) -> pd.Series:
    """区间收益：close[t] / close[t-horizon] - 1。"""
    return df["close"].astype(float) / df["close"].astype(float).shift(horizon) - 1.0


def volatility_factor(df: pd.DataFrame, window: int) -> pd.Series:
    """滚动波动率：近 window 日日收益标准差。"""
    return df["close"].astype(float).pct_change().rolling(window).std()


def volume_ratio_factor(df: pd.DataFrame) -> pd.Series:
    """量比：activity[t] / mean(activity[t-20..t-1])（排除当日，成交额优先）。"""
    activity = activity_series(df)
    denom = activity.shift(1).rolling(20).mean()
    return activity / denom


def price_vs_ma5_factor(df: pd.DataFrame) -> pd.Series:
    """相对短期均线位置：close / MA5 - 1。"""
    close = df["close"].astype(float)
    return close / close.rolling(5).mean() - 1.0


def ma_spread_factor(df: pd.DataFrame) -> pd.Series:
    """均线价差：(MA5 - MA20) / MA20。"""
    close = df["close"].astype(float)
    s5 = close.rolling(5).mean()
    s20 = close.rolling(20).mean()
    return (s5 - s20) / s20


def sma5_slope_factor(df: pd.DataFrame) -> pd.Series:
    """短期均线斜率：(MA5[t] - MA5[t-3]) / |MA5[t-3]|（近零分母→1.0）。"""
    sma5 = df["close"].astype(float).rolling(5).mean()
    denom = sma5.shift(3).abs()
    # 近零分母→1.0（与 compute_ma_alignment_breakdown 的 denom 守卫一致）
    denom = denom.where(denom > 1e-12, 1.0)
    return sma5.diff(3) / denom


def relative_volume_factor(df: pd.DataFrame) -> pd.Series:
    """相对成交量：volume[t] / mean(volume[t-19..t])（含当日）。"""
    vol = df["volume"].astype(float)
    return vol / vol.rolling(20).mean()


def amihud_illiquidity_factor(df: pd.DataFrame) -> pd.Series:
    """Amihud 风格非流动性：mean(|日收益| / (volume+1e-6), 近 20 日)。"""
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    ret = close.pct_change()
    return (ret.abs() / (vol + 1e-6)).rolling(20).mean()


# 本模块导出的因子注册（供 app/factors/registry.py 自动汇总）。暖机 K 线与
# stock_screening.TECHNICAL_FACTOR_MIN_BARS 保持一致。
FACTORS: list[FactorSpec] = [
    FactorSpec("ret_20", lambda df: return_factor(df, 20), min_bars=61),
    FactorSpec("ret_60", lambda df: return_factor(df, 60), min_bars=61),
    FactorSpec("ret_5", lambda df: return_factor(df, 5), min_bars=12),
    FactorSpec("ret_10", lambda df: return_factor(df, 10), min_bars=12),
    FactorSpec("vol_20", lambda df: volatility_factor(df, 20), min_bars=61),
    FactorSpec("vol_60", lambda df: volatility_factor(df, 60), min_bars=61),
    FactorSpec("vol_ratio", volume_ratio_factor, min_bars=25),
    FactorSpec("price_vs_ma5", price_vs_ma5_factor, min_bars=25),
    FactorSpec("ma_spread", ma_spread_factor, min_bars=25),
    FactorSpec("sma5_slope", sma5_slope_factor, min_bars=25),
    FactorSpec("rel_vol", relative_volume_factor, min_bars=21),
    FactorSpec("amihud_illiq_20", amihud_illiquidity_factor, min_bars=21),
]
