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


def test_llt_strategy_min_bars_includes_warmup_and_slope_window():
    params = LltTrendParams(period=20, slope_lookback=3)

    from app.strategies.llt_trend import STRATEGY

    assert STRATEGY.min_bars(params) == 24
