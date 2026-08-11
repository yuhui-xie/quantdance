"""高阶矩指标与 walk-forward 策略测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import app.strategies.higher_moment as higher_moment_strategy
from app.factors.higher_moment import rolling_standardized_moment
from app.strategies.higher_moment import (
    HigherMomentParams,
    _signals_from_smoothed,
    _walk_forward_ema,
)


def test_rolling_standardized_moment_matches_definition():
    returns = np.array([-0.03, -0.01, 0.0, 0.02, 0.06])
    close = 100.0 * np.exp(np.r_[0.0, np.cumsum(returns)])
    centered = returns - returns.mean()
    expected = np.mean((centered / returns.std(ddof=0)) ** 5)

    actual = rolling_standardized_moment(close, order=5, window=5)

    assert np.isnan(actual[:-1]).all()
    assert actual[-1] == pytest.approx(expected)


def test_higher_moment_zero_crossings_generate_signals():
    smoothed = np.array([np.nan, -0.2, -0.1, 0.1, 0.3, -0.2, -0.1, 0.2])

    signal = _signals_from_smoothed(smoothed)

    assert signal.tolist() == [0, -1, 0, 1, 0, -1, 0, 1]


def test_alpha_optimization_uses_only_prior_lookback(monkeypatch):
    rows = 400
    df = pd.DataFrame(
        {"close": np.linspace(10.0, 20.0, rows)},
        index=pd.date_range("2020-01-01", periods=rows, freq="D"),
    )
    moment = np.linspace(-1.0, 1.0, rows)
    params = HigherMomentParams()
    calls: list[tuple[int, int]] = []
    selected = iter([0.10, 0.30])

    def fake_select(_df, _moment, *, start, end, **_kwargs):  # noqa: ANN001
        calls.append((start, end))
        return next(selected)

    monkeypatch.setattr(higher_moment_strategy, "_select_alpha", fake_select)

    _, active_alpha = _walk_forward_ema(
        df,
        moment,
        params=params,
        initial_cash=100_000.0,
        commission=0.0003,
    )

    assert calls == [(0, 252), (90, 342)]
    assert np.all(active_alpha[:252] == pytest.approx(0.20))
    assert np.all(active_alpha[252:342] == pytest.approx(0.10))
    assert np.all(active_alpha[342:] == pytest.approx(0.30))


def test_higher_moment_defaults_and_minimum_bars():
    params = HigherMomentParams()

    assert params.order == 5
    assert params.window == 20
    assert params.optimize_interval == 90
    assert higher_moment_strategy.STRATEGY.min_bars(params) == 21
