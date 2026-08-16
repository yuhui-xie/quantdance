"""多头排列策略测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BaseBacktestParams
from app.strategies.bullish_alignment import (
    BullishAlignmentParams,
    _positions_to_events,
)
from app.strategies.registry import STRATEGIES


def _rising_df(rows: int = 120) -> pd.DataFrame:
    x = np.arange(rows, dtype=float)
    close = 20.0 + x * 0.5  # 单调上升，尾部完全多头排列
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 100_000.0,
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="D"),
    )


def test_strategy_registered_and_runs():
    spec = STRATEGIES["bullish_alignment"]
    params = BullishAlignmentParams()
    df = _rising_df()
    base = BaseBacktestParams(initial_cash=100_000, commission=0.0003)

    result = spec.run(df, base, params)

    assert len(result.signal) == len(df)
    assert set(result.signal).issubset({-1, 0, 1})
    assert len(result.price) == len(df)
    # 单调上升时尾部完全多头排列，应保持持仓态。
    assert result.price[-1]["bullish_alignment"] == pytest.approx(1.0)
    assert result.price[-1]["bullish_position"] == 1
    # 各周期均线曲线作为 overlay 输出，供图叠加展示多头排列形态。
    assert result.price[-1]["ma_5"] is not None
    assert result.price[-1]["ma_10"] is not None
    assert result.price[-1]["ma_20"] is not None
    assert spec.compute_signals(df, base, params).tolist() == result.signal


def test_strategy_falls_flat_on_declining_series():
    spec = STRATEGIES["bullish_alignment"]
    params = BullishAlignmentParams()
    rows = 120
    x = np.arange(rows, dtype=float)
    close = 120.0 - x * 0.5  # 单调下降，多头排列分数为 0
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 100_000.0,
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="D"),
    )
    base = BaseBacktestParams(initial_cash=100_000, commission=0.0003)

    result = spec.run(df, base, params)

    assert all(int(s) == 0 for s in result.signal)  # 永不满仓买入
    assert result.price[-1]["bullish_position"] == 0


def test_strategy_min_bars_is_longest_period():
    spec = STRATEGIES["bullish_alignment"]
    assert spec.min_bars(BullishAlignmentParams(periods=(5, 10, 20))) == 20


def test_positions_to_events_round_trip():
    positions = np.array([0, 0, 1, 1, 1, 0, 0, 1], dtype=np.int8)
    assert _positions_to_events(positions).tolist() == [0, 0, 1, 0, 0, -1, 0, 1]


def test_params_requires_hysteresis_gap():
    with pytest.raises(ValueError, match="exit_threshold 必须小于 enter_threshold"):
        BullishAlignmentParams(enter_threshold=0.5, exit_threshold=0.5)


def test_params_rejects_duplicate_periods():
    with pytest.raises(ValueError, match="互不重复"):
        BullishAlignmentParams(periods=(5, 5, 20))
