"""公共 BBI 指标测试。"""

from __future__ import annotations

import numpy as np
import pytest

from app.indicators import bbi


def test_bbi_uses_classic_four_moving_average_formula():
    values = np.arange(1.0, 31.0)

    actual = bbi(values)

    expected_at_24 = (
        np.mean(values[21:24])
        + np.mean(values[18:24])
        + np.mean(values[12:24])
        + np.mean(values[:24])
    ) / 4.0
    assert np.isnan(actual[:23]).all()
    assert actual[23] == pytest.approx(expected_at_24)


def test_bbi_supports_custom_periods_and_non_finite_windows():
    actual = bbi([1.0, 2.0, 3.0, 4.0, np.inf, 6.0], periods=(1, 2))

    np.testing.assert_allclose(
        actual,
        [np.nan, 1.75, 2.75, 3.75, np.nan, np.nan],
        equal_nan=True,
    )


@pytest.mark.parametrize("periods", [(), (0, 2)])
def test_bbi_rejects_invalid_period_values(periods):
    with pytest.raises(ValueError):
        bbi([1.0, 2.0], periods=periods)


def test_bbi_rejects_non_integer_period():
    with pytest.raises(TypeError, match="整数"):
        bbi([1.0, 2.0], periods=(1, 2.5))
