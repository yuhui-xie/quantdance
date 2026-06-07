"""volume_ma_pulse 量比口径与阈值模式测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BaseBacktestParams
from app.strategies.volume_ma_pulse import VolumeMaPulseParams, _activity_series, _run


def _ohlcv(*, volume: list[float], amount: list[float] | None = None) -> pd.DataFrame:
    n = len(volume)
    close = np.linspace(10.0, 12.0, n)
    data = {
        "open": close - 0.05,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": volume,
    }
    if amount is not None:
        data["amount"] = amount
    return pd.DataFrame(data, index=pd.date_range("2024-01-01", periods=n, freq="D"))


def test_volume_ratio_excludes_current_day_from_ma():
    # 前 5 日量均为 100，第 6 日放量到 300
    volume = [100.0] * 5 + [300.0]
    df = _ohlcv(volume=volume)
    result = _run(
        df,
        BaseBacktestParams(initial_cash=100000.0, commission=0.0),
        VolumeMaPulseParams(
            volume_ma_period=5,
            volume_metric="volume",
            threshold_mode="fixed",
            high_ratio=1.5,
            low_ratio=0.5,
            require_bull_bar=False,
            require_bear_bar=False,
        ),
    )
    # 分母应为过去 5 日均量 100，量比=3.0（若含当日则为 300/140≈2.14）
    assert result.price[-1]["vol_ratio"] == pytest.approx(3.0)
    assert result.price[-1]["volume_ma"] == pytest.approx(100.0)


def test_activity_prefers_amount_when_available():
    df = _ohlcv(volume=[1.0, 1.0, 1.0], amount=[10.0, 20.0, 30.0])
    series = _activity_series(df, "amount")
    assert list(series.values) == [10.0, 20.0, 30.0]


def test_activity_falls_back_to_volume_without_amount():
    df = _ohlcv(volume=[1.0, 2.0, 3.0])
    series = _activity_series(df, "amount")
    assert list(series.values) == [1.0, 2.0, 3.0]


def test_percentile_thresholds_are_adaptive():
    rng = np.random.default_rng(0)
    base = rng.uniform(80.0, 120.0, size=160)
    # 末尾制造明显放量，应触发买入（阳线 + 分位上穿）
    volume = list(base) + [500.0]
    n = len(volume)
    close = np.linspace(10.0, 15.0, n)
    df = pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": volume,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )
    result = _run(
        df,
        BaseBacktestParams(initial_cash=100000.0, commission=0.0),
        VolumeMaPulseParams(
            volume_ma_period=20,
            volume_metric="volume",
            threshold_mode="percentile",
            high_percentile=0.8,
            low_percentile=0.2,
            percentile_lookback=100,
            require_bull_bar=True,
            require_bear_bar=False,
        ),
    )
    assert result.price[-1]["vol_ratio"] is not None
    assert result.price[-1]["high_th"] is not None
    assert float(result.price[-1]["vol_ratio"]) > float(result.price[-1]["high_th"])
    assert int(result.metrics["num_trades"]) >= 1


def _pulse_df() -> pd.DataFrame:
    """构造：足够预热后放量，之后两日量回落。"""
    warm = 8
    volume = [100.0] * warm + [300.0, 110.0, 105.0]
    n = len(volume)
    close = [10.0 + 0.1 * i for i in range(n)]
    # 放量日收盘抬高，便于确认日站稳
    close[warm] = 11.2
    close[warm + 1] = 11.3
    close[warm + 2] = 11.4
    open_ = [c - 0.05 for c in close]
    low = [c - 0.15 for c in close]
    high = [c + 0.15 for c in close]
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    ), warm


def test_entry_confirm_bars_zero_buys_on_pulse_day():
    df, warm = _pulse_df()
    result = _run(
        df,
        BaseBacktestParams(initial_cash=100000.0, commission=0.0),
        VolumeMaPulseParams(
            volume_ma_period=5,
            volume_metric="volume",
            threshold_mode="fixed",
            high_ratio=1.5,
            low_ratio=0.5,
            require_bull_bar=False,
            require_bear_bar=False,
            entry_confirm_bars=0,
        ),
    )
    buy_dates = [t["date"] for t in result.trades if t["side"] == "buy"]
    assert buy_dates == [df.index[warm].isoformat()]


def test_entry_confirm_bars_one_buys_after_hold():
    df, warm = _pulse_df()
    result = _run(
        df,
        BaseBacktestParams(initial_cash=100000.0, commission=0.0),
        VolumeMaPulseParams(
            volume_ma_period=5,
            volume_metric="volume",
            threshold_mode="fixed",
            high_ratio=1.5,
            low_ratio=0.5,
            require_bull_bar=False,
            require_bear_bar=False,
            entry_confirm_bars=1,
        ),
    )
    # 放量日不买；次日收盘 11.3 >= 放量日收盘 11.2，确认买入
    buy_dates = [t["date"] for t in result.trades if t["side"] == "buy"]
    assert buy_dates == [df.index[warm + 1].isoformat()]


def test_entry_confirm_bars_invalidates_on_break_of_pulse_low():
    df, warm = _pulse_df()
    # 次日跌破放量日低点 11.2-0.15=11.05
    df.loc[df.index[warm + 1], "close"] = 10.9
    df.loc[df.index[warm + 1], "open"] = 11.0
    df.loc[df.index[warm + 1], "low"] = 10.8
    result = _run(
        df,
        BaseBacktestParams(initial_cash=100000.0, commission=0.0),
        VolumeMaPulseParams(
            volume_ma_period=5,
            volume_metric="volume",
            threshold_mode="fixed",
            high_ratio=1.5,
            low_ratio=0.5,
            require_bull_bar=False,
            require_bear_bar=False,
            entry_confirm_bars=1,
        ),
    )
    buy_dates = [t["date"] for t in result.trades if t["side"] == "buy"]
    assert buy_dates == []
