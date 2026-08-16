"""纯 LLT 趋势拐点策略测试。"""

from __future__ import annotations

import numpy as np

from app.strategies.llt_trend import LltTrendParams, _signals_from_llt


def test_llt_slope_turns_generate_buy_and_sell_signals():
    trend = np.array(
        [10.0, 9.0, 8.0, 7.0, 7.0, 8.0, 9.0, 9.0, 8.0],
        dtype=float,
    )

    signal = _signals_from_llt(trend, period=2, slope_lookback=1)

    assert signal.tolist() == [0, 0, 0, 0, 0, 1, 0, 0, -1]


def test_llt_slope_threshold_zero_preserves_zero_crossing_behavior():
    trend = np.array(
        [10.0, 9.0, 8.0, 7.0, 7.0, 8.0, 9.0, 9.0, 8.0],
        dtype=float,
    )

    assert _signals_from_llt(
        trend, period=2, slope_lookback=1, threshold=0.0
    ).tolist() == [0, 0, 0, 0, 0, 1, 0, 0, -1]


def test_llt_slope_threshold_filters_small_noise_in_dead_zone():
    # 斜率在 ±0.5 之间的小幅震荡不应触发信号
    trend = np.array(
        [10.0, 10.2, 10.1, 10.3, 10.2, 10.4, 10.3, 10.5, 10.4],
        dtype=float,
    )

    signal = _signals_from_llt(
        trend, period=2, slope_lookback=1, threshold=0.5
    )

    assert signal.tolist() == [0, 0, 0, 0, 0, 0, 0, 0, 0]


def test_llt_slope_threshold_fires_only_on_strong_trend():
    # 只有斜率足够强劲（上穿 +0.5 / 下穿 -0.5）才产生买卖信号
    trend = np.array(
        [10.0, 10.0, 10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 10.0],
        dtype=float,
    )

    signal = _signals_from_llt(
        trend, period=2, slope_lookback=1, threshold=0.5
    )

    # 斜率在 i=3 上穿 +0.5 触发买入，在 i=6 下穿 -0.5 触发卖出
    assert signal.tolist() == [0, 0, 0, 1, 0, 0, -1, 0, 0]


def test_llt_strategy_min_bars_includes_warmup_and_slope_window():
    params = LltTrendParams(period=20, slope_lookback=3)

    from app.strategies.llt_trend import STRATEGY

    assert STRATEGY.min_bars(params) == 24
