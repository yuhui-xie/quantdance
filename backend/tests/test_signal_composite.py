"""多策略信号组合测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BaseBacktestParams
from app.strategies.registry import STRATEGIES
from app.strategies.signal_composite import (
    CompositeLeg,
    SignalCompositeParams,
    _events_to_positions,
    _positions_to_events,
)


def _sample_ohlcv(rows: int = 120) -> pd.DataFrame:
    x = np.arange(rows, dtype=float)
    close = 20.0 + np.sin(x / 4.0) * 2.0 + x * 0.02
    open_ = close + np.cos(x / 6.0) * 0.15
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 0.3,
            "low": np.minimum(open_, close) - 0.3,
            "close": close,
            "volume": 100_000.0 + (x % 7) * 5_000.0,
        },
        index=pd.date_range("2024-01-01", periods=rows, freq="D"),
    )


def test_event_and_position_conversion_round_trip():
    events = np.array([0, 1, 0, 0, -1, 0, 1], dtype=np.int8)

    positions = _events_to_positions(events)

    assert positions.tolist() == [0, 1, 1, 1, 0, 0, 1]
    assert _positions_to_events(positions).tolist() == events.tolist()


def test_composite_strategy_combines_registered_strategy_states():
    spec = STRATEGIES["signal_composite"]
    params = SignalCompositeParams(
        mode="weighted",
        entry_threshold=0.6,
        exit_threshold=0.4,
        legs=[
            CompositeLeg(
                strategy_id="ema_crossover",
                weight=2,
                params={"fast_period": 5, "slow_period": 15},
            ),
            CompositeLeg(
                strategy_id="llt_trend",
                weight=1,
                params={"period": 10, "slope_lookback": 1},
            ),
        ],
    )
    df = _sample_ohlcv()
    base = BaseBacktestParams(initial_cash=100_000, commission=0.0003)

    result = spec.run(df, base, params)

    assert len(result.signal) == len(df)
    assert set(result.signal).issubset({-1, 0, 1})
    assert len(result.price) == len(df)
    assert {
        "leg_1_ema_crossover",
        "leg_1_ema_crossover__fast_ma",
        "leg_1_ema_crossover__slow_ma",
        "leg_2_llt_trend",
        "leg_2_llt_trend__llt",
        "composite_score",
        "composite_position",
    }.issubset(result.price[0])
    assert result.price[-1]["leg_1_ema_crossover__fast_ma"] is not None
    assert result.price[-1]["leg_2_llt_trend__llt"] is not None
    assert spec.compute_signals(df, base, params).tolist() == result.signal


def test_composite_rejects_unknown_child_strategy():
    spec = STRATEGIES["signal_composite"]
    params = SignalCompositeParams(
        legs=[
            CompositeLeg(strategy_id="does_not_exist"),
            CompositeLeg(strategy_id="ema_crossover"),
        ]
    )

    with pytest.raises(ValueError, match="未知子策略"):
        spec.min_bars(params)


def test_composite_requires_hysteresis_gap():
    with pytest.raises(ValueError, match="exit_threshold 必须小于 entry_threshold"):
        SignalCompositeParams(entry_threshold=0.5, exit_threshold=0.5)
