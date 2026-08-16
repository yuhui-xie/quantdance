"""区间震荡因子（ADX / Choppiness Index）测试。"""

from __future__ import annotations

import numpy as np
import pytest

from app.factors import adx, choppiness_index


def _trending() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构造一段稳定单边上行行情（ADX 应偏高、CHOP 应偏低）。"""
    n = 80
    close = np.arange(10.0, 10.0 + n)
    high = close + 0.5
    low = close - 0.5
    return high, low, close


def _choppy() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构造一段随机游走震荡行情（ADX 应偏低、CHOP 应偏高）。"""
    rng = np.random.default_rng(7)
    n = 100
    close = 10.0 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.5
    low = close - 0.5
    return high, low, close


def test_adx_output_shape_and_nan_prefix():
    high, low, close = _trending()
    actual = adx(high, low, close, period=14)

    assert actual.shape == close.shape
    # 首个有效值约在 period 处，此前为 NaN
    assert np.isnan(actual[:13]).all()
    assert np.isfinite(actual[14:]).any()


def test_adx_is_high_on_trend_and_low_on_range():
    th, tl, tc = _trending()
    ch, cl, cc = _choppy()

    trend_adx = adx(th, tl, tc, period=14)
    range_adx = adx(ch, cl, cc, period=14)

    assert np.nanmean(trend_adx[40:]) > np.nanmean(range_adx[40:])
    # 单边行情 ADX 应明显高于震荡阈值 20
    assert np.nanmean(trend_adx[40:]) > 40


def test_choppiness_high_on_range_and_low_on_trend():
    th, tl, tc = _trending()
    ch, cl, cc = _choppy()

    trend_chop = choppiness_index(th, tl, tc, period=14)
    range_chop = choppiness_index(ch, cl, cc, period=14)

    assert np.nanmean(range_chop[40:]) > np.nanmean(trend_chop[40:])
    assert 0 <= np.nanmin(range_chop[40:]) <= np.nanmax(range_chop[40:]) <= 100


def test_choppiness_nan_prefix_and_invalid_window():
    high, low, close = _choppy()
    actual = choppiness_index(high, low, close, period=14)

    assert actual.shape == close.shape
    assert np.isnan(actual[:13]).all()


def test_zero_range_yields_nan_for_choppiness():
    flat = np.full(30, 10.0)
    actual = choppiness_index(flat, flat, flat, period=14)
    assert np.isnan(actual).all()


def test_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="等长"):
        adx([1.0, 2.0], [1.0], [1.0, 2.0])


def test_rejects_non_integer_period():
    with pytest.raises(TypeError, match="整数"):
        adx([1.0], [1.0], [1.0], period=2.5)
    with pytest.raises(TypeError, match="整数"):
        choppiness_index([1.0], [1.0], [1.0], period=2.5)


def test_rejects_short_period():
    with pytest.raises(ValueError, match="大于等于 1"):
        choppiness_index([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], period=0)
