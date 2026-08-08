"""公共 LLT 指标及策略接入测试。"""

from __future__ import annotations

import numpy as np
import pytest

from app.indicators import llt
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
