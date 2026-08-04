"""BBI 趋势策略测试。"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from app.strategies.bbi_trend import (
    STRATEGY,
    BbiTrendParams,
    _signals_from_bbi,
)


def test_price_crossing_bbi_generates_buy_and_sell_signals():
    close = np.array([9.0, 10.0, 11.0, 10.0, 9.0])
    bbi_values = np.full(5, 10.0)

    signal = _signals_from_bbi(close, bbi_values)

    assert signal.tolist() == [0, 0, 1, 0, -1]


def test_bbi_strategy_requires_two_valid_bbi_values():
    params = BbiTrendParams()

    assert STRATEGY.min_bars(params) == 25


def test_bbi_periods_must_be_strictly_increasing():
    with pytest.raises(ValidationError, match="严格递增"):
        BbiTrendParams(
            short_period=6,
            medium_period=6,
            long_period=12,
            longest_period=24,
        )
