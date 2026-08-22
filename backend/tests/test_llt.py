"""公共 LLT 指标及策略接入测试。"""

from __future__ import annotations

import numpy as np
import pytest

from app.factors import llt
from app.factors.llt import llt_slope, llt_slope_fit, llt_slope_reg
from app.strategies.cross_section.limit_up_pullback import _mild_ma_up


def test_llt_tracks_constant_and_linear_series():
    constant = np.full(12, 8.0)
    np.testing.assert_allclose(llt(constant, period=5), constant)

    linear = np.arange(1.0, 13.0)
    np.testing.assert_allclose(llt(linear, period=3), linear)


def test_llt_restarts_after_non_finite_value():
    actual = llt([1.0, 2.0, 3.0, np.nan, 10.0, 11.0, 12.0], period=3)
    np.testing.assert_allclose(
        actual,
        [1.0, 2.0, 3.0, np.nan, 10.0, 11.0, 12.0],
        equal_nan=True,
    )


@pytest.mark.parametrize("period", [0, 1])
def test_llt_rejects_invalid_period(period: int):
    with pytest.raises(ValueError, match="大于等于 2"):
        llt([1.0, 2.0], period=period)


def test_llt_slope_difference():
    linear = np.arange(1.0, 9.0)
    expected = np.concatenate([[np.nan], np.diff(linear)])
    np.testing.assert_allclose(llt_slope(linear, lookback=1), expected)
    # lookback=2 为两期差分，前 2 项为 NaN
    dt = llt_slope(linear, lookback=2)
    np.testing.assert_array_equal(dt[:2], [np.nan, np.nan])
    np.testing.assert_allclose(dt[2:], np.full(6, 2.0))


def test_llt_slope_reg_recovers_exact_linear_slope():
    linear = np.arange(0.0, 12.0)  # 斜率 1.0
    slope = llt_slope_reg(linear, window=6)
    # 前 window-1 项为 NaN
    assert np.isnan(slope[:5]).all()
    np.testing.assert_allclose(slope[5:], np.full(7, 1.0))


def test_llt_slope_reg_smooths_noisy_trend():
    # 单调但含抖动：回归斜率与单点差分都为正，但回归更接近整体趋势
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 1.0, 40)
    series = np.arange(40.0) + noise
    reg = llt_slope_reg(series, window=10)
    diff = llt_slope(series, lookback=1)
    # 整体线性趋势斜率为 1，回归斜率应显著更接近 1 且更稳定
    assert np.nanmean(np.abs(reg)) < np.nanmean(np.abs(diff))
    assert np.nanstd(reg) < np.nanstd(diff)


@pytest.mark.parametrize("window", [0, 1])
def test_llt_slope_reg_rejects_invalid_window(window: int):
    with pytest.raises(ValueError, match="大于等于 2"):
        llt_slope_reg([1.0, 2.0, 3.0], window=window)


def test_llt_slope_fit_linear_is_perfect_fit():
    linear = np.arange(0.0, 12.0)
    slope, r2 = llt_slope_fit(linear, window=6)
    np.testing.assert_allclose(slope[5:], np.full(7, 1.0))
    # 完美直线 → R²=1
    np.testing.assert_allclose(r2[5:], np.ones(7))
    # 前 window-1 项为 NaN
    assert np.isnan(slope[:5]).all() and np.isnan(r2[:5]).all()


def test_llt_slope_fit_noisy_has_low_r2():
    rng = np.random.default_rng(1)
    # 纯噪音（无趋势）→ 拟合质量差，R² 接近 0
    noise = rng.normal(0, 5.0, 40)
    _slope, r2 = llt_slope_fit(noise, window=10)
    valid = r2[9:]
    assert np.nanmean(valid) < 0.5


def test_llt_slope_reg_matches_fit_slope():
    series = np.array([1.0, 3.0, 2.0, 5.0, 4.0, 6.0, 8.0, 7.0, 9.0, 11.0])
    np.testing.assert_allclose(
        llt_slope_reg(series, window=4),
        llt_slope_fit(series, window=4)[0],
        equal_nan=True,
    )


def test_mild_ma_up_uses_llt_slope(monkeypatch):
    closes = np.full(30, 10.0)

    monkeypatch.setattr(
        "app.strategies.cross_section.limit_up_pullback.llt",
        lambda values, period: np.arange(len(values), dtype=float),
    )
    assert _mild_ma_up(
        closes,
        fast=5,
        mid=10,
        slow=20,
        llt_period=20,
        llt_slope_lookback=5,
    )

    monkeypatch.setattr(
        "app.strategies.cross_section.limit_up_pullback.llt",
        lambda values, period: np.arange(len(values), 0, -1, dtype=float),
    )
    assert not _mild_ma_up(
        closes,
        fast=5,
        mid=10,
        slow=20,
        llt_period=20,
        llt_slope_lookback=5,
    )
