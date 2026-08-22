"""OBV / VPT 量价指标测试。"""

from __future__ import annotations

import numpy as np
import pytest

from app.factors import obv, vpt


def test_obv_classic_accumulation():
    # 涨 +、跌 -、平盘不变
    close = np.array([10.0, 11.0, 10.0, 10.0, 12.0])
    volume = np.array([100.0, 200.0, 300.0, 50.0, 400.0])
    expected = np.array([0.0, 200.0, -100.0, -100.0, 300.0])
    np.testing.assert_allclose(obv(close, volume), expected)


def test_obv_first_day_zero():
    close = np.array([10.0, 11.0])
    volume = np.array([999.0, 5.0])
    np.testing.assert_allclose(obv(close, volume), [0.0, 5.0])


def test_vpt_classic_accumulation():
    close = np.array([10.0, 11.0, 10.0])
    volume = np.array([100.0, 200.0, 150.0])
    # vpt[0]=0; vpt[1]=200*(11-10)/10=20; vpt[2]=20+150*(10-11)/11=20-13.636...
    expected = np.array([0.0, 20.0, 20.0 - 150.0 / 11.0])
    np.testing.assert_allclose(vpt(close, volume), expected)


def test_obv_vpt_handle_nan_gap():
    close = np.array([10.0, np.nan, 12.0, 13.0])
    volume = np.array([100.0, 200.0, 300.0, 50.0])
    # OBV：首日 0；NaN 日 NaN 且重置；随后从 0 重新累积
    obv_out = obv(close, volume)
    assert np.isnan(obv_out[1])
    # 重置后首段（i=2，last_close 无效）acc=0
    assert obv_out[2] == 0.0
    assert obv_out[3] == 50.0  # 13>12 上涨 +50
    # VPT 同样在 NaN 日输出 NaN 并重置
    vpt_out = vpt(close, volume)
    assert np.isnan(vpt_out[1])
    assert vpt_out[2] == 0.0
    assert vpt_out[3] == 50.0 * (13.0 - 12.0) / 12.0


def test_length_and_dtype():
    close = np.array([10.0, 11.0, 12.0])
    volume = np.array([1.0, 2.0, 3.0])
    for out in (obv(close, volume), vpt(close, volume)):
        assert out.shape == close.shape
        assert out.dtype == np.float64


def test_raises_on_length_mismatch():
    with pytest.raises(ValueError):
        obv(np.array([1.0, 2.0]), np.array([1.0]))


def test_raises_on_2d_input():
    with pytest.raises(ValueError):
        vpt(np.zeros((2, 2)), np.zeros((2, 2)))
