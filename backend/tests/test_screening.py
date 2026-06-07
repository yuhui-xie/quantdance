"""因子选股纯函数单元测试（不访问外网）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.stock_screening import (
    MIN_BARS,
    _minmax_norm,
    _score_from_factor_config,
    _score_from_breakdowns,
    compute_liquidity_breakdown,
    compute_low_volatility_breakdown,
    compute_ma_alignment_breakdown,
    compute_momentum_breakdown,
    compute_short_reversal_breakdown,
    compute_volume_pulse_breakdown,
)
from app.schemas import ScreenFactorConfig


def _sample_ohlcv_uptrend(n: int = 80, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 10 + np.linspace(0, 5, n) + rng.normal(0, 0.05, n)
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.02, n)
    high = np.maximum(open_, close) + rng.random(n) * 0.1
    low = np.minimum(open_, close) - rng.random(n) * 0.1
    vol = 1e6 + rng.random(n) * 1e5
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=t,
    )


def test_minmax_norm_constant_returns_half():
    assert _minmax_norm([3.0, 3.0, 3.0]) == [0.5, 0.5, 0.5]


def test_minmax_norm_range():
    assert _minmax_norm([0.0, 10.0]) == [0.0, 1.0]


def test_momentum_breakdown_uptrend_positive_returns():
    df = _sample_ohlcv_uptrend()
    bd = compute_momentum_breakdown(df)
    assert bd is not None
    assert bd["ret_20"] > 0
    assert bd["ret_60"] > 0
    assert bd["vol_20"] >= 0


def test_momentum_breakdown_too_short():
    df = _sample_ohlcv_uptrend(n=40)
    assert compute_momentum_breakdown(df) is None


def test_volume_pulse_breakdown_keys():
    df = _sample_ohlcv_uptrend()
    bd = compute_volume_pulse_breakdown(df)
    assert bd is not None
    assert "vol_ratio" in bd and "price_vs_ma5" in bd


def test_ma_alignment_breakdown_keys():
    df = _sample_ohlcv_uptrend()
    bd = compute_ma_alignment_breakdown(df)
    assert bd is not None
    assert "ma_spread" in bd and "sma5_slope" in bd


def test_score_from_breakdowns_momentum_ordering():
    """高动量条目得分应高于低动量（横截面内）。"""
    hi = {"ret_20": 0.2, "ret_60": 0.3, "vol_20": 0.01}
    lo = {"ret_20": -0.1, "ret_60": -0.05, "vol_20": 0.05}
    entries = [
        ("000001", "A", hi),
        ("000002", "B", lo),
    ]
    items = _score_from_breakdowns("momentum", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_factor_config_weighted_direction_ordering():
    config = ScreenFactorConfig.model_validate(
        {
            "factors": [
                {"field": "ret_20", "weight": 0.7, "direction": "higher"},
                {"field": "vol_20", "weight": 0.3, "direction": "lower"},
            ]
        }
    )
    better = {"ret_20": 0.15, "vol_20": 0.01}
    worse = {"ret_20": -0.05, "vol_20": 0.08}
    entries = [
        ("000001", "A", better),
        ("000002", "B", worse),
    ]

    items = _score_from_factor_config(config, entries)

    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_low_volatility_breakdown_keys_and_order():
    df = _sample_ohlcv_uptrend(n=80)
    bd = compute_low_volatility_breakdown(df)
    assert bd is not None
    assert "vol_20" in bd and "vol_60" in bd
    assert bd["vol_20"] >= 0 and bd["vol_60"] >= 0


def test_short_reversal_breakdown_keys():
    df = _sample_ohlcv_uptrend()
    bd = compute_short_reversal_breakdown(df)
    assert bd is not None
    assert "ret_5" in bd and "ret_10" in bd


def test_liquidity_breakdown_keys():
    df = _sample_ohlcv_uptrend()
    bd = compute_liquidity_breakdown(df)
    assert bd is not None
    assert "rel_vol" in bd and "amihud_illiq_20" in bd
    assert bd["rel_vol"] > 0


def test_score_from_breakdowns_low_vol_ordering():
    """横截面内波动率更低者得分更高。"""
    low = {"vol_20": 0.01, "vol_60": 0.02}
    high = {"vol_20": 0.08, "vol_60": 0.09}
    entries = [
        ("000001", "A", low),
        ("000002", "B", high),
    ]
    items = _score_from_breakdowns("low_volatility", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_short_reversal_ordering():
    """近期跌幅更大（收益更负）者得分更高。"""
    dump = {"ret_5": -0.15, "ret_10": -0.2}
    flat = {"ret_5": 0.02, "ret_10": 0.03}
    entries = [
        ("000001", "A", dump),
        ("000002", "B", flat),
    ]
    items = _score_from_breakdowns("short_reversal", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_liquidity_ordering():
    """相对成交量更高、Amihud 非流动性更低者得分更高。"""
    liquid = {"rel_vol": 3.0, "amihud_illiq_20": 1e-8}
    thin = {"rel_vol": 0.5, "amihud_illiq_20": 1e-3}
    entries = [
        ("000001", "A", liquid),
        ("000002", "B", thin),
    ]
    items = _score_from_breakdowns("liquidity", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_value_tilt_ordering():
    """PE、PB 更低者得分更高。"""
    cheap = {"pe_ttm": 8.0, "pb": 1.0}
    rich = {"pe_ttm": 40.0, "pb": 5.0}
    entries = [
        ("000001", "A", cheap),
        ("000002", "B", rich),
    ]
    items = _score_from_breakdowns("value_tilt", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_low_pe_ordering():
    """PE 更低者得分更高。"""
    cheap = {"pe_ttm": 10.0, "pb": 2.0}
    rich = {"pe_ttm": 50.0, "pb": 1.0}
    entries = [
        ("000001", "A", cheap),
        ("000002", "B", rich),
    ]
    items = _score_from_breakdowns("low_pe", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_low_pb_ordering():
    """PB 更低者得分更高。"""
    cheap = {"pe_ttm": 20.0, "pb": 0.8}
    rich = {"pe_ttm": 12.0, "pb": 4.0}
    entries = [
        ("000001", "A", cheap),
        ("000002", "B", rich),
    ]
    items = _score_from_breakdowns("low_pb", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_low_ps_ordering():
    """PS 更低者得分更高。"""
    cheap = {"ps_ttm": 1.5}
    rich = {"ps_ttm": 8.0}
    entries = [
        ("000001", "A", cheap),
        ("000002", "B", rich),
    ]
    items = _score_from_breakdowns("low_ps", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_quality_value_ordering():
    """同等条件下 ROE 更高、PE/PB 更低者综合得分更高。"""
    better = {"pe_ttm": 10.0, "pb": 1.0, "roe": 0.18}
    worse = {"pe_ttm": 30.0, "pb": 3.0, "roe": 0.06}
    entries = [
        ("000001", "A", better),
        ("000002", "B", worse),
    ]
    items = _score_from_breakdowns("quality_value", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_score_from_breakdowns_dividend_tilt_ordering():
    """股息率更高者得分更高。"""
    high_div = {"dividend_yield": 0.05}
    low_div = {"dividend_yield": 0.01}
    entries = [
        ("000001", "A", high_div),
        ("000002", "B", low_div),
    ]
    items = _score_from_breakdowns("dividend_tilt", entries)
    assert len(items) == 2
    assert items[0].symbol == "000001"
    assert items[0].score >= items[1].score


def test_min_bars_constant_sensible():
    assert MIN_BARS >= 60
