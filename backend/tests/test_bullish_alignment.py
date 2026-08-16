"""多头排列因子测试。"""

from __future__ import annotations

import numpy as np
import pytest

from app.factors import bullish_alignment


def test_bullish_alignment_full_on_rising_series():
    values = np.arange(1.0, 31.0)

    actual = bullish_alignment(values, periods=(5, 10, 20))

    # 单调上升时短均线恒大于长均线，最长周期形成后分数恒为 1。
    assert np.isnan(actual[:19]).all()
    np.testing.assert_allclose(actual[19:], 1.0)


def test_bullish_alignment_zero_on_falling_series():
    values = np.arange(30.0, 0.0, -1.0)

    actual = bullish_alignment(values, periods=(5, 10, 20))

    np.testing.assert_allclose(actual[19:], 0.0)


def test_bullish_alignment_non_finite_windows_are_nan():
    actual = bullish_alignment([1.0, 2.0, 3.0, 4.0, np.inf, 6.0, 7.0, 8.0], periods=(1, 2))

    # 含 NaN/Inf 的窗口分数未定义，其余短均线>长均线的窗口为 1.0。
    np.testing.assert_allclose(
        actual,
        [np.nan, 1.0, 1.0, 1.0, np.nan, np.nan, 1.0, 1.0],
        equal_nan=True,
    )


def test_bullish_alignment_handles_unsorted_periods():
    values = np.arange(1.0, 31.0)

    np.testing.assert_allclose(
        bullish_alignment(values, periods=(20, 10, 5)),
        bullish_alignment(values, periods=(5, 10, 20)),
        equal_nan=True,
    )


@pytest.mark.parametrize("periods", [(1,), (0, 2)])
def test_bullish_alignment_rejects_invalid_period_values(periods):
    with pytest.raises(ValueError):
        bullish_alignment([1.0, 2.0], periods=periods)


def test_bullish_alignment_rejects_non_integer_period():
    with pytest.raises(TypeError, match="整数"):
        bullish_alignment([1.0, 2.0], periods=(1, 2.5))
