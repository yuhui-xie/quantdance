"""limit_up_pullback_lowbuy 涨停回调低吸策略测试。

覆盖：缩量十字星回踩均线设置日识别；放量收阳买点触发；止损/下次放量/时间
兜底三种离场；min_bars。全部内存构造，不访问网络。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.base import BaseBacktestParams
from app.strategies.limit_up_pullback_lowbuy import (
    LimitUpPullbackLowbuyParams,
    STRATEGY,
    _is_volume_expansion,
    _qualifying_setup,
)


def _params(**overrides) -> LimitUpPullbackLowbuyParams:
    defaults = dict(
        limit_pct=9.5,
        limit_lookback=8,
        ma_period=5,
        support_tolerance=0.02,
        doji_body_ratio=0.30,
        low_vol_ratio=0.65,
        confirm_vol_ratio=1.2,
        vol_ma_period=3,
        stop_loss_pct=0.05,
        max_hold_days=3,
    )
    defaults.update(overrides)
    return LimitUpPullbackLowbuyParams(**defaults)


def _df(
    close: np.ndarray,
    open_a: np.ndarray | None = None,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    volume: np.ndarray | None = None,
) -> pd.DataFrame:
    n = len(close)
    c = np.asarray(close, dtype=float)
    o = c.copy() if open_a is None else np.asarray(open_a, dtype=float)
    h = c * 1.01 if high is None else np.asarray(high, dtype=float)
    lo = c * 0.99 if low is None else np.asarray(low, dtype=float)
    v = np.full(n, 100.0) if volume is None else np.asarray(volume, dtype=float)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": o, "high": h, "low": lo, "close": c, "volume": v}, index=idx
    )


def _base():
    return BaseBacktestParams(initial_cash=100000.0, commission=0.0003)


def _scenario_close():
    """标准场景：索引 7 涨停→索引 12 缩量十字星回踩均线（设置日）→索引 13 放量收阳。

    注意索引 13 收盘(10.6)高于开盘，故 open[13] 需单独覆盖。
    """
    close = np.array(
        [10.0, 10.0, 10.0, 10.0, 10.0, 10.2, 10.4, 11.5,
         10.3, 10.3, 10.3, 10.3, 10.3, 10.6],
        dtype=float,
    )
    return close


def _scenario_open():
    n = 14
    o = np.full(n, 10.0, dtype=float)
    o[12] = 10.30  # 设置日十字星：open≈close（close[12]=10.3）
    o[13] = 10.35  # 确认日收阳：close(10.6) > open(10.35)
    return o


def _scenario_high_low():
    n = 14
    close = _scenario_close()
    hi = close * 1.01
    lo = close * 0.99
    # 设置日十字星：open≈close、上下影明显、low 触及均线
    hi[12] = 10.50
    lo[12] = 10.25
    return hi, lo


def _scenario_volume():
    n = 14
    v = np.full(n, 100.0)
    v[12] = 30.0   # 缩量十字星
    v[13] = 200.0  # 放量收阳确认
    return v


def _scenario_df() -> pd.DataFrame:
    close = _scenario_close()
    hi, lo = _scenario_high_low()
    return _df(close, _scenario_open(), hi, lo, _scenario_volume())


def _setup_flags(df: pd.DataFrame, params=None):
    params = params or _params()
    c = df["close"].astype(float).to_numpy()
    o = df["open"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    lo = df["low"].astype(float).to_numpy()
    v = df["volume"].astype(float).to_numpy()
    n = len(c)
    pct = np.full(n, np.nan)
    pct[1:] = c[1:] / c[:-1] - 1.0
    ma = pd.Series(c).rolling(params.ma_period).mean().to_numpy()
    vol_ma = pd.Series(v).rolling(params.vol_ma_period).mean().to_numpy()
    return _qualifying_setup(c, o, h, lo, v, pct, ma, vol_ma, params)


# --- 设置日识别 ---


def test_setup_day_detected_after_limit_pullback_doji():
    df = _scenario_df()
    qual = _setup_flags(df)
    assert bool(qual[12])


def test_no_setup_without_recent_limit_up():
    df = _scenario_df()
    c = df["close"].astype(float).to_numpy().copy()
    # 去掉涨停：把 7 的涨停压回普通涨幅
    c[7] = 10.45
    hi, lo = _scenario_high_low()
    df2 = _df(c, _scenario_open(), hi, lo, _scenario_volume())
    qual = _setup_flags(df2)
    # 仍可能因其它条件触发？无涨停则索引 12 不应是设置日
    assert not bool(qual[12])


def test_no_setup_when_volume_not_shrink():
    df = _scenario_df()
    v = _scenario_volume().copy()
    v[12] = 100.0  # 非极度缩量
    df2 = _df(_scenario_close(), _scenario_open(), *_scenario_high_low(), v)
    qual = _setup_flags(df2)
    assert not bool(qual[12])


# --- 放量判定 ---


def test_volume_expansion_flag():
    df = _scenario_df()
    params = _params()
    v = df["volume"].astype(float).to_numpy()
    vol_ma = pd.Series(v).rolling(params.vol_ma_period).mean().to_numpy()
    # 确认日 13 放量
    assert _is_volume_expansion(v, vol_ma, 13, params)
    # 普通量能日非放量
    assert not _is_volume_expansion(v, vol_ma, 11, params)


# --- 端到端：买点 ---


def test_buy_on_confirmation_volume_expansion():
    df = _scenario_df()
    result = STRATEGY.run(df, _base(), _params())
    buys = [t for t in result.trades if t["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["price"] == 10.6  # 确认日收盘价
    assert buys[0]["date"] == df.index[13].isoformat()


# --- 端到端：离场 ---


def _run_with_tail(tail_close, tail_open, tail_high, tail_low, tail_volume):
    """在标准场景 14 根后再接一段尾串，用于离场测试。"""
    close = np.concatenate([_scenario_close(), tail_close])
    open_a = np.concatenate([_scenario_open(), tail_open])
    hi = np.concatenate([_scenario_high_low()[0], tail_high])
    lo = np.concatenate([_scenario_high_low()[1], tail_low])
    v = np.concatenate([_scenario_volume(), tail_volume])
    df = _df(close, open_a, hi, lo, v)
    return STRATEGY.run(df, _base(), _params())


def test_stop_loss_exit():
    # 进场(13)后一日(14)低点跌破成本-5%
    tail_close = np.array([10.0])
    tail_open = np.array([10.0])
    tail_high = np.array([10.2])
    tail_low = np.array([9.9])   # 成本 10.6*0.95=10.07 → 触发止损
    tail_volume = np.array([100.0])
    result = _run_with_tail(tail_close, tail_open, tail_high, tail_low, tail_volume)
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["date"] == df_index_of(result, 14)


def test_next_volume_expansion_exit():
    # 进场后一日放量 → 落袋
    tail_close = np.array([10.8])
    tail_open = np.array([10.5])
    tail_high = np.array([11.0])
    tail_low = np.array([10.6])   # 不触发止损
    tail_volume = np.array([300.0])  # 放量
    result = _run_with_tail(tail_close, tail_open, tail_high, tail_low, tail_volume)
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["date"] == df_index_of(result, 14)


def test_time_cap_exit():
    # 连续 max_hold_days(3) 日既不止损也不放量 → 时间兜底平仓
    tail_close = np.array([10.6, 10.7, 10.7])
    tail_open = np.array([10.55, 10.65, 10.68])
    tail_high = np.array([10.7, 10.75, 10.72])
    tail_low = np.array([10.55, 10.62, 10.65])  # 高于止损
    tail_volume = np.array([100.0, 100.0, 100.0])  # 非放量
    result = _run_with_tail(tail_close, tail_open, tail_high, tail_low, tail_volume)
    sells = [t for t in result.trades if t["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["date"] == df_index_of(result, 16)  # 16-13=3 ≥ max_hold_days


def df_index_of(result, i: int) -> str:
    # 从回测结果 price 反查第 i 根日期
    return result.price[i]["date"]


# --- min_bars ---


def test_min_bars_uses_max_of_periods():
    p = _params(ma_period=5, limit_lookback=8, vol_ma_period=3)
    assert STRATEGY.min_bars(p) == max(5, 8, 3) + 2  # 10
