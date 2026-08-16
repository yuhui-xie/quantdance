"""my_strategy 策略测试（重点覆盖 Choppiness 震荡市过滤）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.my_strategy import (
    MyStrategyParams,
    STRATEGY,
    _compute,
    _positions,
)


def _params(**overrides) -> MyStrategyParams:
    defaults = dict(
        periods=(5, 10, 20),
        enter_threshold=1.0,
        exit_threshold=0.5,
        llt_period=20,
        slope_lookback=1,
    )
    defaults.update(overrides)
    return MyStrategyParams(**defaults)


def test_ranging_blocks_new_trend_entry():
    # 条件全部满足（score 高、斜率向上），但处于震荡市区间 → 不开单、强制空仓
    score = np.ones(9)
    slope = np.ones(9)
    ranging = np.array([0, 0, 0, 1, 1, 1, 0, 0, 0], dtype=bool)
    params = _params()

    positions = _positions(score, slope, ranging, params)

    assert positions.tolist() == [1, 1, 1, 0, 0, 0, 1, 1, 1]


def test_ranging_forces_exit_of_existing_position():
    # 已持仓后进入震荡市 → 立即平仓转空
    score = np.ones(4)
    slope = np.ones(4)
    ranging = np.array([0, 0, 1, 1], dtype=bool)
    params = _params()

    positions = _positions(score, slope, ranging, params)

    assert positions.tolist() == [1, 1, 0, 0]


def test_no_ranging_opens_trend_position():
    score = np.ones(5)
    slope = np.ones(5)
    ranging = np.zeros(5, dtype=bool)
    params = _params()

    positions = _positions(score, slope, ranging, params)

    assert positions.tolist() == [1, 1, 1, 1, 1]


def test_chop_filter_disabled_ignores_ranging():
    # use_chop_filter=False 时，即使 CHOP 高也不阻塞
    params = _params(use_chop_filter=False)
    df = _choppy_df()

    _signal, overlays = _compute(df, params)

    assert not overlays["chop_ranging"].any()
    # 原始趋势逻辑仍会持仓（此处不依赖具体 CHOP 值，只验证过滤被关闭）
    assert overlays["chop_ranging"].dtype == np.int8


def test_compute_chop_overlay_matches_threshold_mask():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)
    chop = overlays["chop"]
    chop_ranging = overlays["chop_ranging"].astype(bool)

    finite = np.isfinite(chop)
    np.testing.assert_array_equal(chop_ranging[finite], chop[finite] > params.chop_threshold)
    # 非有限（暖机期）不应误判为震荡
    assert not chop_ranging[~finite].any()


def test_positions_flat_wherever_ranging():
    df = _choppy_df()
    params = _params()

    _signal, overlays = _compute(df, params)
    ranging = overlays["chop_ranging"].astype(bool)
    positions = overlays["position"]

    assert (positions[ranging] == 0).all()


def test_min_bars_includes_chop_period_when_filter_enabled():
    params = _params(use_chop_filter=True, chop_period=30)
    assert STRATEGY.min_bars(params) == 32  # max(20, 30) + 1 + slope(1)


def test_min_bars_ignores_chop_period_when_filter_disabled():
    params = _params(use_chop_filter=False, chop_period=30)
    assert STRATEGY.min_bars(params) == 22  # 20 + 1 + slope(1)


def _choppy_df() -> pd.DataFrame:
    """构造一段高频强震荡行情（CHOP 稳定高于 62）。"""
    n = 120
    close = 10.0 + np.concatenate(
        [np.repeat(np.arange(k, k + 2), 4) for k in range(30)]
    )[:n].astype(float)
    high = close + 0.8
    low = close - 0.8
    return pd.DataFrame(
        {"high": high, "low": low, "close": close, "volume": np.ones(n)}
    )
